"""Tests that the current release version is not hard-coded (drift guard).

The release version has one source (see "Versioning and releases" in
`docs/contributing.md`): the release tag and, on `dev`, the `version` of
`CITATION.cff`. The docs write it as `{{ version }}` (replaced by
`docs/hooks.py`); comments, templates and tests use placeholders (e.g.,
`1.2.3`) and refer to sections by anchor. So the current release version may
only appear in the files and lines of the allowlist below, which are about
that release itself, not about the latest one.
"""

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location("citation", ROOT / "scripts/citation.py")
assert _spec and _spec.loader
citation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(citation)

# Files that may name the release version: its source, the changelog, and
# the tests of the release tooling, whose versions are test data (if one is
# the release version, it means no other)
ALLOWED_FILES = (
    "CITATION.cff",
    "docs/changelog.md",
    "tests/test_citation.py",
    "tests/test_docs_hooks.py",
    "tests/test_release_assets.py",
    "tests/test_version_mentions.py",
)
# Files that may name it by their path: the migration guides, which are
# about their release (`docs/migration-X.Y.md`)
ALLOWED_PATHS = re.compile(r"docs/migration-[0-9]+\.[0-9]+\.md")
# Mentions of the version that refer to a release of FOCA (`{version}` is the
# escaped version, `{end}` the end of a version): the tag, the image and
# package, the version in prose or in inline code. Other mentions of the same
# number, e.g., the version of the library chart or of a dependency, are not
# about FOCA's release
MENTIONS = (
    r"(?:^|[\s`(\"'\[]|refs/tags/|releases/(?:tag|download)/|\.\.\."
    r"|(?i:foca)/(?:blob|tree|compare|commits)/|(?i:foca)/\S*@)v{version}{end}",
    # Read the Docs versions (`/en/1.2.3/`, `/en/v1.2.3/`)
    r"/en/v?{version}{end}",
    # The image, the package (also with extras and any version specifier,
    # and on PyPI, `project/foca/1.2.3/`) and FOCA in prose
    r"(?i:\bfoca)(?:\[[^\]]*\])?\s*(?:\(?\s*[<>=~!]=?=?|[:\-@/(])?\s*"
    r"v?{version}{end}",
    r"(?<![0-9.]){version}{end} releases?\b",
    r"(?i:\b(?:release|version)s?)\s+v?{version}{end}",
    r"`v?{version}`",
    # Tags of the images of other Python versions than the default one
    r"(?<![0-9.]){version}-py[0-9]",
    r"(?<![0-9.]){version}{end} (?:on|or later|and later)\b",
    r"(?i:\bsince)\s+v?{version}{end}",
    r"(?:\btag|appVersion|_VERSION)[\"']?\s*[:=]\s*[\"']?v?{version}{end}",
)
# Parts of lines that may name it (the rest of the line is still checked):
# links to the notes and assets of the release on GitHub, links to and
# headings of its migration guide (also in `mkdocs.yml`, `docs/index.md` and
# `README.md`), and statements of what applies from that release on, which
# are about that release itself
ALLOWED_LINES = (
    r"releases/(?:tag|download)/v{version}\S*",
    r"Migrating to \[?{version}{end}\]?",
    r"\[{version}\]\(migration-[0-9.]+\.md[^)]*\)",
    r"\bFrom FOCA {version} on\b",
    # Comments of actions pinned to commits (`uses: x/y@<sha> # v1.2.3`),
    # which name the versions of the actions
    r"@[0-9a-f]{{40}} # v{version}\S*",
    # Revisions of the pre-commit hooks (`rev: v1.2.3`), never FOCA's
    r"^\s*rev: v?{version}\S*$",
)


