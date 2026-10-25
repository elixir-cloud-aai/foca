"""Tests of the plugin test kit (`foca.plugins.testing`, `pytest_plugin`)."""

import doctest
import importlib
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from foca.plugins import (
    Context,
    InboundRequest,
    OutboundRequest,
    Plugin,
    loader,
    testing,
)
from foca.plugins.base import EmptyModel
from foca.plugins.config import PluginsConfig
from foca.plugins.pytest_plugin import RegisterPlugins
from foca.plugins.routing import OrderedRouter
from foca.plugins.testing import (
    ContractViolation,
    FakeDistribution,
    FakeHttp,
    PluginHarness,
    check_plugin_contract,
    fake_entry_points,
    framework_imports,
    make_backend,
    make_inbound,
    make_outbound,
    make_principal,
    make_response,
)
from foca.utils.http import (
    DeadlineExceeded,
    InvalidRequestURL,
    RequestMaybeSent,
    RequestNotSent,
)
from tests.plugins.fakes import (
    FailingPlugin,
    HeaderPlugin,
    RejectPlugin,
    SlowPlugin,
    TagPlugin,
)


class GeoPlugin(Plugin[EmptyModel, EmptyModel]):
    """Record the country of a lookup service."""

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Look up the country.

        Args:
            request: Inbound request.
            ctx: Context.
        """
        assert ctx.http is not None
        ctx.record("country", ctx.http.get("https://geo.example.org/").json()["c"])


class BrokenForward(Plugin[EmptyModel, EmptyModel]):
    """Fail in the forward phase."""

    def on_forward(self, outbound: OutboundRequest, ctx: Context) -> None:
        """Fail.

        Args:
            outbound: Outbound request.
            ctx: Context.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("broken")


class Untitled(Plugin[EmptyModel, EmptyModel]):  # noqa: D101 (no title)
    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Do nothing.

        Args:
            request: Inbound request.
            ctx: Context.
        """


class Opaque:
    """Type without a JSON Schema."""


class OpaqueParams(BaseModel):
    """Parameters without a JSON Schema.

    Attributes:
        value: Value.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    value: Opaque | None = None


class OpaquePlugin(Untitled):
    """Have parameters without a JSON Schema."""

    Params = OpaqueParams


def test_doctests() -> None:
    """The examples of the kit work."""
    assert doctest.testmod(testing).failed == 0


def test_builders() -> None:
    """Builders give valid defaults."""
    principal = make_principal(groups=["admin"])
    assert principal.claims == {"sub": "user", "iss": "https://login.example.org"}
    assert principal.groups == ("admin",)
    assert make_principal(claims={}).claims == {}
    inbound = make_inbound(headers={"X-A": "1"}, query={"q": ["1"]})
    assert inbound.payload["executors"][0]["image"] == "alpine"
    assert inbound.headers["x-a"] == "1"
    assert make_inbound({"a": 1}).payload == {"a": 1}
    backend = make_backend("b", labels={"eu": "1"}, group="reg")
    assert backend.url == "https://b.example.org/ga4gh/tes/v1"
    assert backend.group == "reg"
    assert make_outbound({"a": 1}).backend.id == "a"
    response = make_response()
    assert response.client_payload == {"id": "remote-1"}
    assert make_response(client_payload={}).client_payload == {}


def test_fake_http() -> None:
    """Fake clients return canned responses in order and record calls."""
    http = FakeHttp()
    http.add("get", "https://x.example.org/a", json={"n": 1}, headers={"X-A": "1"})
    http.add("GET", "https://x.example.org/a", content=b"2")
    http.add("GET", "https://x.example.org/e", error=RequestMaybeSent("lost"))
    first = http.get("https://x.example.org/a")
    assert first.json() == {"n": 1}
    assert first.headers == {"x-a": "1"}
    assert http.get("https://x.example.org/a").content == b"2"
    assert http.get("https://x.example.org/a").content == b"2"
    with pytest.raises(RequestMaybeSent):
        http.get("https://x.example.org/e")
    with pytest.raises(RequestNotSent) as info:
        http.post("https://x.example.org/b", json={})
    assert info.value.reason == "no_canned_response"
    with pytest.raises(InvalidRequestURL):
        http.get("http://x.example.org/a")
    with pytest.raises(InvalidRequestURL):
        http.get("x.example.org")
    assert [call.method for call in http.calls] == ["GET"] * 4 + ["POST"]


