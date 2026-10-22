"""Set and check the release in the citation metadata (`CITATION.cff`).

Zenodo archives each GitHub release with the `CITATION.cff` of the tagged
commit, so `version` and `date-released` are set before the release is
tagged, and the release workflow checks them. Errors are printed as GitHub
annotations to standard error.

Commands:
    `set <version> [--date YYYY-MM-DD]`: set `version` and `date-released`
        (default: today, in UTC), adding the keys if they are missing.
    `check <version>`: check that `version` is the release version and that
        `date-released` is a valid date that is not in the future (UTC).

Only the stdlib is used, so the workflows run it without installing the
package; the keys are edited as top-level lines, keeping the rest of the
file as is.
"""

import argparse
import datetime
import re
import sys
from collections.abc import Sequence
from pathlib import Path

CITATION = Path(__file__).resolve().parents[1] / "CITATION.cff"

# Release versions as in the release tags, without the `v`: `X.Y.Z`, or
# `X.Y.ZaN`, `X.Y.ZbN` and `X.Y.ZrcN` for alpha, beta and release candidates
VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?",
    re.ASCII,
)


def _key(name: str) -> re.Pattern[str]:
    """Return the pattern of a top-level key's line, capturing its value.

    Args:
        name: Name of the key.

    Returns:
        The pattern; group `value` is the value without quotes.
    """
    return re.compile(
        rf"^{re.escape(name)}:[ \t]*(?P<quote>[\"']?)(?P<value>[^\"'#\n]*?)"
        r"(?P=quote)[ \t]*(?:#.*)?$",
        re.MULTILINE,
    )


def _value(text: str, name: str) -> str | None:
    """Return the value of a top-level key.

    Args:
        text: Content of the citation file.
        name: Name of the key.

    Returns:
        The value, or `None` if the key is missing.

    Raises:
        ValueError: If the key occurs more than once.
    """
    matches = list(_key(name).finditer(text))
    if len(matches) > 1:
        raise ValueError(f"'{name}' occurs more than once")
    return matches[0]["value"] if matches else None


def _check_version(version: str) -> None:
    """Check that a version is a release version.

    Args:
        version: Version, e.g., `1.2.3` or `1.2.3rc1`.

    Raises:
        ValueError: If it is not.
    """
    if VERSION.fullmatch(version) is None:
        raise ValueError(
            f"'{version}' is not a release version (e.g., 1.2.3 or 1.2.3rc1)"
        )


def set_release(text: str, version: str, date: datetime.date) -> str:
    """Return the citation file with the version and date of a release.

    Args:
        text: Content of the citation file.
        version: Release version, e.g., `1.2.3`.
        date: Release date.

    Returns:
        The content with `version` and `date-released` set; keys that are
        missing are appended.

    Raises:
        ValueError: If the version is not a release version, or a key occurs
            more than once.
    """
    _check_version(version)
    for name, value in (("version", version), ("date-released", date.isoformat())):
        line = f'{name}: "{value}"'
        if _value(text, name) is None:
            text = f"{text.rstrip(chr(10))}\n{line}\n"
        else:
            text = _key(name).sub(line, text)
    return text


def check_release(text: str, version: str, today: datetime.date) -> None:
    """Check that the citation file has the version of a release.

    Args:
        text: Content of the citation file.
        version: Release version, e.g., `1.2.3`.
        today: Current date; `date-released` must not be later.

    Raises:
        ValueError: If `version` differs or is missing, or `date-released` is
            missing, invalid or later than `today`.
    """
    actual = _value(text, "version")
    if actual != version:
        raise ValueError(
            f"'version' is '{actual}', not the release version '{version}'"
            f" (run `python scripts/citation.py set {version}`)"
        )
    released = _value(text, "date-released")
    if released is None:
        raise ValueError("'date-released' is missing")
    try:
        date = datetime.date.fromisoformat(released)
    except ValueError:
        raise ValueError(
            f"'date-released' is '{released}', not a date (YYYY-MM-DD)"
        ) from None
    if date > today:
        raise ValueError(f"'date-released' {released} is in the future")


def main(argv: Sequence[str] | None = None, path: Path = CITATION) -> int:
    """Run a command (see the module's docstring).

    Args:
        argv: Command-line arguments, without the program name; those of the
            process if `None`.
        path: Citation file.

    Returns:
        The exit status: 0 on success, 1 on errors.
    """
    parser = argparse.ArgumentParser(
        description="Set and check the release in the citation metadata."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    set_parser = commands.add_parser("set", help="set version and date")
    set_parser.add_argument("version", help="release version, e.g., 1.2.3")
    set_parser.add_argument(
        "--date",
        type=datetime.date.fromisoformat,
        default=None,
        help="release date (YYYY-MM-DD; default: today, in UTC)",
    )
    check_parser = commands.add_parser("check", help="check version and date")
    check_parser.add_argument("version", help="release version, e.g., 1.2.3")
    args = parser.parse_args(argv)
    today = datetime.datetime.now(datetime.UTC).date()
    try:
        text = path.read_text(encoding="utf-8")
        if args.command == "set":
            text = set_release(text, args.version, args.date or today)
            path.write_text(text, encoding="utf-8")
        else:
            check_release(text, args.version, today)
    except (OSError, ValueError) as error:
        print(f"::error file={path.name}::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
