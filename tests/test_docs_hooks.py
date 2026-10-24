"""Tests of the MkDocs hooks (`docs/hooks.py`)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

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