def test_fake_http_clients() -> None:
    """Clients made by a fake share its state, with their own deadline."""
    http = FakeHttp(allow_http=True)
    http.add("GET", "http://x.example.org/?a=1&b=2", json=[])
    client = http.client(deadline=time.monotonic() + 10, allow_http=True)
    assert client.get("http://x.example.org/", params={"a": 1, "b": 2}).json() == []
    with pytest.raises(RequestNotSent):
        client.get("http://x.example.org/?a=1", params={"b": 2, "c": 3})
    assert [call.url for call in http.calls] == [
        "http://x.example.org/?a=1&b=2",
        "http://x.example.org/?a=1&b=2&c=3",
    ]
    late = http.client(deadline=time.monotonic() - 1)
    with pytest.raises(DeadlineExceeded):
        late.get("https://x.example.org/")


def test_harness_request() -> None:
    """Harnesses run request hooks with config and params."""
    with PluginHarness(
        TagPlugin, config={"prefix": "p-"}, params={"key": "k"}
    ) as harness:
        assert harness.run is None
        assert harness.executions == []
        inbound = harness.run_request(make_inbound({"name": "t"}))
        assert inbound.payload == {"name": "t", "tags": {"k": "p-yes"}}
        assert [e.outcome for e in harness.executions] == ["ok"]
        assert harness.executions[0].annotations == {"key": "k"}
        assert harness.plugin.entry_point == "tests.plugins.fakes:TagPlugin"


def test_harness_http() -> None:
    """Hooks get the fake HTTP client of the harness."""
    http = FakeHttp()
    http.add("GET", "https://geo.example.org/", json={"c": "SE"})
    with PluginHarness(GeoPlugin, http=http, timeout=5) as harness:
        harness.run_request()
        assert harness.executions[0].annotations == {"country": "SE"}
    assert len(http.calls) == 1
    assert 0 < http.calls[0].remaining <= 5


def test_harness_route() -> None:
    """Harnesses run route hooks on candidates."""
    with PluginHarness(OrderedRouter, plugin_id="foca.routing.ordered") as harness:
        assert [b.id for b in harness.run_route()] == ["a", "b", "c"]
        backends = [make_backend("x"), make_backend("y")]
        assert harness.run_route(backends) == backends


def test_harness_forward_and_response() -> None:
    """Harnesses run forward and response hooks."""
    with PluginHarness(HeaderPlugin, params={"key": "X-A", "value": "1"}) as harness:
        outbound = harness.run_forward()
        assert outbound is not None
        assert outbound.headers["X-A"] == "1"
        response = harness.run_response()
        assert response.client_headers["X-A"] == "1"
        assert [e.phase for e in harness.executions] == ["forward", "response"]
    with PluginHarness(BrokenForward) as harness:
        assert harness.run_forward() is None
        assert harness.executions[0].error == "RuntimeError"


def test_harness_invalid() -> None:
    """Invalid plugins and settings fail early; the plugin is closed."""
    closed: list[bool] = []

    class Closing(TagPlugin):
        """Record closing."""

        def close(self) -> None:
            """Record closing."""
            closed.append(True)

    with pytest.raises(ValueError, match="not a subclass"):
        PluginHarness(dict)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PluginHarness(Closing, params={"unknown": 1})
    assert closed == [True]


def test_contract_passes() -> None:
    """FOCA's router and well-behaved fakes meet the contract."""
    check_plugin_contract(OrderedRouter, plugin_id="foca.routing.ordered")
    check_plugin_contract(TagPlugin, params={"key": "k"}, threads=4)
    check_plugin_contract(HeaderPlugin, threads=4)
    check_plugin_contract(RejectPlugin, threads=2)  # rejecting is allowed
    http = FakeHttp()
    http.add("GET", "https://geo.example.org/", json={"c": "SE"})
    check_plugin_contract(GeoPlugin, http=http, threads=2)


@pytest.mark.parametrize(
    ("plugin", "kwargs", "problem"),
    [
        (dict, {}, "not a subclass"),
        (Untitled, {}, "No title"),
        (OpaquePlugin, {}, "`Params` exports no JSON Schema"),
        (TagPlugin, {"params": {"bad": 1}}, "Not usable"),
        (FailingPlugin, {"threads": 2}, "request (once): outcome error"),
        (FailingPlugin, {"threads": 2}, "request (threads): outcome error"),
        (GeoPlugin, {"threads": 1}, "outcome error (RequestNotSent)"),
        (BrokenForward, {"threads": 1}, "forward (once): outcome error"),
    ],
)
def test_contract_violations(plugin: Any, kwargs: dict[str, Any], problem: str) -> None:
    """Contract violations list actionable problems."""
    with pytest.raises(ContractViolation) as info:
        check_plugin_contract(plugin, **kwargs)
    assert any(problem in item for item in info.value.problems), info.value.problems
    assert "breaks the plugin contract" in str(info.value)


