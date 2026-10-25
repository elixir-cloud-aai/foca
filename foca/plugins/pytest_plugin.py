"""Pytest fixtures for apps with gateway plugins (needs pytest).

Not registered automatically; enable it in the app's `conftest.py`:

```python
pytest_plugins = ["foca.plugins.pytest_plugin"]
```
"""

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from foca.plugins import loader
from foca.plugins.testing import FakeDistribution, FakeEntryPoint, fake_entry_points

RegisterPlugins = Callable[..., list[FakeEntryPoint]]
"""Type of the function of the `fake_plugin_entry_points` fixture."""


@pytest.fixture
def fake_plugin_entry_points(monkeypatch: pytest.MonkeyPatch) -> RegisterPlugins:
    """Register fake plugins as installed entry points of `foca.plugins`.

    Patches the entry point lookup of :py:mod:`foca.plugins.loader` (and
    restores it after the test), so that apps load the fakes through the
    real allowlist path (`plugins.available`), without a test-only
    distribution. Entry points of other groups, and the installed plugins,
    are not listed then.

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
        `dist`, cf. :py:class:`foca.plugins.testing.FakeDistribution`),
        registers them (replacing earlier ones), and returns the entry
        points.
    """

    def register(
        plugins: Mapping[str, Any], dist: FakeDistribution | None = None
    ) -> list[FakeEntryPoint]:
        """Register fake plugins.

        Args:
            plugins: Plugin classes by identifier.
            dist: Distribution of the entry points.

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

    return register
