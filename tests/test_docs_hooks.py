"""Tests of the MkDocs hooks (`docs/hooks.py`)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

# `docs/` is not a package, so the hooks are loaded from their file
_spec = importlib.util.spec_from_file_location("docs_hooks", ROOT / "docs/hooks.py")
assert _spec and _spec.loader
hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hooks)


def _render(markdown: str, root: Path = ROOT) -> str:
    """Run the hook on the Markdown of a page.

    Args:
        markdown: Markdown of the page.
        root: Directory of the docs settings (`mkdocs.yml`) and of the
            citation file; default: the repository's.

    Returns:
        The Markdown returned by the hook.
    """
    config = SimpleNamespace(config_file_path=str(root / "mkdocs.yml"))
    # The hooks are loaded from their file, so their types are unknown
    rendered: str = hooks.on_page_markdown(
        markdown, page=None, config=config, files=None
    )
    return rendered


def _version() -> str:
    """Return the version that the docs show for the current release.

    Returns:
        The `version` of `CITATION.cff`, or `UNRELEASED` if it has none.
    """
    version = hooks.citation.release_version(
        (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    )
    # The hooks are loaded from their file, so their types are unknown
    assert version is None or isinstance(version, str)
    return version or str(hooks.UNRELEASED)


def test_mkdocs_settings() -> None:
    """The docs build runs the hooks, and doesn't publish them."""
    settings = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "\nhooks:\n  - docs/hooks.py\n" in settings
    assert "\nexclude_docs: |\n  hooks.py\n" in settings


def test_on_page_markdown_version() -> None:
    """The placeholder is the release version, also in code blocks."""
    version = _version()
    markdown = _render(
        "Version {{ version }}.\n\n```bash\ndocker pull x:{{ version }}\n```\n"
        "{{ json .SBOM }} {{version}} {{ .Values.version }}\n"
    )
    assert markdown == (
        f"Version {version}.\n\n```bash\ndocker pull x:{version}\n```\n"
        "{{ json .SBOM }} {{version}} {{ .Values.version }}\n"
    )


@pytest.mark.parametrize(
    ("citation", "expected"),
    [("cff-version: 1.2.0\n", "X.Y.Z"), ('version: "1.2.3rc1"\n', "1.2.3rc1")],
)
def test_on_page_markdown_citation(
    tmp_path: Path, citation: str, expected: str
) -> None:
    """The version is that of `CITATION.cff`; without one, `UNRELEASED`.

    Args:
        tmp_path: Directory of the docs settings and the citation file.
        citation: Content of the citation file.
        expected: Expected version.
    """
    (tmp_path / "CITATION.cff").write_text(citation, encoding="utf-8")
    assert _render("{{ version }}", tmp_path) == expected


