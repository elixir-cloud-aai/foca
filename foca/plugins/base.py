"""Base class and exceptions for gateway plugins.

A plugin is a class deriving from :py:class:`Plugin`, registered under an
entry point of the group `foca.plugins`, e.g., in the plugin's
`pyproject.toml`:

```toml
[project.entry-points."foca.plugins"]
"org.example.tes.policy.images" = "example_plugins.images:AllowedImages"
```

It implements any of the hooks of the phases of a gateway request, which
get plain data (cf. :py:mod:`foca.plugins.model`) and a
:py:class:`foca.plugins.context.Context`:

- `request`, once: `on_request(request, ctx) -> None` changes the inbound
  request (e.g., its payload) in place;
- `route`, once: `on_route(candidates, ctx) -> list[Backend]` returns a
  subset of the candidate backends, in order;
- `forward`, per attempt: `on_forward(outbound, ctx) -> None` changes the
  outbound request in place;
- `response`, per attempt: `on_response(response, ctx) -> None` changes the
  response to the client in place.

Hooks are plain functions (`def`); FOCA 1.0 will also accept `async def`
hooks with the same arguments. Hooks get a copy of their input, which the
engine keeps only if the hook succeeds, so a failing hook leaves no trace.
A plugin instance serves all requests of a process: keep per-request data
in `ctx.state`, not on `self`.
"""

import re
from typing import Any, ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic.errors import PydanticUserError

API_VERSION = 1
"""Major version of the plugin API; plugins declare it as `api_version`."""

PHASES: tuple[str, ...] = ("request", "route", "forward", "response")
"""Phases of a gateway request, in order; hooks are named `on_<phase>`."""

REJECTION_STATUSES = frozenset({400, 403, 409, 413, 422})
"""HTTP statuses that plugins may reject requests with."""

CAPABILITY_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
"""Pattern of capability descriptors, e.g., `crypt4gh-input`."""

Maturity = Literal["experimental", "stable", "deprecated"]


class EmptyModel(BaseModel):
    """Model without fields, the default `Config` and `Params` of plugins."""

    model_config = ConfigDict(extra="forbid")


ConfigT = TypeVar("ConfigT", bound=BaseModel)
ParamsT = TypeVar("ParamsT", bound=BaseModel)


class NotApplicable(Exception):
    """The plugin does not apply to the request.

    The engine tries the next alternative of the step, without logging an
    error (e.g., distance-based routing for a task without inputs).
    """


class Rejected(Exception):
    """The plugin rejects the request, e.g., as a policy does not allow it.

    The engine stops at once, without trying other alternatives, and the
    client gets the status, title and detail as problem details.

    Attributes:
        status: HTTP status of the response to the client.
        title: Title of the problem.
        detail: Description of the problem, for the client.
    """

    def __init__(
        self,
        detail: str,
        status: int = 400,
        title: str | None = None,
    ) -> None:
        """Reject a request.

        Args:
            detail: Description of the problem, for the client.
            status: HTTP status: one of `REJECTION_STATUSES`.
            title: Title of the problem; defaults to the reason phrase of
                the status.

        Raises:
            ValueError: The status is not one of `REJECTION_STATUSES`.
        """
        if status not in REJECTION_STATUSES:
            raise ValueError(
                f"Rejections need one of the statuses {sorted(REJECTION_STATUSES)}"
            )
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


class InvalidParams(ValueError):
    """Parameters of a pipeline step are not allowed (cf. `check_params`)."""


class PluginError(Exception):
    """A plugin failed; the engine tries the next alternative.

    Any other exception raised by a hook is treated the same way.
    """


