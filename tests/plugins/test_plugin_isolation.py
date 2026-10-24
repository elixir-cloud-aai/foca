"""Tests of isolation between calls, requests and gateways."""

import copy
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from foca.plugins import (
    Backend,
    Context,
    Headers,
    InboundRequest,
    OutboundRequest,
    Plugin,
    Principal,
)
from foca.plugins.config import PluginsConfig
from foca.plugins.engine import LoopDetected, PluginHost
from foca.plugins.loader import load_plugins
from tests.plugins.fakes import fake_entry_points

BACKEND = Backend(id="x", url="https://x.example.org")


class Params(BaseModel):
    """Parameters with a mutable field."""

    items: list[str] = []


class Mutator(Plugin[BaseModel, Params]):
    """Mutate the parameters and claims, then fail."""

    Params = Params
    seen: ClassVar[list[tuple[list[str], dict[str, Any]]]] = []

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Record what the call got, mutate it and fail.

        Args:
            request: Inbound request.
            ctx: Context.

        Raises:
            RuntimeError: Always.
        """
        params: Any = ctx.params
        Mutator.seen.append(
            (list(params.items), copy.deepcopy(dict(ctx.principal.claims)))
        )
        params.items.append("changed")
        with pytest.raises(TypeError):
            ctx.principal.claims["sub"] = "mallory"  # type: ignore[index]
        ctx.principal.claims["groups"].append("admin")
        raise RuntimeError("failed")


class Exiter(Plugin[BaseModel, BaseModel]):
    """Exit the process (badly)."""

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Raise `SystemExit`.

        Args:
            request: Inbound request.
            ctx: Context.

        Raises:
            SystemExit: Always.
        """
        raise SystemExit(1)


class ViaSetter(Plugin[BaseModel, BaseModel]):
    """Try to overwrite the `Via` header."""

    def on_forward(self, outbound: OutboundRequest, ctx: Context) -> None:
        """Remove the `Via` header.

        Args:
            outbound: Outbound request.
            ctx: Context.
        """
        outbound.headers["Via"] = "1.1 elsewhere"


def host(pipeline: list[Any], gateway_id: str | None = None) -> PluginHost:
    """Create a host with the plugins of this module.

    Args:
        pipeline: Default pipeline.
        gateway_id: Name of the gateway.

    Returns:
        Host.
    """
    targets = {"t.mutator": Mutator, "t.exiter": Exiter, "t.via": ViaSetter}
    conf = PluginsConfig.model_validate(
        {
            "available": {name: {} for name in targets},
            "pipeline": pipeline,
            "gateway_id": gateway_id,
        }
    )
    plugins = load_plugins(
        conf, api="tes", find_entry_points=fake_entry_points(targets)
    )
    return PluginHost(conf, api="tes", plugins=plugins)


def request(via: str | None = None) -> InboundRequest:
    """Build an inbound request.

    Args:
        via: `Via` header, if any.

    Returns:
        Request.
    """
    headers = Headers({"Via": via} if via else {})
    return InboundRequest(method="POST", path="/tasks", payload={}, headers=headers)


def test_params_and_claims_isolated() -> None:
    """Calls get their own parameters and read-only copies of the claims."""
    Mutator.seen.clear()
    gateway = host(
        [
            {
                "id": "s",
                "alternatives": [{"plugin": "t.mutator", "params": {"items": ["a"]}}]
                * 2,
                "on_failure": "skip",
            }
        ]
    )
    principal = Principal(subject="alice", claims={"sub": "alice", "groups": ["u"]})
    for _ in range(2):
        gateway.start(request(), principal).request()
    assert Mutator.seen == [(["a"], {"sub": "alice", "groups": ["u"]})] * 4
    assert principal.claims == {"sub": "alice", "groups": ["u"]}
    params: Any = gateway.default_pipeline.steps[0].alternatives[0].params
    assert params.items == ["a"]
    gateway.close()


def test_base_exceptions_recorded() -> None:
    """Hooks that raise `SystemExit` fail like others."""
    gateway = host([{"plugin": "t.exiter", "on_failure": "skip"}])
    run = gateway.start(request(), Principal())
    run.request()
    assert run.executions[0].outcome == "error"
    assert run.executions[0].error is not None
    assert "SystemExit" in run.executions[0].error
    gateway.close()


def forward(gateway: PluginHost, via: str | None) -> str | None:
    """Forward a request through a gateway.

    Args:
        gateway: Host.
        via: `Via` header of the inbound request, if any.

    Returns:
        `Via` header of the outbound request, if any.
    """
    run = gateway.start(request(via), Principal())
    attempt = next(run.attempts([BACKEND]))
    via_out = attempt.outbound.headers.get("Via")
    attempt.failed("connect_error")
    return via_out


def test_loops_through_gateways() -> None:
    """Loops through several gateways are detected, whatever plugins set."""
    a = host(["t.via"], gateway_id="a.example.org")
    b = host(["t.via"], gateway_id="b.example.org")
    via = forward(a, None)
    assert via == "1.1 a.example.org"
    via = forward(b, via)
    assert via == "1.1 a.example.org, 1.1 b.example.org"
    with pytest.raises(LoopDetected):
        a.start(request(via), Principal())
    anonymous = host(["t.via"])
    assert forward(anonymous, via) == via
    assert forward(anonymous, None) is None
    for gateway in (a, b, anonymous):
        gateway.close()


def test_interrupts_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interrupts are not swallowed."""

    def interrupt(self: Any, request: InboundRequest, ctx: Context) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Exiter, "on_request", interrupt)
    gateway = host([{"plugin": "t.exiter", "on_failure": "skip"}])
    with pytest.raises(KeyboardInterrupt):
        gateway.start(request(), Principal()).request()
    gateway.close()
