"""Tests of the default router (`foca.plugins.routing`) and built-in plugins."""

import functools
from collections import Counter
from importlib.metadata import entry_points
from typing import Any

import pytest

from foca.plugins import Backend
from foca.plugins.config import PluginsConfig
from foca.plugins.engine import PluginHost
from foca.plugins.loader import load_plugins
from foca.plugins.routing import OrderedRouter
from tests.plugins.fakes import FakeDistribution, FakeEntryPoint, make_host
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


def route(params: dict[str, Any] | None = None, **kwargs: Any) -> tuple[list[str], Any]:
    """Route the candidates with the router.

    Args:
        params: Parameters of the router.
        kwargs: Other arguments of `make_host`.

    Returns:
        Identifiers of the routed candidates, and the run.
    """
    host = make_host(
        [{"plugin": "foca.routing.ordered", "params": params or {}}], **kwargs
    )
    run = host.start(inbound(), PRINCIPAL)
    routed = run.route(CANDIDATES)
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
    ids, run = route({"labels": {"region": "us"}, "seed": None})
    assert sorted(ids) == ["r0", "r2"]
    ids, run = route({"labels": {"nothing": "x"}})
    assert ids == []
    assert "shuffled_groups" not in run.executions[0].annotations


def test_appended_to_client_pipelines() -> None:
    """Pipelines of clients without a route step get the router."""
    host = make_host([{"plugin": "test.tag"}])
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


def test_builtin_entry_points() -> None:
    """Built-ins load from FOCA's entry point, never from other distributions."""
    installed = list(entry_points(group="foca.plugins"))
    assert "foca.routing.ordered" in [ep.name for ep in installed]
    loaded = load_plugins(
        PluginsConfig(), api="tes", find_entry_points=lambda: installed
    )
    plugin = loaded["foca.routing.ordered"]
    assert plugin.cls is OrderedRouter
    assert plugin.distribution == "foca"
    assert plugin.entry_point == "foca.plugins.routing:OrderedRouter"
    # without FOCA's metadata, from the object reference
    loaded = load_plugins(PluginsConfig(), api="tes", find_entry_points=lambda: [])
    assert loaded["foca.routing.ordered"].cls is OrderedRouter
    assert loaded["foca.routing.ordered"].distribution is None
    foreign: list[Any] = [
        FakeEntryPoint("foca.routing.ordered", OrderedRouter),
        FakeEntryPoint(
            "foca.routing.ordered", OrderedRouter, dist=FakeDistribution(name="foca")
        ),
    ]
    for entry_point in foreign:
        with pytest.raises(ValueError, match="built into FOCA"):
            load_plugins(
                PluginsConfig(),
                api="tes",
                find_entry_points=functools.partial(list, [entry_point]),
            )
