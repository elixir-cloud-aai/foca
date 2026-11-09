"""Check curated release notes and build the body of a GitHub release.

Release notes follow the format agreed for all repositories of the
organization (see "Release notes" in `docs/contributing/index.md`):

1. An intro paragraph saying what the release brings, with a link to the
   upgrade or migration guide if there is one.
2. Grouped sections, in the order of `SECTIONS`, with empty ones left out;
   each bullet ends with its issue and pull request numbers, e.g.,
   `(#12, #34)`.
3. `## Commits`: GitHub's generated notes, verbatim (including "New
   Contributors"), without their `## What's Changed` header.
4. Last, GitHub's generated `**Full Changelog**: ...` line.

The release body never changes, so curated notes must not link to versions
that move (`UNVERSIONED`, e.g., the latest docs or branch `dev`), but to the
release's (e.g., `/en/vX.Y.Z/`, `blob/vX.Y.Z/`).

The curated notes (`docs/releases/<version>.md`) hold parts 1 and 2; the
release workflow (`.github/workflows/release.yml`) adds parts 3 and 4 from
GitHub's `generate-notes` API.

The repository has two release streams: the package and its images (tags
`vX.Y.Z`, `release.yml`) and the library chart `foca-common` (tags
`chart-vX.Y.Z`, `release-chart.yml`, curated notes in
`docs/releases/chart/<version>.md`). The generated notes of each stream
compare against the previous tag of the same stream (`previous_tag`) and
list only the pull requests that changed it (`filter_generated`,
`in_stream`): those that changed the chart's files (`CHART_PATHS`) in the
chart's, those that changed any other file in the package's; pull requests
that changed both are in both.

Commands:
    `check <notes>`: check the structure of curated notes.
    `body <notes> <generated>`: check curated notes, then print the body of
        the release to standard output: the curated notes, then the
        generated notes (the `body` of the `generate-notes` response, in a
        file).
    `require <version> [--releases <dir>]`: fail if the curated notes of a
        release (`<dir>/<version>.md`, by default in `docs/releases/`) are
        missing, unless no other release has notes (see `missing_notes`).
    `generated <tag> --repo <owner/name>`: print GitHub's generated notes
        of a release (a `v*` or `chart-v*` tag) for its stream, with the
        GitHub CLI (`gh`, authenticated, e.g., with `GH_TOKEN`; see
        `generated_notes`).

Errors (and warnings) are printed as GitHub annotations to standard error.
Only the stdlib is used, so the workflow runs it without installing the app.
"""

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Headings of the curated notes, in their order; all are optional
SECTIONS = (
    "Breaking changes",
    "Features",
    "Security",
    "Fixes",
    "Documentation",
    "Maintenance",
)
# Added by the release workflow, after the curated sections
COMMITS = "Commits"
# Header of GitHub's generated notes, which is dropped
WHATS_CHANGED = "## What's Changed"
# Start of the last line of GitHub's generated notes, which the curated notes
# must not have
FULL_CHANGELOG = "**Full Changelog**"

# ATX headings (`## Title`)
HEADING = re.compile(
    r" {0,3}(?P<level>#{1,6})(?:[ \t]+(?P<title>.*?))??(?:[ \t]+#*)?[ \t]*"
)
# Setext heading underlines (`---`, `===`) and thematic breaks (also `***`,
# `___`), which are rejected: a line of `-` or `=` under a paragraph makes it
# a heading
UNDERLINE = re.compile(r" {0,3}(?:(?:-[ \t]*)+|=+[ \t]*|(?:[*_][ \t]*){3,})")
# Opening and closing lines of fenced code blocks: a block closes with a line
# of the same character, at least as long as the opening one, and nothing else
FENCE = re.compile(r"[ \t]*(?P<marker>`{3,}|~{3,})(?P<rest>.*)")
# Top-level list items (bullets or numbered)
ITEM = re.compile(r"(?:[-*+]|[0-9]+[.)])(?:[ \t]|$)")
# The end of a bullet: its issue and pull request numbers, e.g., `(#12, #34)`
# (only the pull request's if it closes no issue)
REFERENCES = re.compile(r".*\(#[0-9]+(?:[,;][ \t]*#[0-9]+)*\)", re.ASCII)