class Plugin(Generic[ConfigT, ParamsT]):
    """Base class of gateway plugins.

    Subclasses set the class attributes below and implement any of the
    hooks `on_request`, `on_route`, `on_forward` and `on_response` (cf. the
    module documentation).

    Attributes:
        Config: Model of the admin settings of the plugin (`config` of its
            entry in `plugins.available`).
        Params: Model of the parameters of the plugin in a pipeline (set by
            admins or, in per-request pipelines, by clients).
        api_version: Major version of the plugin API that the plugin
            implements (`API_VERSION`).
        apis: APIs that the plugin supports (e.g., `{"tes"}`), or `{"*"}`
            for all.
        capabilities: Generic descriptors of what the plugin provides, from
            a community vocabulary (e.g., `crypt4gh-input`), announced in
            the service info; no package names or code references.
        maturity: `experimental`, `stable` or `deprecated`.
        title: Short name; defaults to the first line of the docstring.
        description: Description for clients; defaults to the docstring.
        data_transfers: Third parties that the plugin sends request data to,
            e.g., `["geolocation service db-ip.com"]`, listed for clients.
        id: Identifier of the plugin (its entry point name).
        config: Validated admin settings.
    """

    Config: ClassVar[type[BaseModel]] = EmptyModel
    Params: ClassVar[type[BaseModel]] = EmptyModel
    api_version: ClassVar[int] = API_VERSION
    apis: ClassVar[frozenset[str]] = frozenset({"*"})
    capabilities: ClassVar[frozenset[str]] = frozenset()
    maturity: ClassVar[Maturity] = "experimental"
    title: ClassVar[str | None] = None
    description: ClassVar[str | None] = None
    data_transfers: ClassVar[tuple[str, ...]] = ()

    def __init__(self, plugin_id: str, config: ConfigT) -> None:
        """Create the plugin instance of a process.

        Args:
            plugin_id: Identifier of the plugin.
            config: Validated admin settings.
        """
        self.id = plugin_id
        self.config = config
        self.setup(config)

    def setup(self, config: ConfigT) -> None:
        """Set up the plugin; override as needed.

        It may run more than once per process (e.g., FOCA loads the
        plugins once on startup, then again in each worker process), so it
        must be idempotent.

        Args:
            config: Validated admin settings.
        """

    def close(self) -> None:
        """Release resources at shutdown; override as needed."""

    def check_params(self, params: ParamsT) -> None:
        """Check parameters against the admin settings; override as needed.

        Called when pipelines are resolved (at startup, and per request for
        clients' pipelines), after `Params` validated them.

        Args:
            params: Validated parameters of a pipeline step.

        Raises:
            InvalidParams: The parameters are not allowed. Other exceptions
                are plugin errors: they are logged, and stop the app at
                startup or fail the request (`PluginError`).
        """

    @classmethod
    def phases(cls) -> tuple[str, ...]:
        """Get the phases that the plugin implements hooks for.

        Returns:
            Phases, in order (cf. `PHASES`).
        """
        return tuple(
            phase for phase in PHASES if callable(getattr(cls, f"on_{phase}", None))
        )

    @classmethod
    def summary(cls) -> tuple[str, str]:
        """Get the title and description of the plugin.

        Returns:
            Title and description, from the class attributes or the
            docstring.
        """
        doc = (cls.__doc__ or "").strip()
        first, _, rest = doc.partition("\n")
        title = cls.title or first.strip() or cls.__name__
        description = cls.description or (rest.strip() or title)
        return title, description


def check_plugin_class(cls: Any, plugin_id: str, api: str) -> type[Plugin[Any, Any]]:
    """Check that a class loaded from an entry point is a valid plugin.

    Args:
        cls: Object that the entry point refers to.
        plugin_id: Identifier of the plugin, for messages.
        api: API of the app (e.g., `tes`).

    Returns:
        The class.

    Raises:
        ValueError: The object is not a subclass of :py:class:`Plugin`, it
            implements another major version of the plugin API, does not
            support the app's API, implements no hook, has invalid
            metadata (e.g., a title that is no string), or parameters
            without JSON Schema (which the plugin listing serves).
    """
    if not isinstance(cls, type) or not issubclass(cls, Plugin):
        raise ValueError(
            f"Plugin {plugin_id!r} is not a subclass of foca.plugins.Plugin"
        )
    if cls.api_version != API_VERSION:
        raise ValueError(
            f"Plugin {plugin_id!r} implements plugin API version {cls.api_version},"
            f" not {API_VERSION}"
        )
    if "*" not in cls.apis and api not in cls.apis:
        raise ValueError(f"Plugin {plugin_id!r} does not support API {api!r}")
    if not cls.phases():
        raise ValueError(f"Plugin {plugin_id!r} implements no hook")
    if cls.maturity not in ("experimental", "stable", "deprecated"):
        raise ValueError(f"Plugin {plugin_id!r} has an invalid maturity")
    # metadata that the plugin listing serves as JSON
    for name in ("title", "description"):
        if not isinstance(getattr(cls, name), str | None):
            raise ValueError(f"Plugin {plugin_id!r} has an invalid {name}")
    transfers = cls.data_transfers
    if not isinstance(transfers, tuple | list) or not all(
        isinstance(transfer, str) for transfer in transfers
    ):
        raise ValueError(f"Plugin {plugin_id!r} has invalid data_transfers")
    for model in (cls.Config, cls.Params):
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise ValueError(
                f"Plugin {plugin_id!r} has no pydantic Config/Params model"
            )
    try:
        cls.Params.model_json_schema()
    except PydanticUserError as error:  # e.g., arbitrary types
        raise ValueError(
            f"Plugin {plugin_id!r} has Params without JSON Schema: {error}"
        ) from error
    invalid = sorted(
        repr(capability)
        for capability in cls.capabilities
        if not (
            isinstance(capability, str) and CAPABILITY_PATTERN.fullmatch(capability)
        )
    )
    if invalid:
        raise ValueError(f"Plugin {plugin_id!r} has invalid capabilities: {invalid}")
    return cls
