"""MkDocs hooks: the release version and the chart version in the docs.

Versions are not hard-coded in many places of the docs, but written as
placeholders (see "Versioning and releases" in `docs/contributing/index.md`),
which the hook replaces, also in code blocks:

- `{{ version }}` (`VARIABLE`), e.g., in commands such as
  `docker pull elixircloud/foca:{{ version }}`: the current release version,
  the `version` of `CITATION.cff` (the latest release, or the upcoming one,
  once its citation metadata is set; `UNRELEASED` without one).
- `{{ chart_version }}` (`CHART_VARIABLE`), e.g., in the dependency of an
  app chart: the version of the Helm library chart `foca-common`, which is
  versioned independently of FOCA, the `version` of
  `charts/foca-common/Chart.yaml` (`CHART`; the latest chart release, or the
  upcoming one, once the chart changed since).

Only these exact forms are replaced; other `{{ ... }}`, e.g., of Helm
templates in `docs/helm-chart.md` or of `docker buildx imagetools inspect
--format`, are left as they are (which is why the docs don't use
mkdocs-macros, whose Jinja would process them). Snippets (`--8<--` sections
of `README.md`) are included after the hook ran, so they can't use them.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Protocol

import yaml

CITATION = Path("CITATION.cff")
# Placeholder of the release version in pages
VARIABLE = "{{ version }}"
# Version that the docs show if `CITATION.cff` has no `version`
UNRELEASED = "X.Y.Z"
CHART = Path("charts/foca-common/Chart.yaml")
# Placeholder of the version of the library chart in pages
CHART_VARIABLE = "{{ chart_version }}"
# Versions of the library chart: final versions only (as released)
CHART_VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}")


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


def chart_version(chart: str) -> str:
    """Return the version of the library chart.

    Args:
        chart: Content of the chart's `Chart.yaml`.

    Returns:
        The chart's `version`, e.g., `1.2.3`.

    Raises:
        ValueError: If the chart has no `version`, or it is not a final
            version (`X.Y.Z`), as only those are released.
    """
    metadata = yaml.safe_load(chart)
    version = metadata.get("version") if isinstance(metadata, dict) else None
    if not isinstance(version, str) or not CHART_VERSION.fullmatch(version):
        raise ValueError(
            f"The version of the chart ({version!r} in {CHART}) is not a final"
            " version (X.Y.Z)"
        )
    return version


def substitute_chart_version(markdown: str, version: str) -> str:
    """Replace the placeholder of the version of the library chart.

    Args:
        markdown: Markdown of a page.
        version: The chart version, e.g., `1.2.3`.

    Returns:
        The Markdown, with `version` in place of `CHART_VARIABLE`.
    """
    return markdown.replace(CHART_VARIABLE, version)


def on_page_markdown(markdown: str, page: object, config: Config, files: object) -> str:
    """Replace the release and chart versions in a page.

    Args:
        markdown: Markdown of the page.
        page: The page (`mkdocs.structure.pages.Page`); unused.
        config: Settings of MkDocs.
        files: Files of the site (`mkdocs.structure.files.Files`); unused.

    Returns:
        The Markdown, with the release and chart versions in place of their
        placeholders.

    Raises:
        ValueError: If the version in `CITATION.cff` or in the chart's
            `Chart.yaml` is invalid.
    """
    root = Path(config.config_file_path).parent
    # Pages without a placeholder need no version (the file isn't read)
    if VARIABLE in markdown:
        version = citation.release_version(
            (root / CITATION).read_text(encoding="utf-8")
        )
        markdown = substitute_version(markdown, version)
    if CHART_VARIABLE in markdown:
        markdown = substitute_chart_version(
            markdown, chart_version((root / CHART).read_text(encoding="utf-8"))
        )
    return markdown