BULLETS_ONLY = "Sections hold bullets only, e.g., - <change> (#12, #34)"

# Names of curated notes: `<version>.md`, for release versions as the
# release workflow accepts them (`vX.Y.Z`, or `vX.Y.ZaN`, `vX.Y.ZbN`,
# `vX.Y.ZrcN`; see "Check release tag and commit" in `release.yml`)
NOTES_NAME = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?\.md",
    re.ASCII,
)
# Docs on Read the Docs and repository on GitHub, and links to versions of
# them that move
DOCS_HOST = "foca.readthedocs.io"
REPOSITORY = "elixir-cloud-aai/foca"
_MOVING = r"(?![\w.-])"
UNVERSIONED = re.compile(
    rf"{re.escape(DOCS_HOST)}/(?:en/)?(?:latest|stable){_MOVING}"
    # The root of the docs, which redirects to the default version
    rf"|{re.escape(DOCS_HOST)}(?:/(?:en/?)?)?(?![\w./-])"
    rf"|github\.com/{re.escape(REPOSITORY)}/"
    rf"(?:blob|tree|raw|edit|blame|commits)/(?:refs/heads/)?(?:dev|main|HEAD)"
    rf"{_MOVING}"
    rf"|github\.com/{re.escape(REPOSITORY)}/releases/latest{_MOVING}"
    rf"|raw\.githubusercontent\.com/{re.escape(REPOSITORY)}/"
    rf"(?:refs/heads/)?(?:dev|main|HEAD){_MOVING}",
    re.ASCII | re.IGNORECASE,
)
MOVING_LINK = (
    "Link to a version that moves (e.g., latest docs, branch dev): the release"
    " body never changes, so link the release's (/en/vX.Y.Z/, blob/vX.Y.Z/)"
)
# Relative links (inline, also of images, or link reference definitions):
# destinations without a scheme (e.g., `https:`, `mailto:`), which GitHub
# resolves against the release page, not the docs
_NO_SCHEME = r"(?:<|(?!<))(?![a-z][a-z0-9+.-]*:)"
RELATIVE = re.compile(
    rf"\]\([ \t]*{_NO_SCHEME}[^)\s<>]"
    rf"|^ {{0,3}}\[[^\]]+\]:[ \t]*{_NO_SCHEME}[^\s<>]",
    re.ASCII | re.IGNORECASE,
)
RELATIVE_LINK = (
    "Relative link (e.g., to a page of the docs): it doesn't resolve in the"
    " GitHub release, so link the release's docs or files absolutely"
    " (https://foca.readthedocs.io/en/vX.Y.Z/...)"
)

