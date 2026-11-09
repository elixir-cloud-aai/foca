"""Tests of the release notes check and body (`scripts/release_notes.py`)."""

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release_notes.py"

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
assert _spec and _spec.loader
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)

CURATED = """\
The release brings a feature. See the [upgrade
guide](https://example.org/upgrading/).

## Breaking changes

- A breaking change, described
  over two lines (#1, #2)
- Another one, with a lazy
continuation line (#3; #4, #5)

## Features

- A feature with an example (#6, #7):

  ```yaml
  key: value
  ## not a heading
  ```

  And more text about it (#8)
* A bullet with another marker (#9)

## Maintenance

1. A numbered item (#10)
"""

# As GitHub's `generate-notes` API returns them (`body`)
GENERATED = """\
## What's Changed
* feat: add a feature by @someone in https://github.com/o/r/pull/7
* fix: fix a bug by @newcomer in https://github.com/o/r/pull/11

## New Contributors
* @newcomer made their first contribution in https://github.com/o/r/pull/11

**Full Changelog**: https://github.com/o/r/compare/v0.2.0...v0.3.0"""


def _messages(markdown: str) -> list[tuple[int, str]]:
    """Return the line and the start of the message of each finding.

    Args:
        markdown: Curated release notes.

    Returns:
        Line and first three words of the message of each finding.
    """
    return [
        (finding.line, " ".join(finding.message.split()[:3]))
        for finding in release_notes.check(markdown)
    ]


@pytest.mark.parametrize(
    "markdown",
    [
        CURATED,
        # The intro alone, and sections with closing hashes
        "A fix release.\n",
        "Intro.\n\n## Fixes ##\n\n- Fix (#1)\n",
        # Code blocks in the intro, headings in code blocks
        "Intro.\n\n```text\n## Unknown\n```\n\n## Fixes\n\n- Fix (#1)\n",
        # Fences close only with the same character, at least as long, and
        # nothing else on the line
        "Intro.\n\n## Fixes\n\n- Fix (#1):\n\n  ````md\n  ```\n  ## No\n  ```\n"
        "  ````\n\n  More (#2)\n",
        "Intro.\n\n## Fixes\n\n- Fix (#1):\n\n  ```\n  ~~~\n  ``` x\n  ```\n\n"
        "  More (#2)\n",
        # HTML comments, and a byte order mark
        "<!-- Notes of 0.3.0 -->\nIntro.\n\n## Fixes\n\n<!-- comment -->\n- Fix (#1)\n",
        "\ufeffIntro.\n\n## Fixes\n\n- Fix (#1)\n",
        "Intro.\n\n## Fixes\n\n- Fix (#1)\n    <!-- c -->\n- Fix (#2)\n<!-- c -->\n",
        "Intro.\n\n## Fixes\n\n- Fix (#1)\n\n<!-- c -->\n\n- Fix (#2)\n",
        # Code blocks may show the generated parts
        "Intro.\n\n```md\n**Full Changelog**: https://x/compare/a...b\n```\n",
        # Numbers wrapped over lines
        "Intro.\n\n## Fixes\n\n- Fix (#1,\n  #2; #3,\n  #4)\n",
        # Hashes that are no headings
        "Intro.\n\n## Fixes\n\n- Fix of\n#12 (#13)\n",
    ],
)
def test_check_valid(markdown: str) -> None:
    """Notes in the agreed format have no findings."""
    assert release_notes.check(markdown) == []


