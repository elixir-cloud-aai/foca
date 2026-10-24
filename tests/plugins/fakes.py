"""Fake plugins and entry points for the tests of `foca.plugins`."""

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from foca.plugins import (
    Backend,
    BackendResponse,
    Context,
    InboundRequest,
    NotApplicable,
    OutboundRequest,
    Plugin,
    PluginError,
    Rejected,
)
from foca.plugins.base import EmptyModel
from foca.plugins.config import PluginsConfig
from foca.plugins.engine import PluginHost
from foca.plugins.loader import LoadedPlugin, load_plugins


class TagParams(BaseModel):
    """Parameters of :py:class:`TagPlugin`.

    Attributes:
        key: Tag key.
        value: Tag value.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = "tagged"
    value: str = "yes"


class TagConfig(BaseModel):
    """Admin settings of :py:class:`TagPlugin`.

    Attributes:
        prefix: Prefix of tag values.
    """

    model_config = ConfigDict(extra="forbid")

    prefix: str = ""


class TagPlugin(Plugin[TagConfig, TagParams]):
    """Add a tag to the payload.

    Sets `tags[key]` of the payload to the prefixed value.
    """

    Config = TagConfig
    Params = TagParams
    capabilities = frozenset({"tagging"})
    maturity = "stable"

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Add the tag.

        Args:
            request: Inbound request.
            ctx: Context.
        """
        params: Any = ctx.params
        request.payload.setdefault("tags", {})[params.key] = (
            f"{self.config.prefix}{params.value}"
        )
        ctx.state["tagged"] = True
        ctx.record("key", params.key)


class FailingPlugin(Plugin[EmptyModel, EmptyModel]):
    """Change the payload, then fail."""

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Change the payload, then fail.

        Args:
            request: Inbound request.
            ctx: Context.

        Raises:
            PluginError: Always.
        """
        request.payload["broken"] = True
        raise PluginError("failed at https://user:secret@example.org/x")


class SkipPlugin(Plugin[EmptyModel, EmptyModel]):
    """Never applies."""

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Do not apply.

        Args:
            request: Inbound request.
            ctx: Context.

        Raises:
            NotApplicable: Always.
        """
        raise NotApplicable("never")


class RejectPlugin(Plugin[EmptyModel, EmptyModel]):
    """Reject requests."""

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Reject the request.

        Args:
            request: Inbound request.
            ctx: Context.

        Raises:
            Rejected: Always.
        """
        raise Rejected("not allowed", status=403)

    def on_response(self, response: BackendResponse, ctx: Context) -> None:
        """Reject the response, which counts as an error.

        Args:
            response: Response.
            ctx: Context.

        Raises:
            Rejected: Always.
        """
        raise Rejected("too late")


class SlowPlugin(Plugin[EmptyModel, EmptyModel]):
    """Hang until released."""

    release = threading.Event()

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Wait for the release, at most 5 seconds.

        Args:
            request: Inbound request.
            ctx: Context.
        """
        self.release.wait(5)
        request.payload["slow"] = True


class ReverseRouter(Plugin[EmptyModel, EmptyModel]):
    """Reverse the candidates."""

    def on_route(self, candidates: list[Backend], ctx: Context) -> list[Backend]:
        """Reverse the candidates.

        Args:
            candidates: Candidates.
            ctx: Context.

        Returns:
            Candidates, reversed.
        """
        return list(reversed(candidates))


class BadRouter(Plugin[EmptyModel, EmptyModel]):
    """Return a backend that is not a candidate."""

    def on_route(self, candidates: list[Backend], ctx: Context) -> list[Backend]:
        """Return an unknown backend.

        Args:
            candidates: Candidates.
            ctx: Context.

        Returns:
            An unknown backend.
        """
        return [Backend(id="https://evil.example.org", url="https://evil.example.org")]


class HeaderPlugin(Plugin[EmptyModel, TagParams]):
    """Set a header on outbound requests and on the client's response."""

    Params = TagParams

    def on_forward(self, outbound: OutboundRequest, ctx: Context) -> None:
        """Set a header.

        Args:
            outbound: Outbound request.
            ctx: Context.
        """
        params: Any = ctx.params
        outbound.headers[params.key] = params.value
        ctx.state.setdefault("forwards", 0)
        ctx.state["forwards"] += 1

    def on_response(self, response: BackendResponse, ctx: Context) -> None:
        """Set a header of the client's response.

        Args:
            response: Response.
            ctx: Context.
        """
        params: Any = ctx.params
        response.client_headers[params.key] = params.value


