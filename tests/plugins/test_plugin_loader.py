"""Tests of the loading of plugins (`foca.plugins.loader`)."""

import hashlib
from typing import Any

import pytest

from foca.plugins.config import PluginsConfig
from foca.plugins.loader import check_legacy_keys, load_plugins
from tests.plugins.fakes import (
    FakeDistribution,
    FakeEntryPoint,
    TagPlugin,
)


def conf(available: dict[str, Any]) -> PluginsConfig:
    """Build a plugin configuration.

    Args:
        available: Allowlist.

    Returns:
        Configuration.
    """
    return PluginsConfig.model_validate({"available": available})


def test_loads_allowlisted_plugins_only() -> None:
    """Only enabled allowlisted entry points are loaded."""
    eps = [
        FakeEntryPoint("test.tag", TagPlugin),
        FakeEntryPoint("test.other", TagPlugin),
        FakeEntryPoint("test.off", TagPlugin),
    ]
    loaded = load_plugins(
        conf(
            {
                "test.tag": {
                    "distribution": "Fake_Plugins",
                    "version": ">=1,<2",
                    "config": {"prefix": "p-"},
                },
                "test.off": {"enabled": False},
            }
        ),
        api="tes",
        find_entry_points=lambda: eps,
    )
    assert list(loaded) == ["test.tag"]
    plugin = loaded["test.tag"]
    assert plugin.instance.config.prefix == "p-"
    assert plugin.instance.id == "test.tag"
    assert plugin.distribution == "fake-plugins"
    assert plugin.version == "1.2.0"
    assert plugin.entry_point == "tests.plugins.fakes:TagPlugin"
    digest = hashlib.sha256(b"fake/__init__.py,sha256=abc,10\n").hexdigest()
    assert plugin.record_digest == f"sha256:{digest}"
    assert [ep.loaded for ep in eps] == [1, 0, 0]


def test_record_digest_missing() -> None:
    """Plugins without `RECORD` file or distribution have no digest."""
    for dist in (FakeDistribution(record=None), None):
        eps = [FakeEntryPoint("test.tag", TagPlugin, dist=dist)]
        loaded = load_plugins(
            conf({"test.tag": {}}), api="tes", find_entry_points=eps.copy
        )
        assert loaded["test.tag"].record_digest is None


@pytest.mark.parametrize(
    ("available", "eps", "message"),
    [
        ({"test.tag": {}}, [], "not installed"),
        (
            {"test.tag": {}},
            [
                FakeEntryPoint("test.tag", TagPlugin),
                FakeEntryPoint("test.tag", TagPlugin, dist=FakeDistribution("b")),
            ],
            "several entry points",
        ),
        (
            {"test.tag": {"distribution": "other"}},
            [FakeEntryPoint("test.tag", TagPlugin)],
            "not 'other'",
        ),
        (
            {"test.tag": {"distribution": "other"}},
            [FakeEntryPoint("test.tag", TagPlugin, dist=None)],
            "not 'other'",
        ),
        (
            {"test.tag": {"version": ">=2"}},
            [FakeEntryPoint("test.tag", TagPlugin)],
            "does not match",
        ),
        (
            {"test.tag": {"version": ">=2"}},
            [FakeEntryPoint("test.tag", TagPlugin, dist=None)],
            "does not match",
        ),
        (
            {"test.tag": {"config": {"other": 1}}},
            [FakeEntryPoint("test.tag", TagPlugin)],
            "Invalid config",
        ),
        ({"test.tag": {}}, [FakeEntryPoint("test.tag", dict)], "not a subclass"),
    ],
)
def test_load_errors(
    available: dict[str, Any], eps: list[FakeEntryPoint], message: str
) -> None:
    """Problems with plugins stop the app."""
    with pytest.raises(ValueError, match=message):
        load_plugins(conf(available), api="tes", find_entry_points=lambda: eps)


def test_installed_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """By default, installed entry points of the group are used."""
    from foca.plugins import loader  # noqa: PLC0415

    groups = []

    def entry_points(group: str) -> list[FakeEntryPoint]:
        groups.append(group)
        return [FakeEntryPoint("test.tag", TagPlugin)]

    monkeypatch.setattr(loader, "entry_points", entry_points)
    assert list(load_plugins(conf({"test.tag": {}}), api="tes")) == ["test.tag"]
    assert groups == ["foca.plugins"]


@pytest.mark.parametrize(
    ("config", "key"),
    [
        ({"middlewares": []}, "'middlewares'"),
        ({"custom": {"middlewares": [["x"]]}}, "'custom.middlewares'"),
    ],
)
def test_legacy_keys(config: dict[str, Any], key: str) -> None:
    """Keys of earlier middleware stacks stop the app with a hint."""
    with pytest.raises(ValueError, match=key) as exc_info:
        check_legacy_keys(config)
    assert "plugins.available" in str(exc_info.value)


def test_no_legacy_keys() -> None:
    """Configurations without legacy keys pass."""
    from foca.models.config import Config  # noqa: PLC0415

    check_legacy_keys(Config.model_validate({"custom": {"other": 1}}))
    check_legacy_keys(Config())