# Files of the library chart's release stream: the chart (with its tests),
# its docs and curated notes, the example apps' charts (which test it), and
# its release workflow, the actions and kind configurations that test it,
# and the former chart workflow `charts.yml` (now in `ci.yml`; for the
# pull requests before its removal, in notes since then), and the chart's
# tests in `tests/` (`test_chart*.py`). Everything else (also the migration
# guides, the deployment docs, `mkdocs.yml`, `docs/changelog.md` and
# `ci.yml`) belongs to the package, so a chart's release preparation, which
# adds its notes to the docs' navigation and changelog, is in both
CHART_PATHS = re.compile(
    r"charts/foca-common/.+"
    r"|docs/helm-chart\.md"
    r"|docs/releases/chart/.+"
    r"|examples/[^/]+/chart/.+"
    r"|\.github/workflows/(?:release-chart|charts)\.yml"
    r"|\.github/actions/(?:test|install)-chart/.+"
    r"|\.github/kind/.+"
    r"|tests/test_chart[^/]*\.py"
)
# Release streams and the prefixes of their tags
STREAMS = {"package": "v", "chart": "chart-v"}
# Release tags of both streams, as the workflows accept them (see "Check
# release tag and commit" in `release.yml` and "Check tag and version" in
# `release-chart.yml`; also with leading zeros, so that a tag they accept
# never fails here, after publishing)
TAG = re.compile(
    r"(?P<prefix>chart-v|v)(?P<major>[0-9]+)\.(?P<minor>[0-9]+)"
    r"\.(?P<patch>[0-9]+)(?:(?P<pre>a|b|rc)(?P<number>[0-9]+))?",
    re.ASCII,
)
# Order of pre-releases before the final version (`None`)
PRE_RELEASES = {"a": 0, "b": 1, "rc": 2, None: 3}
# Bullets of GitHub's generated notes that name a pull request, in "What's
# Changed" (`* <title> by @<author> in <url>`) and "New Contributors"
# (`* @<author> made their first contribution in <url>`)
PULL = re.compile(
    r"\* .* in https://github\.com/[\w.-]+/[\w.-]+/pull/(?P<number>[0-9]+)",
    re.ASCII,
)
# Files of a pull request from the `pulls/<number>/files` API, one per line,
# with the old name of a renamed one after a tab; the API lists at most
# `MAX_FILES` files
FILES_JQ = '.[] | [.filename, (.previous_filename // empty)] | join("\\t")'
MAX_FILES = 3000


@dataclass(frozen=True)
class Finding:
    """A problem in the structure of curated release notes.

    Attributes:
        line: Number of the line (from 1).
        message: What is wrong.
    """

    line: int
    message: str


def _is_comment(line: str) -> bool:
    """Return whether a line is a single HTML comment, which counts as blank.

    Comments over several lines are not supported. Lines are checked one by
    one, so this is no HTML filter (the notes are published as written).

    Args:
        line: Line of the notes.

    Returns:
        Whether the line, without surrounding whitespace, is one comment.
    """
    text = line.strip()
    return (
        len(text) >= len("<!---->")
        and text.startswith("<!--")
        and text.endswith("-->")
        and "-->" not in text[len("<!--") : -len("-->")]
    )


def _heading_finding(
    number: int, level: int, title: str, last: int | None
) -> Finding | None:
    """Return the problem of a heading of curated notes, if any.

    Args:
        number: Number of the heading's line.
        level: Level of the heading (number of `#`).
        title: Title of the heading.
        last: Index in `SECTIONS` of the last valid section before, if any.

    Returns:
        A finding unless the heading is a section of `SECTIONS` after `last`.
    """
    allowed = ", ".join(SECTIONS)
    if level != 2:
        return Finding(
            number,
            f"Heading of level {level} ('{title}'): only sections"
            f" ('## <title>') are allowed: {allowed}",
        )
    if title == COMMITS:
        return Finding(
            number,
            f"Section '{COMMITS}': the release workflow adds it from GitHub's"
            " generated notes",
        )
    if title not in SECTIONS:
        return Finding(
            number, f"Unknown section '{title}': allowed are, in this order: {allowed}"
        )
    if last is not None and SECTIONS.index(title) <= last:
        return Finding(
            number,
            f"Section '{title}' after '{SECTIONS[last]}': sections are in this"
            f" order, each once: {allowed}",
        )
    return None


