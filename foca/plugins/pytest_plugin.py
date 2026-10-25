"""Pytest fixtures for gateway plugins and their apps (needs pytest).

Not registered automatically; enable it in `conftest.py`:

```python
pytest_plugins = ["foca.plugins.pytest_plugin"]
```

Fixtures:

- `fake_plugin_entry_points`: registers fake plugins as installed entry
  points, for app tests.
- `plugin_contract`: checks plugins against the plugin contract (cf.
  :py:func:`foca.plugins.testing.check_plugin_contract`), for the tests
  of plugin packages.
"""

import functools
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from foca.plugins import loader
from foca.plugins.testing import (
    FakeDistribution,
    FakeEntryPoint,
    check_plugin_contract,
    fake_entry_points,
)

RegisterPlugins = Callable[..., list[FakeEntryPoint]]
"""Type of the function of the `fake_plugin_entry_points` fixture."""

CheckContract = Callable[..., None]
"""Type of the function of the `plugin_contract` fixture."""


def register_fake_plugins(
    monkeypatch: pytest.MonkeyPatch,
    plugins: Mapping[str, Any],
    dist: FakeDistribution | None = None,
) -> list[FakeEntryPoint]:
    """Register fake plugins as installed entry points of `foca.plugins`.

    Patches the entry point lookup of :py:mod:`foca.plugins.loader` with
    `monkeypatch`, which restores it. Entry points of other groups, and the
    installed plugins, are not listed then.

    Args:
        monkeypatch: Monkeypatch, e.g., the pytest fixture or
            `pytest.MonkeyPatch.context()`.
        plugins: Plugin classes by identifier; replace earlier ones.
        dist: Distribution of the entry points (cf.
            :py:class:`foca.plugins.testing.FakeDistribution`).

    Returns:
        Entry points.
    """
    find = fake_entry_points(plugins, dist=dist)

    def entry_points(**kwargs: Any) -> list[FakeEntryPoint]:
        """List the fake entry points of a group.

        Args:
            kwargs: Arguments of `importlib.metadata.entry_points`.

        Returns:
            The fakes for the group `foca.plugins`, else none.
        """
        group = kwargs.get("group")
        return find() if group in (None, loader.ENTRY_POINT_GROUP) else []

    monkeypatch.setattr(loader, "entry_points", entry_points)
    return find()


@pytest.fixture
def fake_plugin_entry_points(monkeypatch: pytest.MonkeyPatch) -> RegisterPlugins:
    """Register fake plugins as installed entry points, for one test.

    Apps then load the fakes through the real allowlist path
    (`plugins.available`), without a test-only distribution (cf.
    :py:func:`register_fake_plugins`).

    Examples:
        ```python
        def test_app(fake_plugin_entry_points):
            fake_plugin_entry_points({"org.example.tag": TagPlugin})
            app = create_app("config.yaml")  # loads org.example.tag
        ```

    Args:
        monkeypatch: Pytest fixture.

    Returns:
        Function that takes plugin classes by identifier (and optionally a
        `dist`), registers them, and returns the entry points.
    """
    return functools.partial(register_fake_plugins, monkeypatch)


@pytest.fixture
def plugin_contract() -> CheckContract:
    """Check plugins against the plugin contract.

    Examples:
        ```python
        def test_contract(plugin_contract):
            plugin_contract(AllowedImages, config={"registries": ["quay.io"]})
        ```

    Returns:
        :py:func:`foca.plugins.testing.check_plugin_contract`, which raises
        a :py:class:`foca.plugins.testing.ContractViolation` listing the
        problems.
    """
    return check_plugin_contract
