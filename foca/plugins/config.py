"""Configuration models of gateway plugins (FOCA config section `plugins`)."""

import re
from typing import Any, Literal, Self

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)

PLUGIN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*")
"""Pattern of plugin identifiers (entry point names), e.g., `tes.data.crypt4gh`."""

STEP_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
"""Pattern of step identifiers."""

# Keys of steps reserved for later versions (conditions, dependencies)
_RESERVED_STEP_KEYS = ("when", "needs")
# Maximum length of plugin identifiers
_MAX_PLUGIN_ID_LENGTH = 128

GATEWAY_ID_PATTERN = (
    r"^(?:[A-Za-z0-9._-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::[0-9]{1,5})?$"
)
"""Pattern of gateway names in `Via` headers (host, optional port)."""


class _Model(BaseModel):
    """Base of the plugin configuration models; unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


def check_plugin_id(plugin_id: str) -> str:
    """Check a plugin identifier.

    Args:
        plugin_id: Plugin identifier.

    Returns:
        The identifier.

    Raises:
        ValueError: The identifier does not match `PLUGIN_ID_PATTERN` or is
            longer than 128 characters.
    """
    if len(plugin_id) > _MAX_PLUGIN_ID_LENGTH or not PLUGIN_ID_PATTERN.fullmatch(
        plugin_id
    ):
        raise ValueError(f"Invalid plugin identifier: {plugin_id!r}")
    return plugin_id


class AlternativeConfig(_Model):
    """One alternative of a pipeline step: a plugin with parameters.

    Args:
        plugin: Plugin identifier, from `plugins.available`.
        params: Parameters, validated against the plugin's `Params` model.

    Attributes:
        plugin: Plugin identifier.
        params: Parameters.
    """

    plugin: str
    params: dict[str, Any] = {}

    @field_validator("plugin")
    @classmethod
    def _check_plugin(cls, plugin_id: str) -> str:
        """Check the plugin identifier.

        Args:
            plugin_id: Plugin identifier.

        Returns:
            The identifier.
        """
        return check_plugin_id(plugin_id)


class StepConfig(_Model):
    """Step of a pipeline: ordered alternatives (the first that succeeds wins).

    The short forms `<id>` and `{plugin: <id>, params: {...}}` are steps
    with one alternative, whose identifier defaults to the plugin identifier. The
    keys `when` and `needs` are reserved for later versions.

    Args:
        id: Identifier of the step, unique in the pipeline; defaults to the
            plugin of the first alternative.
        alternatives: Alternatives, in order; at least one.
        timeout: Seconds per alternative and call (default:
            `plugins.defaults.timeout`).
        on_failure: What happens when all alternatives failed: `fail` the
            request, or `skip` the step.
        if_missing_phase: Only for steps of `plugins.client_pipelines.append`:
            add the step only if no other step implements this phase (e.g.,
            a default `route` step).

    Attributes:
        id: Identifier of the step.
        alternatives: Alternatives, in order.
        timeout: Seconds per alternative and call, if set.
        on_failure: What happens when all alternatives failed.
        if_missing_phase: Phase that the step is a default for, if any.
    """

    id: str | None = None
    alternatives: list[AlternativeConfig] = Field(min_length=1)
    timeout: PositiveFloat | None = None
    on_failure: Literal["fail", "skip"] = "fail"
    if_missing_phase: Literal["request", "route", "forward", "response"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _short_form(cls, data: Any) -> Any:
        """Expand the short form and reject reserved keys.

        Args:
            data: Step, as configured.

        Returns:
            Step in the long form.

        Raises:
            ValueError: The step uses a reserved key, or both forms.
        """
        if isinstance(data, str):
            data = {"plugin": data}
        if not isinstance(data, dict):
            return data
        reserved = [key for key in _RESERVED_STEP_KEYS if key in data]
        if reserved:
            raise ValueError(
                f"Step keys {reserved} are reserved for a later version of FOCA"
            )
        if "plugin" in data:
            if "alternatives" in data:
                raise ValueError("Steps have either 'plugin' or 'alternatives'")
            data = dict(data)
            alternative = {"plugin": data.pop("plugin")}
            if "params" in data:
                alternative["params"] = data.pop("params")
            data["alternatives"] = [alternative]
        return data

    @model_validator(mode="after")
    def _default_id(self) -> Self:
        """Default the identifier to the first alternative's plugin.

        Returns:
            The step.

        Raises:
            ValueError: The identifier does not match `STEP_ID_PATTERN`.
        """
        if self.id is None:
            self.id = self.alternatives[0].plugin[:64]
        if not STEP_ID_PATTERN.fullmatch(self.id):
            raise ValueError(f"Invalid step identifier: {self.id!r}")
        return self


def check_pipeline(steps: list[StepConfig]) -> list[StepConfig]:
    """Check that the steps of a pipeline have unique identifiers.

    Args:
        steps: Steps.

    Returns:
        The steps.

    Raises:
        ValueError: Two steps have the same identifier.
    """
    seen: set[str] = set()
    for step in steps:
        if step.id in seen:
            raise ValueError(f"Duplicate step identifier {step.id!r}: set unique 'id's")
        seen.add(step.id or "")
    return steps


class AvailablePluginConfig(_Model):
    """Entry of the allowlist of plugins (`plugins.available`).

    Args:
        distribution: Name of the distribution that must provide the entry
            point, if pinned.
        version: PEP 440 version specifier that the distribution's version
            must match, if pinned (e.g., `>=1.0,<2`).
        config: Admin settings, validated against the plugin's `Config`.
        client_selectable: Whether clients may choose the plugin in
            per-request pipelines.
        roles: Roles that may choose the plugin (any if empty).
        enabled: Whether the plugin is loaded; if `false`, its alternatives
            are dropped from all pipelines (a kill switch that keeps the
            entry).

    Attributes:
        distribution: Pinned distribution, if any.
        version: Version specifier, if any.
        config: Admin settings.
        client_selectable: Whether clients may choose the plugin.
        roles: Roles that may choose the plugin.
        enabled: Whether the plugin is loaded.
    """

    distribution: str | None = None
    version: str | None = None
    config: dict[str, Any] = {}
    client_selectable: bool = True
    roles: list[str] = []
    enabled: bool = True

    @field_validator("version")
    @classmethod
    def _check_version(cls, version: str | None) -> str | None:
        """Check the version specifier.

        Args:
            version: Version specifier, if any.

        Returns:
            The specifier.

        Raises:
            ValueError: The specifier is invalid as per PEP 440.
        """
        if version is not None:
            try:
                SpecifierSet(version)
            except InvalidSpecifier as exc:
                raise ValueError(f"Invalid version specifier: {version!r}") from exc
        return version


class HttpConfig(_Model):
    """Outbound HTTP of plugins (`plugins.defaults.http`, cf. `ctx.http`).

    Args:
        allow_private_addresses: Whether plugins may connect to addresses
            that are not public (e.g., internal services of a private
            deployment); off by default against SSRF.
        allow_http: Whether plugins may use `http` URLs.
        max_response_size: Maximum size of response bodies, in bytes.

    Attributes:
        allow_private_addresses: Whether private addresses are allowed.
        allow_http: Whether `http` URLs are allowed.
        max_response_size: Maximum size of response bodies, in bytes.
    """

    allow_private_addresses: bool = False
    allow_http: bool = False
    max_response_size: PositiveInt = 16 * 1024 * 1024


class PluginDefaultsConfig(_Model):
    """Limits of plugin calls and forwarding (`plugins.defaults`).

    Args:
        timeout: Seconds per alternative and call, unless set by the step.
        pipeline_timeout: Seconds for the `request` and `route` phases of a
            request together.
        forward_timeout: Seconds for all forwarding attempts of a request
            together; when exceeded, no further backend is tried (`503`).
        workers: Threads that run hooks (per process); calls beyond fail
            at once, as timed out.
        http: Outbound HTTP of plugins (`ctx.http`).

    Attributes:
        timeout: Seconds per alternative and call.
        pipeline_timeout: Seconds for the `request` and `route` phases.
        forward_timeout: Seconds for all forwarding attempts.
        workers: Threads that run hooks.
        http: Outbound HTTP of plugins.
    """

    timeout: PositiveFloat = 10
    pipeline_timeout: PositiveFloat = 30
    forward_timeout: PositiveFloat = 60
    workers: PositiveInt = 8
    http: HttpConfig = HttpConfig()


class ClientPipelinesConfig(_Model):
    """Pipelines chosen by clients per request (`plugins.client_pipelines`).

    Clients choose a pipeline in the reserved tag `<prefix>pipeline` of the
    request's payload (cf. :py:mod:`foca.plugins.selection`): the name of a
    preset, or a pipeline as JSON.

    Args:
        enabled: Whether clients may choose pipelines; if not, requests with
            the tag are refused (`400`).
        allowed: Forms that clients may use: `presets` (names) and `inline`
            (JSON).
        max_steps: Maximum number of steps of inline pipelines.
        max_alternatives: Maximum number of alternatives per step.
        max_size: Maximum size of the tag's value, in bytes.
        roles: Roles that may choose pipelines (any authenticated caller if
            empty).
        prepend: Steps run before the client's steps (e.g., policies);
            clients cannot remove or reorder them.
        append: Steps run after the client's steps; steps with
            `if_missing_phase` only if no other step implements that phase
            (e.g., a default `route` step).

    Attributes:
        enabled: Whether clients may choose pipelines.
        allowed: Forms that clients may use.
        max_steps: Maximum number of steps of inline pipelines.
        max_alternatives: Maximum number of alternatives per step.
        max_size: Maximum size of the tag's value, in bytes.
        roles: Roles that may choose pipelines.
        prepend: Steps run before the client's steps.
        append: Steps run after the client's steps.
    """

    enabled: bool = True
    allowed: list[Literal["presets", "inline"]] = ["presets", "inline"]
    max_steps: PositiveInt = 8
    max_alternatives: PositiveInt = 3
    max_size: PositiveInt = 8192
    roles: list[str] = []
    prepend: list[StepConfig] = []
    append: list[StepConfig] = []

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        """Check that the frame's steps have unique identifiers.

        Returns:
            The configuration.
        """
        check_pipeline([*self.prepend, *self.append])
        return self


class ClientBackendsConfig(_Model):
    """Backends chosen by clients per request (`plugins.client_backends`).

    Clients choose backends in the reserved tag `<prefix>backends` (cf.
    :py:mod:`foca.plugins.selection`); only candidates of the request (i.e.,
    configured or verified backends of the app's API) can be chosen, others
    are discarded.

    Args:
        enabled: Whether clients may choose backends; if not, requests with
            the tag are refused (`400`).
        max_items: Maximum number of backends in the tag.

    Attributes:
        enabled: Whether clients may choose backends.
        max_items: Maximum number of backends in the tag.
    """

    enabled: bool = True
    max_items: PositiveInt = 32


class TagsConfig(_Model):
    """Reserved tags of gateway requests (`plugins.tags`).

    Args:
        prefix: Prefix of the reserved tags (e.g., `gateway.pipeline`):
            clients may set only the pipeline and backends tags under it.

    Attributes:
        prefix: Prefix of the reserved tags.
    """

    prefix: str = Field(default="gateway.", pattern=r"^[a-z0-9][a-z0-9_.-]{0,31}\.$")


class ProvenanceConfig(_Model):
    """Provenance of gateway requests (`plugins.provenance`).

    Args:
        detail: What is recorded of the changes of each plugin call:
            `paths`, the hashes before and after and the changed JSON
            Pointers, without values (the only level for now).
        record_failed: Whether records of failed pipeline runs (e.g.,
            rejected requests, which have no task) are stored for audits,
            in a collection of the app with a retention (TTL index); off by
            default, so that no record of rejected input is kept.

    Attributes:
        detail: What is recorded of the changes of each plugin call.
        record_failed: Whether records of failed pipeline runs are stored.
    """

    detail: Literal["paths"] = "paths"
    record_failed: bool = False


class PluginsConfig(_Model):
    """Gateway plugins: allowlist and pipelines (FOCA config section `plugins`).

    Args:
        available: Allowlist of plugins by identifier (entry point name in
            the group `foca.plugins`): only these are loaded, even if more
            are installed.
        defaults: Limits of plugin calls and forwarding.
        pipeline: Default pipeline: steps, in order.
        presets: Named pipelines that clients may choose.
        client_pipelines: Pipelines chosen by clients per request.
        client_backends: Backends chosen by clients per request.
        tags: Reserved tags of gateway requests.
        provenance: Provenance of gateway requests.
        gateway_id: Name of this gateway in `Via` headers (RFC 9110,
            section 7.6.3), which detect routing loops: a host name or IPv4
            address, or an IPv6 address in brackets, with an optional port;
            defaults to the host and port of `server.public_url`. Without
            either, loops are not detected.

    Attributes:
        available: Allowlist of plugins.
        defaults: Limits of plugin calls and forwarding.
        pipeline: Default pipeline.
        presets: Named pipelines that clients may choose.
        client_pipelines: Pipelines chosen by clients per request.
        client_backends: Backends chosen by clients per request.
        tags: Reserved tags of gateway requests.
        provenance: Provenance of gateway requests.
        gateway_id: Name of this gateway in `Via` headers, if set.

    Examples:
        >>> conf = PluginsConfig(
        ...     available={"org.example.tes.policy.images": {}},
        ...     pipeline=[{"plugin": "org.example.tes.policy.images"}],
        ... )
        >>> conf.pipeline[0].id
        'org.example.tes.policy.images'
    """

    available: dict[str, AvailablePluginConfig] = {}
    defaults: PluginDefaultsConfig = PluginDefaultsConfig()
    pipeline: list[StepConfig] = []
    presets: dict[str, list[StepConfig]] = {}
    client_pipelines: ClientPipelinesConfig = ClientPipelinesConfig()
    client_backends: ClientBackendsConfig = ClientBackendsConfig()
    tags: TagsConfig = TagsConfig()
    provenance: ProvenanceConfig = ProvenanceConfig()
    gateway_id: str | None = Field(default=None, pattern=GATEWAY_ID_PATTERN)

    @field_validator("pipeline")
    @classmethod
    def _check_pipeline(cls, steps: list[StepConfig]) -> list[StepConfig]:
        """Check the default pipeline.

        Args:
            steps: Steps.

        Returns:
            The steps.
        """
        return check_pipeline(steps)

    @field_validator("presets")
    @classmethod
    def _check_presets(
        cls, presets: dict[str, list[StepConfig]]
    ) -> dict[str, list[StepConfig]]:
        """Check the names and steps of the presets.

        Args:
            presets: Presets.

        Returns:
            The presets.

        Raises:
            ValueError: A name does not match the pattern of preset names.
        """
        for name, steps in presets.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
                raise ValueError(f"Preset name {name!r} does not match the pattern")
            check_pipeline(steps)
        return presets

    @field_validator("available")
    @classmethod
    def _check_available(
        cls, available: dict[str, AvailablePluginConfig]
    ) -> dict[str, AvailablePluginConfig]:
        """Check the plugin identifiers of the allowlist.

        Args:
            available: Allowlist.

        Returns:
            The allowlist.
        """
        for plugin_id in available:
            check_plugin_id(plugin_id)
        return available

    def pipelines(self) -> dict[str, list[StepConfig]]:
        """Get all admin-defined pipelines, by name.

        Returns:
            Pipelines, by name: `pipeline` (default), `presets.<name>`,
            `prepend` and `append` (the frame of client pipelines).
        """
        return {
            "pipeline": self.pipeline,
            **{f"presets.{name}": steps for name, steps in self.presets.items()},
            "prepend": self.client_pipelines.prepend,
            "append": self.client_pipelines.append,
        }

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        """Check that pipelines refer to allowlisted plugins only.

        Returns:
            The configuration.

        Raises:
            ValueError: A pipeline refers to a plugin that is not in
                `available`, or a step other than in
                `client_pipelines.append` sets `if_missing_phase`.
        """
        for name, steps in self.pipelines().items():
            for step in steps:
                if step.if_missing_phase is not None and name != "append":
                    raise ValueError(
                        f"'if_missing_phase' is only allowed in"
                        f" 'client_pipelines.append' (step {step.id!r} of {name})"
                    )
                for alternative in step.alternatives:
                    if alternative.plugin not in self.available:
                        raise ValueError(
                            f"Plugin {alternative.plugin!r} of {name} is not in"
                            " 'plugins.available'"
                        )
        return self