@pytest.mark.parametrize(
    ("markdown", "messages"),
    [
        # No intro
        ("", [(1, "No intro paragraph")]),
        ("<!-- comment -->\n", [(1, "No intro paragraph")]),
        # Lists and code blocks are no intro paragraph
        ("- Item\n\n## Fixes\n\n- Fix (#1)\n", [(3, "No intro paragraph")]),
        ("```\ncode\n```\n\n## Fixes\n\n- Fix (#1)\n", [(5, "No intro paragraph")]),
        (
            "<!-- a --> Intro <!-- b -->\n\n## Fixes\n\n- Fix (#1)\n",
            [],
        ),
        (
            "<!-- comment -->\n## Fixes\n\n- Fix (#1)\n",
            [(2, "No intro paragraph")],
        ),
        ("\ufeff## Fixes\n\n- Fix (#1)\n", [(1, "No intro paragraph")]),
        ("\n\n", [(2, "No intro paragraph")]),
        (
            "## Fixes\n\n- Fix (#1)\n",
            [(1, "No intro paragraph")],
        ),
        # Other headings
        (
            "# FOCA 0.3.0\n\nIntro.\n",
            [(1, "Heading of level"), (1, "No intro paragraph")],
        ),
        (
            "Intro.\n\n## Fixes\n\n### API\n\n- Fix (#1)\n",
            [(3, "Empty section"), (5, "Heading of level")],
        ),
        (
            "Intro.\n\n## New features\n\n- Feature (#1)\n",
            [(3, "Unknown section 'New")],
        ),
        (
            "Intro.\n\n#\n",
            [(3, "Heading of level")],
        ),
        # Setext headings and thematic breaks
        ("Intro\n===\n", [(2, "Setext heading or")]),
        ("Intro.\n\n## Fixes\n\n- Fix (#1)\n\n---\n", [(7, "Setext heading or")]),
        ("Intro.\n\n***\n", [(3, "Setext heading or")]),
        # Order, duplicates and empty sections
        (
            "Intro.\n\n## Fixes\n\n- Fix (#1)\n\n## Features\n\n- Feature (#2)\n",
            [(7, "Section 'Features' after")],
        ),
        (
            "Intro.\n\n## Fixes\n\n- Fix (#1)\n\n## Fixes\n\n- Fix (#2)\n",
            [(7, "Section 'Fixes' after")],
        ),
        (
            "Intro.\n\n## Fixes\n\n## Maintenance\n\n- Chore (#1)\n",
            [(3, "Empty section")],
        ),
        ("Intro.\n\n## Fixes\n", [(3, "Empty section")]),
        # Parts that the release workflow adds
        (
            "Intro.\n\n## Commits\n\n* fix by @a in https://x/pull/1\n",
            [(3, "Section 'Commits': the")],
        ),
        (
            "Intro.\n\n**Full Changelog**: https://x/compare/a...b\n",
            [(3, "'**Full Changelog**' line:")],
        ),
        # Sections hold bullets only
        (
            "Intro.\n\n## Fixes\n\n- Fix (#1)\n\nA paragraph.\n",
            [(7, "Sections hold bullets")],
        ),
        (
            "Intro.\n\n## Fixes\n\nA paragraph.\n\n- Fix (#1)\n",
            [(5, "Sections hold bullets")],
        ),
        (
            "Intro.\n\n## Fixes\n\n```\ncode\n```\n\n- Fix (#1)\n",
            [(5, "Sections hold bullets")],
        ),
        # Bullets end with their issue and pull request numbers
        (
            "Intro.\n\n## Fixes\n\n- Fix\n- Fix (#1).\n- Fix (#1) more\n"
            "- Fix (1, 2)\n- Fix (#a)\n- Fix #1\n",
            [
                (5, "Bullet doesn't end"),
                (6, "Bullet doesn't end"),
                (7, "Bullet doesn't end"),
                (8, "Bullet doesn't end"),
                (9, "Bullet doesn't end"),
                (10, "Bullet doesn't end"),
            ],
        ),
        (
            "Intro.\n\n## Fixes\n\n- Fix (#1):\n\n  More text.\n",
            [(7, "Bullet doesn't end")],
        ),
        # Unclosed code blocks
        (
            "Intro.\n\n## Fixes\n\n- Fix (#1)\n\n  ```\n  code\n",
            [(8, "Bullet doesn't end"), (8, "Unclosed fenced code")],
        ),
    ],
)
def test_check_invalid(markdown: str, messages: list[tuple[int, str]]) -> None:
    """Notes that differ from the agreed format have findings."""
    assert _messages(markdown) == messages


@pytest.mark.parametrize("notes", sorted((ROOT / "docs/releases").glob("**/*.md")))
def test_curated_notes(notes: Path) -> None:
    """The curated notes in the repository are in the agreed format."""
    assert release_notes.check(notes.read_text(encoding="utf-8")) == []