def test_on_page_markdown_invalid_citation(tmp_path: Path) -> None:
    """An invalid version in `CITATION.cff` fails the build.

    Args:
        tmp_path: Directory of the docs settings and the citation file.
    """
    (tmp_path / "CITATION.cff").write_text('version: "1.0"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="is not a release version"):
        _render("{{ version }}", tmp_path)


def test_on_page_markdown_without_placeholder(tmp_path: Path) -> None:
    """Pages without the placeholder are left as they are, unread version.

    Args:
        tmp_path: Directory of the docs settings, without a citation file.
    """
    markdown = "# Page\n\n{{ .Values.image }}\n"
    assert _render(markdown, tmp_path) == markdown


@pytest.mark.parametrize(
    ("markdown", "version", "expected"),
    [
        ("a {{ version }} b {{ version }}", "1.2.3", "a 1.2.3 b 1.2.3"),
        ("{{ version }}-py3.12", "1.2.3rc1", "1.2.3rc1-py3.12"),
        ("no placeholder", None, "no placeholder"),
        # Without a release version, the placeholder becomes `UNRELEASED`
        ("{{ version }}", None, "X.Y.Z"),
    ],
)
def test_substitute_version(markdown: str, version: str | None, expected: str) -> None:
    """The placeholder is replaced with the version.

    Args:
        markdown: Markdown of a page.
        version: Release version.
        expected: Expected Markdown.
    """
    assert hooks.substitute_version(markdown, version) == expected


def test_docs_use_placeholder() -> None:
    """The docs take the release version from the placeholder."""
    deployment = (ROOT / "docs/deployment.md").read_text(encoding="utf-8")
    assert "elixircloud/foca:{{ version }}" in deployment
    assert f"elixircloud/foca:{_version()}" in _render(deployment)


def _chart_version() -> str:
    """Return the version of the library chart.

    Returns:
        The `version` of `charts/foca-common/Chart.yaml`.
    """
    chart = yaml.safe_load((ROOT / hooks.CHART).read_text(encoding="utf-8"))
    version = chart["version"]
    assert isinstance(version, str)
    return version


def test_on_page_markdown_chart_version() -> None:
    """The chart placeholder is the chart's version, also in code blocks."""
    version = _chart_version()
    markdown = _render(
        "Chart {{ chart_version }}.\n\n```yaml\nversion: {{ chart_version }}\n```\n"
        "{{chart_version}} {{ .Chart.Version }}\n"
    )
    assert markdown == (
        f"Chart {version}.\n\n```yaml\nversion: {version}\n```\n"
        "{{chart_version}} {{ .Chart.Version }}\n"
    )


def test_on_page_markdown_both_versions(tmp_path: Path) -> None:
    """Both placeholders are replaced, each from its own file.

    Args:
        tmp_path: Directory of the docs settings, the citation file and the
            chart.
    """
    (tmp_path / "CITATION.cff").write_text('version: "1.2.3"\n', encoding="utf-8")
    chart = tmp_path / hooks.CHART
    chart.parent.mkdir(parents=True)
    chart.write_text("name: foca-common\nversion: 0.4.5\n", encoding="utf-8")
    assert _render("{{ version }} {{ chart_version }}", tmp_path) == "1.2.3 0.4.5"


def test_on_page_markdown_chart_only(tmp_path: Path) -> None:
    """Pages with the chart placeholder only need no citation file.

    Args:
        tmp_path: Directory of the docs settings and the chart, without a
            citation file.
    """
    chart = tmp_path / hooks.CHART
    chart.parent.mkdir(parents=True)
    chart.write_text("version: 10.0.1\n", encoding="utf-8")
    assert _render("foca-common:{{ chart_version }}", tmp_path) == (
        "foca-common:10.0.1"
    )


@pytest.mark.parametrize(
    "chart",
    [
        "version: 0.2.0\n",
        'version: "1.0.0"\n',
        "name: foca-common\nversion: 12.34.56\n",
    ],
)
def test_chart_version(chart: str) -> None:
    """The chart version is the final version of `Chart.yaml`.

    Args:
        chart: Content of `Chart.yaml`.
    """
    assert hooks.chart_version(chart) == yaml.safe_load(chart)["version"]


@pytest.mark.parametrize(
    "chart",
    [
        "name: foca-common\n",
        "version: 1.0\n",
        "version: 1.2.3-rc.1\n",
        "version: 01.2.3\n",
        "version: v1.2.3\n",
        "- version: 1.2.3\n",
        "",
    ],
)
def test_chart_version_invalid(chart: str) -> None:
    """Charts without a final version fail the build.

    Args:
        chart: Content of `Chart.yaml`.
    """
    with pytest.raises(ValueError, match="is not a final version"):
        hooks.chart_version(chart)


def test_substitute_chart_version() -> None:
    """The chart placeholder is replaced with the chart version."""
    assert hooks.substitute_chart_version(
        "a {{ chart_version }} b {{ chart_version }} {{ version }}", "1.2.3"
    ) == ("a 1.2.3 b 1.2.3 {{ version }}")


def test_docs_use_chart_placeholder() -> None:
    """The docs take the current chart version from the placeholder."""
    helm_chart = (ROOT / "docs/helm-chart.md").read_text(encoding="utf-8")
    assert "    version: {{ chart_version }}\n" in helm_chart
    assert f"    version: {_chart_version()}\n" in _render(helm_chart)
    deployment = (ROOT / "docs/deployment.md").read_text(encoding="utf-8")
    assert "charts/foca-common:{{ chart_version }}" in deployment
    assert f"charts/foca-common:{_chart_version()}" in _render(deployment)
