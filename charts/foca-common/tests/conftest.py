"""Fixtures for the tests of the library chart foca-common."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from chart_helpers import (
    HELM,
    Docs,
    build_chart,
    index,
    template,
    template_error,
)


@pytest.fixture(scope="session")
def chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy of the app chart with its dependency built."""
    if HELM is None:
        pytest.skip("helm not installed")
    return build_chart(tmp_path_factory.mktemp("chart"))


@pytest.fixture
def render(chart: Path) -> Callable[..., Docs]:
    """Render the app chart and index the manifests by kind and name."""

    def _render(*args: str, required: bool = True) -> Docs:
        return index(template(chart, *args, required=required))

    return _render


@pytest.fixture
def render_error(chart: Path) -> Callable[..., str]:
    """Render the app chart, expecting Helm to fail; return its error."""

    def _render_error(*args: str, required: bool = True) -> str:
        return template_error(chart, *args, required=required)

    return _render_error


@pytest.fixture
def chart_with_settings(
    tmp_path: Path,
) -> Iterator[Callable[[str], Path]]:
    """Build copies of the app chart with other settings of the app."""
    if HELM is None:
        pytest.skip("helm not installed")
    count = 0

    def _build(settings: str) -> Path:
        nonlocal count
        count += 1
        directory = tmp_path / str(count)
        directory.mkdir()
        return build_chart(directory, settings)

    yield _build
