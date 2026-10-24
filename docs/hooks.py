"""MkDocs hooks: the release version in the docs.

Pages write the current release version as `{{ version }}` (`VARIABLE`),
e.g., in commands such as `docker pull elixircloud/foca:{{ version }}`, so
that it is not hard-coded in many places (see "Versioning and releases" in
`docs/contributing.md`): the hook replaces it with the `version` of
`CITATION.cff`, the latest release (or the upcoming one, once its citation
metadata is set; without one, the docs show `X.Y.Z` (`UNRELEASED`)), also in
code blocks. Only this exact form is replaced; other `{{ ... }}`, e.g., of
Helm templates in `docs/helm-chart.md` or of `docker buildx imagetools
inspect --format`, are left as they are (which is why the docs don't use
mkdocs-macros, whose Jinja would process them). Snippets (`--8<--` sections
of `README.md`) are included after the hook ran, so they can't use it.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Protocol

CITATION = Path("CITATION.cff")
# Placeholder of the release version in pages
VARIABLE = "{{ version }}"
# Version that the docs show if `CITATION.cff` has no `version`
UNRELEASED = "X.Y.Z"


def _load(name: str) -> ModuleType:
    """Load a script of `scripts/` (not a package) from its file.

    Args:
        name: Name of the script, without `.py`.

    Returns:
        The script, as a module.

    Raises:
        ImportError: If the script can't be loaded.
    """
    path = Path(__file__).resolve().parents[1] / f"scripts/{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} can't be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


citation = _load("citation")


class Config(Protocol):
    """Settings of MkDocs that the hooks use (of `MkDocsConfig`).

    A protocol, so that the hooks are type-checked without MkDocs, which only
    the `docs` extra installs.

    Attributes:
        config_file_path: Path of `mkdocs.yml`.
    """

    config_file_path: str


def substitute_version(markdown: str, version: str | None) -> str:
    """Replace the placeholder of the release version.

    Args:
        markdown: Markdown of a page.
        version: The release version, e.g., `1.2.3`; `None` if there is none.

    Returns:
        The Markdown, with `version` (or `UNRELEASED`, if it is `None`) in
        place of `VARIABLE`.
    """
    return markdown.replace(VARIABLE, version or UNRELEASED)


def on_page_markdown(markdown: str, page: object, config: Config, files: object) -> str:
    """Replace the release version in a page.

    Args:
        markdown: Markdown of the page.
        page: The page (`mkdocs.structure.pages.Page`); unused.
        config: Settings of MkDocs.
        files: Files of the site (`mkdocs.structure.files.Files`); unused.

    Returns:
        The Markdown, with the release version in place of its placeholder.

    Raises:
        ValueError: If the version in `CITATION.cff` is invalid.
    """
    # Pages without the placeholder need no version (`CITATION.cff` isn't read)
    if VARIABLE not in markdown:
        return markdown
    root = Path(config.config_file_path).parent
    version = citation.release_version((root / CITATION).read_text(encoding="utf-8"))
    return substitute_version(markdown, version)