def mentions(text: str, version: str) -> list[tuple[int, str]]:
    """Return the lines that mention a release version, unless allowed.

    Args:
        text: Content of a file.
        version: Release version, e.g., `1.2.3`.

    Returns:
        Number (from 1) and text of each line that mentions the version as a
        release of FOCA (`MENTIONS`; not as part of another version, e.g.,
        `11.2.3`, `1.2.30` or `1.2.3rc1`), outside the parts that
        `ALLOWED_LINES` allow.
    """
    fields = {"version": re.escape(version), "end": r"(?!\.?[0-9]|(?:a|b|rc)[0-9])"}
    found = [re.compile(pattern.format(**fields)) for pattern in MENTIONS]
    allowed = [re.compile(pattern.format(**fields)) for pattern in ALLOWED_LINES]
    result = []
    for number, line in enumerate(text.splitlines(), start=1):
        checked = line
        for pattern in allowed:
            checked = pattern.sub(" ", checked)
        if any(pattern.search(checked) for pattern in found):
            result.append((number, line))
    return result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("docker pull foca:1.2.3", [(1, "docker pull foca:1.2.3")]),
        ("elixircloud/foca:1.2.3-py3.12", [(1, "elixircloud/foca:1.2.3-py3.12")]),
        ("a\n--source-ref refs/tags/v1.2.3", [(2, "--source-ref refs/tags/v1.2.3")]),
        ("Release 1.2.3.", [(1, "Release 1.2.3.")]),
        ("since version 1.2.3,", [(1, "since version 1.2.3,")]),
        ("FOCA 1.2.3", [(1, "FOCA 1.2.3")]),
        ("foca-1.2.3.tar.gz", [(1, "foca-1.2.3.tar.gz")]),
        ("foca==1.2.3 foca@1.2.3", [(1, "foca==1.2.3 foca@1.2.3")]),
        ("foca>=1.2.3", [(1, "foca>=1.2.3")]),
        ("foca~=1.2.3", [(1, "foca~=1.2.3")]),
        ("foca >= 1.2.3", [(1, "foca >= 1.2.3")]),
        ("foca[docs] (>=1.2.3)", [(1, "foca[docs] (>=1.2.3)")]),
        ("foca[docs]==1.2.3", [(1, "foca[docs]==1.2.3")]),
        ("pypi.org/project/foca/1.2.3/", [(1, "pypi.org/project/foca/1.2.3/")]),
        ("foca.readthedocs.io/en/1.2.3/", [(1, "foca.readthedocs.io/en/1.2.3/")]),
        ("FOCA (1.2.3)", [(1, "FOCA (1.2.3)")]),
        ("[v1.2.3](x)", [(1, "[v1.2.3](x)")]),
        (
            "foca/.github/workflows/x.yml@v1.2.3",
            [(1, "foca/.github/workflows/x.yml@v1.2.3")],
        ),
        ("the 1.2.3 release", [(1, "the 1.2.3 release")]),
        ("(e.g., `1.2.3`)", [(1, "(e.g., `1.2.3`)")]),
        ("e.g., `1.2.3-py3.12`", [(1, "e.g., `1.2.3-py3.12`")]),
        ("from 1.2.3 on", [(1, "from 1.2.3 on")]),
        ("1.2.3 or later", [(1, "1.2.3 or later")]),
        ("/en/v1.2.3/", [(1, "/en/v1.2.3/")]),
        ("Tag v1.2.3", [(1, "Tag v1.2.3")]),
        ("  tag: 1.2.3", [(1, "  tag: 1.2.3")]),
        ('appVersion: "1.2.3"', [(1, 'appVersion: "1.2.3"')]),
        ("X_VERSION=1.2.3", [(1, "X_VERSION=1.2.3")]),
        ("Since 1.2.3, x", [(1, "Since 1.2.3, x")]),
        ("x/foca/tree/v1.2.3/a", [(1, "x/foca/tree/v1.2.3/a")]),
        ("x/compare/v1.2.2...v1.2.3", [(1, "x/compare/v1.2.2...v1.2.3")]),
        # Other versions, and the number not as a release of FOCA
        ("v11.2.3 v1.2.30 v1.2.3.4 foca:11.2.3 foca-1.2.3rc1.tar.gz", []),
        ("foca-common:1.2.3 refs/tags/chart-v1.2.3 x.org/spec/v1.2.3/schema", []),
        ("version: 1.2.3", []),
        ("openapi: 1.2.3", []),
        ("Until 1.2.3, all releases are pre-releases", []),
        ("{{ version }}", []),
        # Allowed lines
        ("x/foca/releases/tag/v1.2.3", []),
        ("x/releases/download/v1.2.3/foca-1.2.3.tar.gz", []),
        ("  - Migrating to 1.2.3: migration-1.2.md", []),
        ("See [Migrating to 1.2.3](migration-1.2.md).", []),
        ("- Migrating to [1.2.3](migration-1.2.md), [1.2.2](migration-1.1.md)", []),
        ("From FOCA 1.2.3 on, images are signed", []),
        (f"uses: x/y@{'0' * 40} # v1.2.3", []),
        ("    rev: v1.2.3", []),
        # Only the allowed part of a line is skipped
        (
            "From FOCA 1.2.3 on, release 1.2.3 is",
            [(1, "From FOCA 1.2.3 on, release 1.2.3 is")],
        ),
    ],
)
def test_mentions(text: str, expected: list[tuple[int, str]]) -> None:
    """Lines naming the version are found, unless they are allowed.

    Args:
        text: Content of a file.
        expected: Expected lines.
    """
    assert mentions(text, "1.2.3") == expected


def test_mentions_pre_release() -> None:
    """Pre-releases are found, but not as part of other pre-releases."""
    text = "foca:1.2.3rc1\nTag v1.2.3rc1\nfoca:1.2.3rc10"
    assert mentions(text, "1.2.3rc1") == [(1, "foca:1.2.3rc1"), (2, "Tag v1.2.3rc1")]


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("docs/migration-1.2.md", True),
        ("docs/migration-1.2.md.bak", False),
        ("docs/migration-notes.md", False),
        ("docs/sub/docs/migration-1.2.md", False),
    ],
)
def test_allowed_paths(name: str, allowed: bool) -> None:
    """Only the migration guides themselves are allowed by their path.

    Args:
        name: Path of a file, relative to the repository root.
        allowed: Whether the file may name the release version.
    """
    assert (ALLOWED_PATHS.fullmatch(name) is not None) == allowed


def _tracked_files() -> list[str]:
    """Return the files tracked by Git, relative to the repository root.

    Returns:
        The paths, as `git ls-files` lists them.
    """
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("needs a Git checkout")
    listed = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [git, "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    )
    return [name for name in listed.stdout.decode().split("\0") if name]


def test_release_version_not_hard_coded() -> None:
    """The current release version appears only where the allowlist allows."""
    version = citation.release_version(
        (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    )
    if version is None:
        pytest.skip("CITATION.cff has no version (no release yet)")
    found = []
    for name in _tracked_files():
        if name in ALLOWED_FILES or ALLOWED_PATHS.fullmatch(name):
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found += [
            f"{name}:{number}: {line}" for number, line in mentions(text, version)
        ]
    assert found == [], (
        f"The release version {version} is hard-coded; use {{{{ version }}}} in"
        " the docs, placeholders (e.g., 1.2.3) elsewhere, or extend the"
        " allowlist of this test if the line is about that release itself:\n"
        + "\n".join(found)
    )
