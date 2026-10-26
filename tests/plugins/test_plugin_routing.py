"""Tests of the default router (`foca.plugins.routing`) and built-in plugins."""

import functools
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from foca.plugins import Backend, InvalidSelection, PluginError, loader
from foca.plugins.config import BUILTIN_PLUGINS, PluginsConfig
from foca.plugins.engine import PluginHost
from foca.plugins.loader import load_plugins
from foca.plugins.routing import OrderedRouter
from foca.plugins.testing import FakeDistribution, FakeEntryPoint
from tests.plugins.fakes import make_host
from tests.plugins.test_plugin_engine import PRINCIPAL, inbound

CANDIDATES = [
    Backend(id="a", url="https://a.example.org", labels={"region": "eu"}),
    *(
        Backend(
            id=f"r{index}",
            url=f"https://r{index}.example.org",
            source="discovered",
            group="reg",
            labels={"region": "eu" if index % 2 else "us"},
        )
        for index in range(4)
    ),
    Backend(id="b", url="https://b.example.org"),
    Backend(id="s0", url="https://s0.example.org", source="discovered", group="other"),
]


def route(
    params: dict[str, Any] | None = None,
    candidates: list[Backend] | None = None,
    **kwargs: Any,
) -> tuple[list[str], Any]:
    """Route the candidates with the router.

    Args:
        params: Parameters of the router.
        candidates: Candidates; default to `CANDIDATES`.
        kwargs: Other arguments of `make_host`.

    Returns:
        Identifiers of the routed candidates, and the run.
    """
    host = make_host(
        [{"plugin": "foca.routing.ordered", "params": params or {}}], **kwargs
    )
    run = host.start(inbound(), PRINCIPAL)
    routed = run.route(CANDIDATES if candidates is None else candidates)
    host.close()
    return [backend.id for backend in routed], run


def test_order_kept_groups_shuffled() -> None:
    """Configured backends keep their positions; registry groups are shuffled."""
    orders: Counter[tuple[str, ...]] = Counter()
    for _ in range(200):
        ids, run = route()
        assert ids[0] == "a"
        assert ids[5:] == ["b", "s0"]
        assert sorted(ids[1:5]) == ["r0", "r1", "r2", "r3"]
        orders[tuple(ids[1:5])] += 1
    assert len(orders) > 5
    execution = run.executions[0]
    assert execution.plugin == "foca.routing.ordered"
    assert execution.annotations == {"shuffled_groups": 2}


def test_seed() -> None:
    """Seeds give reproducible orders, if allowed."""
    available = {"foca.routing.ordered": {"config": {"allow_seed": True}}}
    first, _ = route({"seed": 7}, available=available)
    second, _ = route({"seed": 7}, available=available)
    assert first == second
    with pytest.raises(ValueError, match="'seed' is not allowed"):
        route({"seed": 7})


def test_labels() -> None:
    """Candidates without the labels are dropped."""
    ids, run = route({"labels": {"region": "eu"}})
    assert ids[0] == "a"
    assert sorted(ids[1:]) == ["r1", "r3"]
    assert run.executions[0].annotations == {"filtered": 4, "shuffled_groups": 1}


def test_no_groups() -> None:
    """Configured backends only are not shuffled."""
    configured = [
        Backend(id=f"c{index}", url=f"https://c{index}.example.org")
        for index in range(8)
    ]
    for _ in range(5):
        ids, run = route(candidates=configured)
        assert ids == [f"c{index}" for index in range(8)]
        assert "shuffled_groups" not in run.executions[0].annotations
    ids, run = route({"labels": {"nothing": "x"}})
    assert ids == []
    assert "shuffled_groups" not in run.executions[0].annotations


def test_appended_to_client_pipelines() -> None:
    """Pipelines of clients without a route step get the router."""
    host = make_host([{"plugin": "test.tag"}], client_pipelines={"enabled": True})
    run = host.start(
        inbound({"tags": {"gateway.pipeline": '[{"plugin": "test.tag"}]'}}), PRINCIPAL
    )
    assert [step.id for step in run.pipeline.steps] == [
        "test.tag",
        "foca.routing.ordered",
    ]
    run = host.start(
        inbound({"tags": {"gateway.pipeline": '[{"plugin": "test.reverse"}]'}}),
        PRINCIPAL,
    )
    assert [step.id for step in run.pipeline.steps] == ["test.reverse"]


def test_prepended_route_steps_keep_router() -> None:
    """Route steps in the admin's prepend do not drop the appended router."""
    host = make_host(
        [{"plugin": "foca.routing.ordered"}],
        client_pipelines={"enabled": True, "prepend": [{"plugin": "test.reverse"}]},
    )
    run = host.start(inbound({"tags": {"gateway.pipeline": "[]"}}), PRINCIPAL)
    assert [step.id for step in run.pipeline.steps] == [
        "test.reverse",
        "foca.routing.ordered",
    ]
    host.close()