def test_body() -> None:
    """The body is the curated notes, then the generated ones, without header."""
    text, warnings = release_notes.body(CURATED, GENERATED)
    changes = GENERATED.removeprefix("## What's Changed\n")
    assert text == f"{CURATED.strip()}\n\n## Commits\n\n{changes}\n"
    assert text.endswith(
        "\n\n**Full Changelog**: https://github.com/o/r/compare/v0.2.0...v0.3.0\n"
    )
    assert "What's Changed" not in text
    assert warnings == []


def test_body_comment() -> None:
    """The header is also dropped after a comment (e.g., of a configuration)."""
    generated = f"<!-- Release notes generated using configuration -->\n\n{GENERATED}"
    text, warnings = release_notes.body("Intro.\n", generated)
    assert text.startswith(
        "Intro.\n\n## Commits\n\n<!-- Release notes generated using configuration"
        " -->\n\n* feat: add a feature"
    )
    assert "What's Changed" not in text
    assert warnings == []


def test_body_no_changes() -> None:
    """Without changes in the generated notes, the body has no `Commits`."""
    generated = "**Full Changelog**: https://github.com/o/r/commits/v0.3.0\n"
    assert release_notes.body("Intro.\n", generated) == (
        "Intro.\n\n**Full Changelog**: https://github.com/o/r/commits/v0.3.0\n",
        [],
    )


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_body_crlf(newline: str) -> None:
    """Generated notes with CRLF or CR line endings are normalized.

    Args:
        newline: Line ending of the generated notes.
    """
    generated = GENERATED.replace("\n", newline) + newline
    assert release_notes.body(CURATED, generated) == release_notes.body(
        CURATED, GENERATED
    )


@pytest.mark.parametrize(
    ("generated", "commits"),
    [
        ("", ""),
        (
            "## What's Changed\n* fix by @a in https://x/pull/1\n",
            "\n\n## Commits\n\n* fix by @a in https://x/pull/1",
        ),
        (
            "**Full Changelog**: https://x/commits/v1\n\nMore text.\n",
            "\n\n## Commits\n\n**Full Changelog**: https://x/commits/v1\n\nMore text.",
        ),
    ],
)
def test_body_unexpected_generated(generated: str, commits: str) -> None:
    """Generated notes without a final `**Full Changelog**` line are kept."""
    assert release_notes.body("Intro.\n", generated) == (
        f"Intro.{commits}\n",
        ["Generated notes don't end with a '**Full Changelog**: ' line"],
    )


def test_main_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`check` passes for notes in the agreed format."""
    notes = tmp_path / "0.3.0.md"
    notes.write_text(CURATED, encoding="utf-8")
    assert release_notes.main(["check", str(notes)]) == 0
    assert capsys.readouterr() == ("", "")


def test_main_check_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`check` prints findings as error annotations."""
    notes = tmp_path / "0.3.0.md"
    notes.write_text("Intro.\n\n## Fixes\n", encoding="utf-8")
    assert release_notes.main(["check", str(notes)]) == 1
    assert capsys.readouterr() == ("", f"::error file={notes},line=3::Empty section\n")


@pytest.mark.parametrize("command", ["check", "body"])
def test_main_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """Files that can't be read fail with an error annotation."""
    notes = tmp_path / "0.3.0.md"
    args = [command, str(notes)] + (
        [str(tmp_path / "x.md")] if command == "body" else []
    )
    assert release_notes.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("::error::Can't read the notes: ")