@dataclass
class _Checker:
    """State of the check of curated release notes, line by line.

    Attributes:
        findings: Findings so far.
        intro: Whether the intro has a paragraph (not only lists or code).
        heading_line: Line of the current heading (0 in the intro).
        valid: Whether the current heading is a valid section.
        bullets: Number of bullets in the current section.
        last: Index in `SECTIONS` of the last valid section, if any.
        item: Lines of the current bullet (number, text).
        fence: Marker of the open fenced code block (e.g., ```` ``` ````), if
            any.
        previous_blank: Whether the previous line is blank.
    """

    findings: list[Finding] = field(default_factory=list)
    intro: bool = False
    heading_line: int = 0
    valid: bool = False
    bullets: int = 0
    last: int | None = None
    item: list[tuple[int, str]] = field(default_factory=list)
    fence: str | None = None
    previous_blank: bool = True

    def run(self, lines: list[str]) -> list[Finding]:
        """Check the lines of curated notes.

        Args:
            lines: Lines of the notes.

        Returns:
            The findings, in the order of their lines.
        """
        for number, line in enumerate(lines, start=1):
            self._line(number, line)
        self._close_section()
        if self.fence:
            self.findings.append(Finding(len(lines), "Unclosed fenced code block"))
        if not self.intro:
            self.findings.append(Finding(max(len(lines), 1), "No intro paragraph"))
        return sorted(self.findings, key=lambda finding: finding.line)

    def _line(self, number: int, line: str) -> None:
        """Check a line.

        Args:
            number: Number of the line.
            line: Text of the line.
        """
        fence = FENCE.fullmatch(line)
        if self.fence or fence:
            self._code(number, line, fence)
            return
        if line.lstrip().startswith(FULL_CHANGELOG):
            self.findings.append(
                Finding(
                    number,
                    f"'{FULL_CHANGELOG}' line: the release workflow adds GitHub's",
                )
            )
        heading = HEADING.fullmatch(line)
        if heading:
            self._heading(number, len(heading["level"]), heading["title"] or "")
            return
        blank = not line.strip() or _is_comment(line)
        if not blank and UNDERLINE.fullmatch(line):
            self.findings.append(
                Finding(
                    number,
                    "Setext heading or thematic break: use no headings but the"
                    " sections, and no rules",
                )
            )
        elif self.heading_line == 0:
            # A paragraph, not a list
            self.intro = self.intro or not (blank or ITEM.match(line))
        elif blank:
            if self.item:
                # As a blank line (also comments)
                self.item.append((number, ""))
        elif ITEM.match(line):
            self._close_item()
            self.item = [(number, line)]
            self.bullets += 1
        elif self.item and (line[0] in " \t" or not self.previous_blank):
            # Indented content of the bullet, or a lazy continuation line of
            # its paragraph
            self.item.append((number, line))
        elif self.valid:
            self.findings.append(Finding(number, BULLETS_ONLY))
        self.previous_blank = blank

    def _code(self, number: int, line: str, fence: re.Match[str] | None) -> None:
        """Check a line of a fenced code block, or a fence.

        Code blocks belong to the intro or to a bullet.

        Args:
            number: Number of the line.
            line: Text of the line.
            fence: Match of `FENCE` on the line, if any.
        """
        if self.fence is None and fence:
            self.fence = fence["marker"]
            # Code blocks in the intro don't make it one
            if self.heading_line and not self.item and self.valid:
                self.findings.append(Finding(number, BULLETS_ONLY))
        elif (
            self.fence
            and fence
            and fence["marker"][0] == self.fence[0]
            and len(fence["marker"]) >= len(self.fence)
            and not fence["rest"].strip()
        ):
            self.fence = None
        if self.heading_line and self.item:
            self.item.append((number, line))
        self.previous_blank = False

    def _heading(self, number: int, level: int, title: str) -> None:
        """Check a heading, which starts a section.

        Args:
            number: Number of the heading's line.
            level: Level of the heading (number of `#`).
            title: Title of the heading.
        """
        self._close_section()
        title = title.strip()
        finding = _heading_finding(number, level, title, self.last)
        if finding:
            self.findings.append(finding)
        else:
            self.last = SECTIONS.index(title)
        self.heading_line, self.valid, self.bullets = number, finding is None, 0
        if not self.intro:
            self.findings.append(
                Finding(number, "No intro paragraph before the first section")
            )
            self.intro = True
        self.previous_blank = False

    def _close_item(self) -> None:
        """Check that the current bullet ends with its numbers, and end it."""
        content = [(number, line) for number, line in self.item if line.strip()]
        # The numbers may be wrapped over lines
        text = " ".join(line.strip() for _, line in content)
        # Only in valid sections, as invalid headings have findings already
        if self.valid and content and not REFERENCES.fullmatch(text):
            self.findings.append(
                Finding(
                    content[-1][0],
                    "Bullet doesn't end with its issue and pull request"
                    " numbers, e.g., (#12, #34)",
                )
            )
        self.item = []

    def _close_section(self) -> None:
        """End the current bullet and section; valid ones must have bullets."""
        self._close_item()
        if self.valid and self.bullets == 0:
            self.findings.append(Finding(self.heading_line, "Empty section"))