def test_errors_outside_hooks() -> None:
    """Errors outside the hooks are reported with their phase."""
    with PluginHarness(TagPlugin) as harness:
        inbound = make_inbound({"tags": "not a map"})
        problems = testing._check_hooks(harness, inbound, None, None, "once")
    assert len(problems) == 1
    assert problems[0].startswith("request (once): InvalidSelection")


def test_framework_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Imports of web frameworks are found in the plugin's module."""
    (tmp_path / "web_plugin.py").write_text(
        "import typing\n"
        "from foca.plugins import Plugin\n"
        "if typing.TYPE_CHECKING:\n"
        "    import flask.json\n"
        "    from starlette.requests import Request\n"
        "class WebPlugin(Plugin):\n"
        '    """Use a framework."""\n'
        "    def on_request(self, request, ctx):\n"
        "        pass\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("web_plugin")
    try:
        assert framework_imports(module.WebPlugin) == [
            "flask.json",
            "starlette.requests",
        ]
        with pytest.raises(ContractViolation) as info:
            check_plugin_contract(module.WebPlugin, threads=1)
        assert "Imports web frameworks" in info.value.problems[0]
    finally:
        sys.modules.pop("web_plugin", None)
    assert framework_imports(TagPlugin) == []
    hidden = type("Hidden", (TagPlugin,), {"__module__": "not.a.module"})
    assert framework_imports(hidden) == []


def test_relative_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relative imports are not framework imports."""
    package = tmp_path / "rel_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "flask.py").write_text("")
    (package / "plugin.py").write_text(
        "from . import flask\nfrom foca.plugins import Plugin\nclass P(Plugin):\n"
        "    def on_request(self, request, ctx):\n        pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        module = importlib.import_module("rel_pkg.plugin")
        assert framework_imports(module.P) == []
    finally:
        for name in ("rel_pkg", "rel_pkg.plugin", "rel_pkg.flask"):
            sys.modules.pop(name, None)


def test_threads_time_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calls that do not end are reported."""
    monkeypatch.setattr(testing, "THREAD_TIMEOUT", 0.2)
    SlowPlugin.release.clear()
    harness = PluginHarness(SlowPlugin, defaults={"workers": 2})
    try:
        problems = testing._check_threads(harness, None, None, None, threads=2)
        assert problems == ["threads: calls did not end within 0.2 s"]
    finally:
        SlowPlugin.release.set()
        harness.close()


def test_fake_entry_points() -> None:
    """Fake entry points load their targets and have references."""
    find = fake_entry_points({"test.tag": TagPlugin}, dist=FakeDistribution("d"))
    (entry_point,) = find()
    assert entry_point.value == "tests.plugins.fakes:TagPlugin"
    assert entry_point.group == "foca.plugins"
    assert entry_point.dist is not None
    assert entry_point.dist.name == "d"
    assert entry_point.load() is TagPlugin
    assert find()[0].loaded == 1
    assert fake_entry_points({"x": len})()[0].value == "builtins:len"
    assert fake_entry_points({"x": 1})()[0].value == "unknown:int"


def test_entry_point_fixture(fake_plugin_entry_points: RegisterPlugins) -> None:
    """The fixture registers fakes for the real loader path."""
    (entry_point,) = fake_plugin_entry_points({"org.example.tag": TagPlugin})
    assert loader.entry_points(group="foca.plugins") == [entry_point]
    assert loader.entry_points(group="console_scripts") == []
    conf = PluginsConfig.model_validate({"available": {"org.example.tag": {}}})
    loaded = loader.load_plugins(conf, api="tes")
    assert loaded["org.example.tag"].distribution == "fake-plugins"
    assert entry_point.loaded == 1
    loader.close_plugins(loaded.values())


def test_entry_point_fixture_restored() -> None:
    """The lookup is restored after tests with the fixture."""
    assert loader.entry_points is importlib.metadata.entry_points