def test_main_body(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`body` prints the body of the release."""
    notes = tmp_path / "0.3.0.md"
    notes.write_text(CURATED, encoding="utf-8")
    generated = tmp_path / "generated.md"
    generated.write_text(GENERATED, encoding="utf-8")
    assert release_notes.main(["body", str(notes), str(generated)]) == 0
    assert capsys.readouterr() == (release_notes.body(CURATED, GENERATED)[0], "")


def test_main_body_findings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`body` fails without output if the curated notes have findings."""
    notes = tmp_path / "0.3.0.md"
    notes.write_text("## Fixes\n\n- Fix (#1)\n", encoding="utf-8")
    generated = tmp_path / "generated.md"
    generated.write_text(GENERATED, encoding="utf-8")
    assert release_notes.main(["body", str(notes), str(generated)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No intro paragraph" in captured.err


def test_main_body_unexpected_generated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`body` warns of unexpected generated notes, and keeps them."""
    notes = tmp_path / "0.3.0.md"
    notes.write_text("Intro.\n", encoding="utf-8")
    generated = tmp_path / "generated.md"
    generated.write_text("Changes.\n", encoding="utf-8")
    assert release_notes.main(["body", str(notes), str(generated)]) == 0
    assert capsys.readouterr() == (
        "Intro.\n\n## Commits\n\nChanges.\n",
        "::warning::Generated notes don't end with a '**Full Changelog**: ' line;"
        " kept as they are\n",
    )


@pytest.mark.parametrize(
    "files, missing",
    [
        # the first release with notes needs none
        ([], False),
        # notes of another release, earlier or later: required
        (["0.4.0.md"], True),
        (["0.6.0.md"], True),
        (["0.4.0.md", "0.5.0.md"], False),
        (["0.5.0.md"], False),
        (["0.4.0rc1.md"], True),
        # other files don't count
        (["README.txt"], False),
        (["README.md", "draft-0.4.0.md", "0.4.md"], False),
    ],
)
def test_missing_notes(tmp_path: Path, files: list[str], missing: bool) -> None:
    """Notes are required once notes of another release exist."""
    for name in files:
        (tmp_path / name).write_text(CURATED, encoding="utf-8")
    expected = tmp_path / "0.5.0.md" if missing else None
    assert release_notes.missing_notes("0.5.0", tmp_path) == expected


def test_missing_notes_without_directory(tmp_path: Path) -> None:
    """Without a directory of notes, none are required."""
    assert release_notes.missing_notes("0.5.0", tmp_path / "releases") is None


def test_main_require(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`require` fails with an error annotation if required notes are missing."""
    (tmp_path / "0.4.0.md").write_text(CURATED, encoding="utf-8")
    args = ["require", "0.5.0", "--releases", str(tmp_path)]
    assert release_notes.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"::error::{tmp_path / '0.5.0.md'} is missing")
    (tmp_path / "0.5.0.md").write_text(CURATED, encoding="utf-8")
    assert release_notes.main(args) == 0
    assert capsys.readouterr() == ("", "")


def test_main_require_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`require` reads the notes in `docs/releases/` by default."""
    releases = tmp_path / "docs" / "releases"
    releases.mkdir(parents=True)
    (releases / "0.4.0.md").write_text(CURATED, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert release_notes.main(["require", "0.5.0"]) == 1
    assert release_notes.main(["require", "0.4.0"]) == 0


def test_script(tmp_path: Path) -> None:
    """The script runs as the release workflow calls it."""
    notes = tmp_path / "0.3.0.md"
    notes.write_text(CURATED, encoding="utf-8")
    generated = tmp_path / "generated.md"
    generated.write_text(GENERATED, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [sys.executable, "-I", str(SCRIPT), "body", str(notes), str(generated)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        release_notes.body(CURATED, GENERATED)[0],
        "",
    )


@pytest.mark.parametrize(
    "link",
    [
        "https://foca.readthedocs.io/en/latest/upgrading/",
        "https://foca.readthedocs.io/en/stable/",
        "https://github.com/elixir-cloud-aai/foca/blob/dev/README.md",
        "https://github.com/elixir-cloud-aai/foca/tree/main/docs",
        "https://github.com/elixir-cloud-aai/foca/releases/latest",
        "https://raw.githubusercontent.com/elixir-cloud-aai/foca/dev/x",
        # The root of the docs, which redirects to the default version
        "https://foca.readthedocs.io",
        "https://foca.readthedocs.io/",
        "https://foca.readthedocs.io/en",
        "https://foca.readthedocs.io/en/",
    ],
)
def test_check_moving_links(link: str) -> None:
    """Links to versions that move are rejected, also in sections."""
    markdown = f"Intro, [docs]({link}).\n\n## Fixes\n\n- Fix, see <{link}> (#1)\n"
    assert _messages(markdown) == [(1, "Link to a"), (5, "Link to a")]


@pytest.mark.parametrize(
    "link",
    [
        "https://foca.readthedocs.io/en/v0.3.0/upgrading/",
        "https://foca.readthedocs.io/en/v0.3.0/",
        "https://foca.readthedocs.io.example.org/",
        "https://github.com/elixir-cloud-aai/foca/blob/v0.3.0/README.md",
        "https://github.com/elixir-cloud-aai/foca/tree/dev-notes/docs",
        "https://example.org/en/latest/",
    ],
)
def test_check_versioned_links(link: str) -> None:
    """Links to a release, and other sites' links, are accepted."""
    assert release_notes.check(f"Intro, [docs]({link}).\n") == []


@pytest.mark.parametrize(
    "markdown",
    [
        "Intro, see [Migrating](migration-0.3.md).\n",
        "Intro, see [the guide](../migration-0.3/#steps).\n",
        "Intro, see [docs](/en/v0.3.0/).\n",
        "Intro, see [section](#fixes).\n",
        "Intro, see [docs](<docs/index.md>).\n",
        "Intro, see ![logo](images/logo.svg).\n",
        "Intro, see [docs][1].\n\n[1]: docs/index.md\n",
    ],
)
def test_check_relative_links(markdown: str) -> None:
    """Relative links, which don't resolve in GitHub releases, are rejected.

    Args:
        markdown: Curated release notes with a relative link.
    """
    assert [message for _, message in _messages(markdown)] == ["Relative link (e.g.,"]


@pytest.mark.parametrize(
    "markdown",
    [
        "Intro, see [docs](https://foca.readthedocs.io/en/v0.3.0/).\n",
        "Intro, write to [us](mailto:x@example.org).\n",
        "Intro, see [docs](<https://example.org/a b>).\n",
        "Intro, see [docs][1].\n\n[1]: https://example.org/\n",
        "Intro with brackets [x] (y) and `code`.\n",
    ],
)
def test_check_absolute_links(markdown: str) -> None:
    """Absolute links (with a scheme) and other brackets are accepted.

    Args:
        markdown: Curated release notes without a relative link.
    """
    assert release_notes.check(markdown) == []


def test_body_splits_at_line_breaks_only() -> None:
    """Other line separators (e.g., U+2028) in generated notes start no line."""
    generated = "* x\u2028## Security by @a in u\n\n**Full Changelog**: c\n"
    text, warnings = release_notes.body("Intro.\n", generated)
    assert "\n## Security" not in text
    assert warnings == []


TAGS = (
    "v0.1.0",
    "v0.1.10",
    "v0.1.9",
    "v0.2.0",
    " v0.2.5 ",
    "v0.2.6-x",
    "v0.3.0",
    "v1.0.0a1",
    "v1.0.0b2",
    "v1.0.0rc1",
    "v1.0.0",
    "v1.0.1",
    "chart-v0.1.0",
    "chart-v0.2.0",
    "chart-v9.0.0",
    "other",
)


@pytest.mark.parametrize(
    ("tag", "previous"),
    [
        ("v0.3.0", "v0.2.5"),
        ("v0.2.0", "v0.1.10"),
        ("v1.0.0rc1", "v1.0.0b2"),
        ("v1.0.0", "v1.0.0rc1"),
        ("v1.0.1", "v1.0.0"),
        ("v0.1.0", None),
        ("chart-v0.2.0", "chart-v0.1.0"),
        ("chart-v0.1.0", None),
    ],
)
def test_previous_tag(tag: str, previous: str | None) -> None:
    """The previous tag is the highest lower version of the same stream.

    Args:
        tag: Tag of the release.
        previous: Expected previous tag.
    """
    assert release_notes.previous_tag(tag, TAGS) == previous


def test_previous_tag_invalid() -> None:
    """Tags of no release stream are rejected."""
    with pytest.raises(ValueError, match="Not a release tag"):
        release_notes.previous_tag("chart-0.1.0", [])


@pytest.mark.parametrize(
    ("files", "package", "chart"),
    [
        (["foca/foca.py"], True, False),
        (["charts/foca-common/values.yaml"], False, True),
        (["charts/foca-common/values.yaml", "docs/helm-chart.md"], False, True),
        (["charts/foca-common/values.yaml", "foca/foca.py"], True, True),
        ([".github/workflows/release-chart.yml"], False, True),
        ([".github/actions/test-chart/action.yml"], False, True),
        (["docs/releases/chart/0.1.0.md"], False, True),
        (["docs/releases/0.1.0.md"], True, False),
        (["docs/helm-chart.md.bak"], True, False),
        (["charts/foca-common-other/Chart.yaml"], True, False),
        (["examples/petstore/chart/Chart.yaml"], False, True),
        ([".github/actions/install-chart/action.yml"], False, True),
        ([".github/workflows/charts.yml", ".github/kind/gateway.yaml"], False, True),
        ([".github/workflows/ci.yml"], True, False),
        (["examples/petstore/chart.md"], True, False),
        (["docs/migration-0.20.md", "docs/helm-chart.md"], True, True),
        (["charts/foca-common/"], True, False),
        ([], True, False),
    ],
)
def test_in_stream(files: list[str], package: bool, chart: bool) -> None:
    """Pull requests belong to the streams whose files they changed.

    Args:
        files: Files of the pull request.
        package: Whether it belongs to the package's stream.
        chart: Whether it belongs to the chart's stream.
    """
    assert release_notes.in_stream(files, "package") is package
    assert release_notes.in_stream(files, "chart") is chart


def test_in_stream_unknown() -> None:
    """Unknown streams are rejected."""
    with pytest.raises(ValueError, match="Unknown release stream"):
        release_notes.in_stream([], "docs")


# Generated notes of pull requests 7 (package), 11 (chart) and 12 (both)
STREAMS_GENERATED = """\
<!-- Release notes generated using configuration in .github/release.yml -->

## What's Changed
* feat: add a feature by @someone in https://github.com/o/r/pull/7
* feat(chart): add a value by @newcomer in https://github.com/o/r/pull/11
* fix: fix both by @other in https://github.com/o/r/pull/12

## New Contributors
* @newcomer made their first contribution in https://github.com/o/r/pull/11


**Full Changelog**: https://github.com/o/r/compare/v0.2.0...v0.3.0"""
COMMENT = "<!-- Release notes generated using configuration in .github/release.yml -->"
FULL_CHANGELOG = "**Full Changelog**: https://github.com/o/r/compare/v0.2.0...v0.3.0"


def test_filter_generated_package() -> None:
    """Bullets of other pull requests and sections left empty are dropped."""
    assert release_notes.filter_generated(
        STREAMS_GENERATED, lambda number: number in {7, 12}
    ) == (
        f"{COMMENT}\n\n"
        "## What's Changed\n"
        "* feat: add a feature by @someone in https://github.com/o/r/pull/7\n"
        "* fix: fix both by @other in https://github.com/o/r/pull/12\n\n"
        f"{FULL_CHANGELOG}\n"
    )


def test_filter_generated_chart() -> None:
    """New contributors of kept pull requests stay."""
    assert release_notes.filter_generated(
        STREAMS_GENERATED.replace("\n", "\r\n"), lambda number: number in {11, 12}
    ) == (
        f"{COMMENT}\n\n"
        "## What's Changed\n"
        "* feat(chart): add a value by @newcomer in https://github.com/o/r/pull/11\n"
        "* fix: fix both by @other in https://github.com/o/r/pull/12\n\n"
        "## New Contributors\n"
        "* @newcomer made their first contribution in"
        " https://github.com/o/r/pull/11\n\n"
        f"{FULL_CHANGELOG}\n"
    )


def test_filter_generated_none() -> None:
    """Without pull requests left, the Full Changelog line stays alone."""
    filtered = release_notes.filter_generated(GENERATED, lambda number: False)
    assert filtered == f"{FULL_CHANGELOG}\n"
    # The body then has no Commits section
    assert "## Commits" not in release_notes.body(CURATED, filtered)[0]


def test_filter_generated_unexpected() -> None:
    """Notes without a Full Changelog line keep their other lines."""
    assert (
        release_notes.filter_generated(
            "Some text\n* a bullet without a pull request", lambda number: False
        )
        == "Some text\n* a bullet without a pull request\n"
    )
    assert release_notes.filter_generated("", lambda number: False) == "\n"


def test_filter_generated_body() -> None:
    """Filtered notes give the body in the agreed format."""
    filtered = release_notes.filter_generated(
        STREAMS_GENERATED, lambda number: number == 7
    )
    assert release_notes.body(CURATED, filtered) == (
        f"{CURATED.strip()}\n\n## Commits\n\n{COMMENT}\n\n"
        "* feat: add a feature by @someone in https://github.com/o/r/pull/7\n\n"
        f"{FULL_CHANGELOG}\n",
        [],
    )


def test_pull_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """The files of a pull request are listed with the GitHub CLI."""
    calls = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Record the call and return two files.

        Args:
            args: Command.
            **kwargs: Options of `subprocess.run`.

        Returns:
            The completed process.
        """
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "a.py\n\nb/c.md\n", "")

    monkeypatch.setattr(release_notes.subprocess, "run", run)
    assert release_notes.pull_files("o/r", 7) == ["a.py", "b/c.md"]
    assert calls == [
        (
            [
                "gh",
                "api",
                "--paginate",
                "repos/o/r/pulls/7/files?per_page=100",
                "--jq",
                release_notes.FILES_JQ,
            ],
            {"capture_output": True, "text": True, "check": True},
        )
    ]


def test_main_previous(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The previous tag is printed, or nothing if there is none."""
    monkeypatch.setattr(
        release_notes.sys, "stdin", io.StringIO("v0.1.0\nchart-v0.1.0\nv0.2.0\n")
    )
    assert release_notes.main(["previous", "v0.3.0"]) == 0
    assert capsys.readouterr().out == "v0.2.0\n"
    monkeypatch.setattr(release_notes.sys, "stdin", io.StringIO("v0.1.0\n"))
    assert release_notes.main(["previous", "chart-v0.1.0"]) == 0
    assert capsys.readouterr().out == ""


def test_main_previous_invalid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tags of no release stream fail."""
    monkeypatch.setattr(release_notes.sys, "stdin", io.StringIO(""))
    assert release_notes.main(["previous", "0.1.0"]) == 1
    assert capsys.readouterr().err == "::error::Not a release tag: '0.1.0'\n"


def _filter_args(generated: Path, stream: str) -> list[str]:
    """Return the arguments of the `filter` command.

    Args:
        generated: Generated notes.
        stream: Release stream.

    Returns:
        The arguments, for repository `o/r`.
    """
    return ["filter", str(generated), "--stream", stream, "--repo", "o/r"]


def test_main_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The files of each pull request are listed once."""
    generated = tmp_path / "generated.md"
    generated.write_text(STREAMS_GENERATED, encoding="utf-8")
    files = {
        7: ["foca/foca.py"],
        11: ["charts/foca-common/values.yaml"],
        12: ["foca/foca.py", "docs/helm-chart.md"],
    }
    calls = []

    def pull_files(repository: str, number: int) -> list[str]:
        """Return the files of a pull request of `files`.

        Args:
            repository: Repository.
            number: Number of the pull request.

        Returns:
            Its files.
        """
        calls.append((repository, number))
        return files[number]

    monkeypatch.setattr(release_notes, "pull_files", pull_files)
    assert release_notes.main(_filter_args(generated, "chart")) == 0
    assert capsys.readouterr().out == release_notes.filter_generated(
        STREAMS_GENERATED, lambda number: number in {11, 12}
    )
    assert calls == [("o/r", 7), ("o/r", 11), ("o/r", 12)]


def test_main_filter_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Generated notes that can't be read fail."""
    assert release_notes.main(_filter_args(tmp_path / "missing.md", "chart")) == 1
    assert capsys.readouterr().err.startswith("::error::Can't read the generated")


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404"),
        FileNotFoundError("gh"),
    ],
)
def test_main_filter_gh_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """Failures to list the files of a pull request fail, printing nothing.

    Args:
        monkeypatch: Pytest fixture.
        tmp_path: Pytest fixture.
        capsys: Pytest fixture.
        error: Error of the GitHub CLI.
    """
    generated = tmp_path / "generated.md"
    generated.write_text(STREAMS_GENERATED, encoding="utf-8")

    def pull_files(repository: str, number: int) -> list[str]:
        """Fail.

        Args:
            repository: Repository.
            number: Number of the pull request.

        Raises:
            Exception: Always.
        """
        raise error

    monkeypatch.setattr(release_notes, "pull_files", pull_files)
    assert release_notes.main(_filter_args(generated, "package")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("::error::Can't list the files")


def test_script_previous() -> None:
    """The script reads the tags from standard input."""
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [sys.executable, "-I", str(SCRIPT), "previous", "chart-v0.2.0"],
        input="chart-v0.1.0\nv0.1.5\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "chart-v0.1.0\n",
        "",
    )