def check(markdown: str) -> list[Finding]:
    """Return the problems in the structure of curated release notes.

    Args:
        markdown: Curated release notes (parts 1 and 2 of the format).

    Returns:
        The findings, in the order of their lines; none if the notes have an
        intro paragraph (lists and code blocks alone don't count), then only
        headings of `SECTIONS` (level 2, each once, in their order, none
        empty), holding bullets only, each ending with its issue and pull
        request numbers, and none of the parts that the release workflow adds
        (`## Commits`, the `**Full Changelog**` line), and no links to
        versions that move (`UNVERSIONED`) and no relative links
        (`RELATIVE`). An HTML comment on a line of its own counts as a blank
        line.
    """
    lines = markdown.removeprefix("\ufeff").splitlines()
    findings = _Checker().run(lines)
    for number, line in enumerate(lines, start=1):
        if UNVERSIONED.search(line):
            findings.append(Finding(number, MOVING_LINK))
        if RELATIVE.search(line):
            findings.append(Finding(number, RELATIVE_LINK))
    return sorted(findings, key=lambda finding: finding.line)


def body(curated: str, generated: str) -> tuple[str, list[str]]:
    """Return the body of a release: curated notes, then generated ones.

    Args:
        curated: Curated release notes (parts 1 and 2 of the format); they
            are not checked here (see `check`).
        generated: GitHub's generated notes (the `body` of the
            `generate-notes` response).

    Returns:
        The body: the curated notes, then `## Commits` with the generated
        notes without their `## What's Changed` header (left out if they list
        no changes), whose last line is the `**Full Changelog**` line; and
        warnings about unexpected generated notes, which are then kept as
        they are (so that an unexpected format doesn't fail the release
        workflow after the image was published).
    """
    # Split at line breaks only (`splitlines` also splits at, e.g., U+2028,
    # which could make a title of a pull request a heading)
    text = generated.strip().replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    warnings = []
    # Also after a comment (e.g., with a configuration in `.github/release.yml`)
    if WHATS_CHANGED in lines:
        lines.remove(WHATS_CHANGED)
    if not lines or not lines[-1].startswith(f"{FULL_CHANGELOG}: "):
        warnings.append(f"Generated notes don't end with a '{FULL_CHANGELOG}: ' line")
        changes, last = "\n".join(lines).strip(), []
    else:
        changes, last = "\n".join(lines[:-1]).strip(), [lines[-1]]
    parts = [curated.strip()]
    if changes:
        parts.append(f"## {COMMITS}\n\n{changes}")
    return "\n\n".join(parts + last) + "\n", warnings


def missing_notes(version: str, releases: Path) -> Path | None:
    """Get the curated notes that a release lacks, if they are required.

    Curated notes are required once notes of any other release exist (in
    practice, for every release after the first one that had them); a
    repository without any notes publishes GitHub's generated notes alone.
    Only files named like notes count (`NOTES_NAME`), not, e.g., a README.

    Args:
        version: Version of the release, e.g., `1.2.3` or `1.2.3rc1`.
        releases: Directory with the curated notes, one `<version>.md` each.

    Returns:
        The path of the missing notes, or `None` if they exist or no other
        release has notes.
    """
    notes = releases / f"{version}.md"
    if notes.is_file():
        return None
    others = [
        path
        for path in releases.glob("*.md")
        if path.name != notes.name and NOTES_NAME.fullmatch(path.name)
    ]
    return notes if others else None


def _tag_key(match: re.Match[str]) -> tuple[int, int, int, int, int]:
    """Return the sort key of a release tag (its version).

    Args:
        match: Match of `TAG` on the tag.

    Returns:
        Major, minor and patch version, then the order of the pre-release
        (`PRE_RELEASES`, final versions last) and its number.
    """
    return (
        int(match["major"]),
        int(match["minor"]),
        int(match["patch"]),
        PRE_RELEASES[match["pre"]],
        int(match["number"] or 0),
    )