def test_disabled() -> None:
    """The router can be disabled; required steps then stop the app."""
    available = {"foca.routing.ordered": {"enabled": False}}
    with pytest.raises(ValueError, match="no enabled plugin"):
        make_host([{"plugin": "foca.routing.ordered"}], available=available)
    host = make_host([{"plugin": "test.tag"}], available={**available, "test.tag": {}})
    assert "foca.routing.ordered" not in host.plugins
    assert host.default_pipeline.steps[0].id == "test.tag"


def test_default_pipeline() -> None:
    """The default pipeline is the router."""
    host = PluginHost(PluginsConfig(), api="wes")
    assert [step.id for step in host.default_pipeline.steps] == ["foca.routing.ordered"]
    host.close()


def test_builtin_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """Built-ins load from `BUILTIN_PLUGINS`, never from other distributions."""
    loaded = load_plugins(PluginsConfig(), api="tes", find_entry_points=lambda: [])
    plugin = loaded["foca.routing.ordered"]
    assert plugin.cls is OrderedRouter
    assert plugin.distribution == "foca"
    assert plugin.version == version("foca")
    assert plugin.entry_point == "foca.plugins.routing:OrderedRouter"
    # entry points of FOCA's older metadata are ignored
    own = SimpleNamespace(
        name=plugin.id,
        value=BUILTIN_PLUGINS[plugin.id],
        dist=FakeDistribution(name="foca"),
    )
    loaded = load_plugins(PluginsConfig(), api="tes", find_entry_points=lambda: [own])
    assert loaded["foca.routing.ordered"].cls is OrderedRouter

    # without FOCA's metadata (source checkouts)
    def missing(name: str) -> None:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(loader, "distribution", missing)
    loaded = load_plugins(PluginsConfig(), api="tes", find_entry_points=lambda: [])
    assert loaded["foca.routing.ordered"].distribution is None
    monkeypatch.undo()
    foreign: list[Any] = [
        FakeEntryPoint("foca.routing.ordered", OrderedRouter),
        FakeEntryPoint(
            "foca.routing.ordered", OrderedRouter, dist=FakeDistribution(name="foca")
        ),
    ]
    # all matches are checked, whatever their order
    for entry_points in (foreign, foreign[::-1], [own, foreign[0]]):
        with pytest.raises(ValueError, match="built into FOCA"):
            load_plugins(
                PluginsConfig(),
                api="tes",
                find_entry_points=functools.partial(list, entry_points),
            )


def test_client_seed_refused() -> None:
    """Clients cannot set a seed unless allowed."""
    host = make_host([{"plugin": "test.tag"}], client_pipelines={"enabled": True})
    pipeline = '[{"plugin": "foca.routing.ordered", "params": {"seed": 1}}]'
    with pytest.raises(InvalidSelection, match="'seed' is not allowed"):
        host.start(inbound({"tags": {"gateway.pipeline": pipeline}}), PRINCIPAL)
    host.close()


def test_snapshots_recorded() -> None:
    """Registry read times of the candidates are recorded."""
    host = make_host([{"plugin": "foca.routing.ordered"}])
    run = host.start(inbound(), PRINCIPAL)
    snapshots = {"reg": "2026-10-25T09:00:00.000Z", "other": None}
    run.route(CANDIDATES, snapshots=snapshots)
    assert run.provenance()["route"]["snapshots"] == snapshots
    host.close()


def test_check_params_bugs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Errors of `check_params` other than `InvalidParams` are plugin errors."""

    def check_params(self: Any, params: Any) -> None:
        raise ValueError("bug")

    monkeypatch.setattr(OrderedRouter, "check_params", check_params)
    with pytest.raises(PluginError, match="failed to check params"):
        make_host([{"plugin": "foca.routing.ordered"}])
    assert "Traceback" in caplog.text


@pytest.mark.parametrize(
    "error",
    [SystemExit(1), ValidationError.from_exception_data("RouterParams", [])],
)
def test_check_params_errors_are_plugin_errors(
    error: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SystemExit` and validation errors of `check_params` are plugin errors."""

    def check_params(self: Any, params: Any) -> None:
        raise error

    monkeypatch.setattr(OrderedRouter, "check_params", check_params)
    with pytest.raises(PluginError, match="failed to check params"):
        make_host([{"plugin": "foca.routing.ordered"}])


def test_check_params_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """`KeyboardInterrupt` in `check_params` is not caught."""

    def check_params(self: Any, params: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(OrderedRouter, "check_params", check_params)
    with pytest.raises(KeyboardInterrupt):
        make_host([{"plugin": "foca.routing.ordered"}])


def test_disabled_builtin_claimed() -> None:
    """Other distributions must not claim built-ins, even disabled ones."""
    conf = PluginsConfig.model_validate(
        {"available": {"foca.routing.ordered": {"enabled": False}}, "pipeline": []}
    )
    foreign = [FakeEntryPoint("foca.routing.ordered", OrderedRouter)]
    with pytest.raises(ValueError, match="built into FOCA"):
        load_plugins(conf, api="tes", find_entry_points=lambda: foreign)