class RedirectPlugin(Plugin[EmptyModel, EmptyModel]):
    """Try to change the backend of outbound requests."""

    def on_forward(self, outbound: OutboundRequest, ctx: Context) -> None:
        """Change the backend.

        Args:
            outbound: Outbound request.
            ctx: Context.
        """
        outbound.backend = Backend(id="x", url="https://evil.example.org")

    def on_response(self, response: BackendResponse, ctx: Context) -> None:
        """Change the backend's status.

        Args:
            response: Response.
            ctx: Context.
        """
        response.status = 500


@dataclass
class FakeDistribution:
    """Distribution of fake entry points.

    Attributes:
        name: Distribution name.
        version: Version.
        record: Content of the `RECORD` file, if any.
    """

    name: str = "fake-plugins"
    version: str = "1.2.0"
    record: str | None = "fake/__init__.py,sha256=abc,10\n"

    def read_text(self, filename: str) -> str | None:
        """Read a metadata file.

        Args:
            filename: File name.

        Returns:
            The `RECORD` file, or `None`.
        """
        return self.record if filename == "RECORD" else None


@dataclass
class FakeEntryPoint:
    """Entry point of a fake plugin.

    Attributes:
        name: Name (plugin identifier).
        target: Object that the entry point refers to.
        dist: Distribution.
        loaded: Number of loads.
    """

    name: str
    target: Any
    dist: FakeDistribution | None = field(default_factory=FakeDistribution)
    loaded: int = 0

    @property
    def value(self) -> str:
        """Get the object reference.

        Returns:
            `module:attribute` of the target.
        """
        return f"tests.plugins.fakes:{getattr(self.target, '__name__', 'x')}"

    def load(self) -> Any:
        """Load the target.

        Returns:
            The target.
        """
        self.loaded += 1
        return self.target


FAKES: dict[str, type[Plugin[Any, Any]]] = {
    "test.tag": TagPlugin,
    "test.fail": FailingPlugin,
    "test.skip": SkipPlugin,
    "test.reject": RejectPlugin,
    "test.slow": SlowPlugin,
    "test.reverse": ReverseRouter,
    "test.bad-router": BadRouter,
    "test.header": HeaderPlugin,
    "test.redirect": RedirectPlugin,
}


def fake_entry_points(
    plugins: dict[str, Any] | None = None,
) -> Callable[[], Iterable[FakeEntryPoint]]:
    """Get a function listing fake entry points.

    Args:
        plugins: Targets by name; defaults to `FAKES`.

    Returns:
        Function returning the entry points.
    """
    eps = [FakeEntryPoint(name, target) for name, target in (plugins or FAKES).items()]
    return lambda: eps


def make_host(
    pipeline: list[dict[str, Any] | str],
    available: dict[str, dict[str, Any]] | None = None,
    **kwargs: Any,
) -> PluginHost:
    """Create a host with fake plugins.

    Args:
        pipeline: Default pipeline.
        available: Allowlist; defaults to all fakes.
        kwargs: Other arguments of `PluginHost`, and `defaults` and
            `gateway_id` of the configuration.

    Returns:
        Host.
    """
    conf = PluginsConfig.model_validate(
        {
            "available": available or {name: {} for name in FAKES},
            "pipeline": pipeline,
            "defaults": kwargs.pop("defaults", {}),
            "gateway_id": kwargs.pop("gateway_id", None),
            **{
                key: kwargs.pop(key)
                for key in (
                    "presets",
                    "client_pipelines",
                    "client_backends",
                    "tags",
                    "provenance",
                )
                if key in kwargs
            },
        }
    )
    plugins: dict[str, LoadedPlugin] = load_plugins(
        conf, api="tes", find_entry_points=fake_entry_points()
    )
    return PluginHost(conf, api="tes", plugins=plugins, **kwargs)


def wait_for(condition: Callable[[], bool], timeout: float = 5) -> bool:
    """Wait for a condition.

    Args:
        condition: Condition.
        timeout: Seconds to wait at most.

    Returns:
        Whether the condition was met.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False