def previous_tag(
    tag: str, tags: Sequence[str], published: Collection[str] = ()
) -> str | None:
    """Return the tag of the previous release of the same stream.

    GitHub's `generate-notes` API compares against the previous release of
    either stream unless it is given the previous tag, so it is always given
    this one, if there is one.

    Args:
        tag: Tag of the release, e.g., `v1.2.3` or `chart-v1.2.3`.
        tags: Tags of the repository; others than release tags are ignored.
        published: Tags with a published GitHub release. If any of the
            candidates has one, only those count, so that a tag whose
            release failed (and was replaced by a later version) is skipped
            and its changes are listed in the next release; if none has one
            (e.g., before the first GitHub release of a stream), all count.

    Returns:
        The tag of the highest version below that of `tag` with the same
        prefix (pre-releases come before their final version, e.g.,
        `v1.0.0rc1` before `v1.0.0`), or `None` if there is none. Final
        releases compare against the previous final release only, so that
        their notes cover all changes since then, as their curated notes do
        (e.g., `v1.0.0` against `v0.9.0`, not `v1.0.0rc2`); pre-releases
        against the previous release of either kind.

    Raises:
        ValueError: If `tag` is not a release tag.
    """
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"Not a release tag: '{tag}'")
    key = _tag_key(match)
    final = match["pre"] is None
    candidates = [
        (_tag_key(other), other.string)
        for other in (TAG.fullmatch(name.strip()) for name in tags)
        if other
        and other["prefix"] == match["prefix"]
        and _tag_key(other) < key
        and (not final or other["pre"] is None)
    ]
    released = [candidate for candidate in candidates if candidate[1] in published]
    candidates = released or candidates
    return max(candidates)[1] if candidates else None


def _is_chart(path: str) -> bool:
    """Return whether a file belongs to the chart's release stream.

    Args:
        path: Path of the file, relative to the repository's root.

    Returns:
        Whether the path matches `CHART_PATHS`.
    """
    return CHART_PATHS.fullmatch(path) is not None


def in_stream(files: Sequence[str], stream: str) -> bool:
    """Return whether a pull request belongs to a release stream.

    Args:
        files: Files that the pull request changed.
        stream: Release stream (`package` or `chart`).

    Returns:
        For the chart, whether any file is one of the chart's
        (`CHART_PATHS`); for the package, whether any file is not, or the
        pull request changed no files.

    Raises:
        ValueError: If `stream` is unknown.
    """
    if stream not in STREAMS:
        raise ValueError(f"Unknown release stream: '{stream}'")
    if stream == "chart":
        return any(_is_chart(path) for path in files)
    return not files or not all(_is_chart(path) for path in files)


def pull_files(repository: str, number: int) -> list[str]:
    """Return the files that a pull request changed, with the GitHub CLI.

    Args:
        repository: Repository, as `<owner>/<name>`.
        number: Number of the pull request.

    Returns:
        The paths of the files (also the old paths of renamed ones).

    Raises:
        subprocess.CalledProcessError: If `gh` fails.
        TruncatedFilesError: If the API lists `MAX_FILES` files, its limit,
            so that the list may be incomplete.
    """
    output = _gh(
        "api",
        "--paginate",
        f"repos/{repository}/pulls/{number}/files?per_page=100",
        "--jq",
        FILES_JQ,
    )
    entries = [line for line in output.splitlines() if line]
    if len(entries) >= MAX_FILES:
        raise TruncatedFilesError(
            f"the API lists at most {MAX_FILES} files of a pull request"
        )
    return [path for entry in entries for path in entry.split("\t") if path]


class TruncatedFilesError(Exception):
    """The files of a pull request are more than the API lists."""


def _gh(*args: str) -> str:
    """Run the GitHub CLI.

    Args:
        *args: Arguments of `gh`.

    Returns:
        Its standard output.

    Raises:
        OSError: If `gh` can't be run.
        subprocess.CalledProcessError: If `gh` fails.
    """
    result = subprocess.run(  # noqa: S603 (no shell)
        ["gh", *args],  # noqa: S607 (`gh` from the PATH, as in the workflows)
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _one_line(error: Exception) -> str:
    """Return an error of the GitHub CLI on one line, as for an annotation.

    Args:
        error: Error of running `gh`.

    Returns:
        The error and, if any, the standard error of `gh`, on one line.
    """
    stderr = getattr(error, "stderr", None) or ""
    return " ".join(f"{error} {stderr}".split())


def stream_of(tag: str) -> str:
    """Return the release stream of a release tag.

    Args:
        tag: Release tag, e.g., `v1.2.3` or `chart-v1.2.3`.

    Returns:
        The stream (a key of `STREAMS`).

    Raises:
        ValueError: If `tag` is not a release tag.
    """
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"Not a release tag: '{tag}'")
    return next(name for name, prefix in STREAMS.items() if prefix == match["prefix"])


def generated_notes(tag: str, repository: str) -> str:
    """Return GitHub's generated notes of a release, for its stream only.

    Generated with GitHub's `generate-notes` API, as `gh release create
    --generate-notes` does, but since the previous tag of the same stream,
    preferring tags with a published release (`previous_tag`; GitHub's
    choice if there is none, e.g., for the first release
    of a stream), then filtered to the pull requests of the stream
    (`filter_generated`, `in_stream`).

    Args:
        tag: Release tag, e.g., `v1.2.3` or `chart-v1.2.3`.
        repository: Repository, as `<owner>/<name>`.

    Returns:
        The filtered generated notes.

    Raises:
        ValueError: If `tag` is not a release tag.
        OSError: If `gh` can't be run.
        subprocess.CalledProcessError: If `gh` fails.
    """
    stream = stream_of(tag)
    tags = _gh(
        "api",
        "--paginate",
        f"repos/{repository}/git/matching-refs/tags/{STREAMS[stream]}",
        "--jq",
        '.[].ref | ltrimstr("refs/tags/")',
    ).splitlines()
    # Tags with a published release (drafts are listed with write access)
    published = set(
        _gh(
            "api",
            "--paginate",
            f"repos/{repository}/releases?per_page=100",
            "--jq",
            ".[] | select(.draft | not) | .tag_name",
        ).splitlines()
    )
    previous = previous_tag(tag, tags, published)
    fields = ["-f", f"tag_name={tag}"]
    if previous:
        fields += ["-f", f"previous_tag_name={previous}"]
    generated = _gh(
        "api",
        "--method",
        "POST",
        f"repos/{repository}/releases/generate-notes",
        *fields,
        "--jq",
        ".body",
    )
    # Files of each pull request, read once; `None` if they can't be listed
    files: dict[int, list[str] | None] = {}

    def keep(number: int) -> bool:
        """Return whether a pull request is in the stream.

        Args:
            number: Number of the pull request.

        Returns:
            Whether it belongs to the stream (see `in_stream`), or `True` if
            its files can't be listed.
        """
        if number not in files:
            try:
                files[number] = pull_files(repository, number)
            except (
                OSError,
                subprocess.CalledProcessError,
                TruncatedFilesError,
            ) as error:
                # Kept, as the release is published by then: a failure
                # that repeats on re-runs would otherwise need a new version
                print(
                    f"::warning::Can't list the files of pull request #{number},"
                    f" kept in the notes: {_one_line(error)}",
                    file=sys.stderr,
                )
                files[number] = None
        changed = files[number]
        return changed is None or in_stream(changed, stream)

    return filter_generated(generated, keep)


def filter_generated(generated: str, keep: Callable[[int], bool]) -> str:
    """Return GitHub's generated notes with only some pull requests.

    Args:
        generated: GitHub's generated notes (the `body` of the
            `generate-notes` response).
        keep: Whether to keep the bullets of a pull request, by its number.

    Returns:
        The notes without the bullets of the other pull requests (in "What's
        Changed" and "New Contributors"), and without sections (`## ...`)
        that have no bullets left; other lines are kept as they are, with
        one blank line between the parts, and the last line (the
        `**Full Changelog**` line) last.
    """
    text = generated.strip().replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    last = []
    if lines and lines[-1].startswith(f"{FULL_CHANGELOG}: "):
        last = [lines.pop()]
    # Parts: the lines before the first section, then one per section
    parts: list[list[str]] = [[]]
    for line in lines:
        if HEADING.fullmatch(line):
            parts.append([line])
            continue
        pull = PULL.fullmatch(line)
        if pull is None or keep(int(pull["number"])):
            parts[-1].append(line)
    kept = []
    for index, part in enumerate(parts):
        content = "\n".join(part).strip()
        bullets = any(line.startswith("* ") for line in part)
        if content and (index == 0 or bullets):
            kept.append(content)
    return "\n\n".join(kept + last) + "\n"


def _generated(args: argparse.Namespace) -> int:
    """Print the generated notes of a release's stream (`generated`).

    Args:
        args: Parsed arguments of the command.

    Returns:
        The exit status: 0 on success, 1 if the tag is not a release tag or
        the GitHub CLI fails.
    """
    try:
        text = generated_notes(args.tag, args.repo)
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError) as error:
        print(
            f"::error::Can't generate the release notes: {_one_line(error)}",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(text)
    return 0


def _require(args: argparse.Namespace) -> int:
    """Fail if a release lacks required curated notes (command `require`).

    Args:
        args: Parsed arguments of the command.

    Returns:
        The exit status: 0 if the notes exist or aren't required, 1 if they
        are missing.
    """
    notes = missing_notes(args.version, args.releases)
    if notes is None:
        return 0
    print(
        f"::error::{notes} is missing: curated release notes are required for"
        " every release after the first one that had them",
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command (see the module's docstring).

    Args:
        argv: Command-line arguments, without the program name; those of the
            process if `None`.

    Returns:
        The exit status: 0 on success, 1 if the curated notes have findings or
        are missing (`require`), a file can't be read, or a tag is not a
        release tag or the GitHub CLI fails (`generated`).
    """
    parser = argparse.ArgumentParser(
        description="Check curated release notes and build the release body."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="check curated notes")
    check_parser.add_argument("notes", type=Path, help="curated notes")
    body_parser = commands.add_parser("body", help="print the release body")
    body_parser.add_argument("notes", type=Path, help="curated notes")
    body_parser.add_argument(
        "generated", type=Path, help="generated notes (`body` of generate-notes)"
    )
    require_parser = commands.add_parser(
        "require", help="fail if required curated notes are missing"
    )
    require_parser.add_argument("version", help="release version, e.g., 1.2.3")
    require_parser.add_argument(
        "--releases",
        type=Path,
        default=Path("docs/releases"),
        help="directory with the curated notes (default: docs/releases)",
    )
    generated_parser = commands.add_parser(
        "generated", help="print the generated notes of a release's stream"
    )
    generated_parser.add_argument("tag", help="release tag, e.g., v1.2.3")
    generated_parser.add_argument(
        "--repo", required=True, help="repository, as <owner>/<name>"
    )
    args = parser.parse_args(argv)
    if args.command == "require":
        return _require(args)
    if args.command == "generated":
        return _generated(args)
    notes: Path = args.notes
    try:
        curated = notes.read_text(encoding="utf-8")
        generated = (
            args.generated.read_text(encoding="utf-8") if args.command == "body" else ""
        )
    except (OSError, UnicodeDecodeError) as error:
        print(f"::error::Can't read the notes: {error}", file=sys.stderr)
        return 1
    findings = check(curated)
    for finding in findings:
        print(
            f"::error file={notes},line={finding.line}::{finding.message}",
            file=sys.stderr,
        )
    if findings:
        return 1
    if args.command == "body":
        text, warnings = body(curated, generated)
        for warning in warnings:
            print(f"::warning::{warning}; kept as they are", file=sys.stderr)
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
