"""Set and check the release in the citation metadata (`CITATION.cff`).

Zenodo archives each GitHub release with the `CITATION.cff` of the tagged
commit, so `version` and `date-released` are set before the release is
tagged, and the release workflow checks them. Errors are printed as GitHub
annotations to standard error.

Commands:
    `set <version> [--date YYYY-MM-DD]`: set `version` and `date-released`
        (default: today, in UTC; not a later date), adding the keys if they
        are missing.
    `check <version>`: check that `version` is the release version and that
        `date-released` is a date (YYYY-MM-DD) that is not in the future
        (UTC).

Only the stdlib is used, so the workflows run it without installing the
package. Both keys must be top-level keys with plain or quoted single-line
values (as `set` writes them); the rest of the file is kept as is. The
schema itself is checked by `cffconvert --validate` in CI.
"""

import argparse
import datetime
import os
import re
import stat
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

CITATION = Path(__file__).resolve().parents[1] / "CITATION.cff"

# Release versions as in the release tags (checked by the release workflow
# before this script runs), without the `v`: `X.Y.Z`, or `X.Y.ZaN`,
# `X.Y.ZbN` and `X.Y.ZrcN` for alpha, beta and release candidates
VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?",
    re.ASCII,
)

# Dates as the CFF schema requires them (`date.fromisoformat` accepts more)
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def _key(name: str) -> re.Pattern[str]:
    """Return the pattern of a top-level key's line, capturing its value.

    Args:
        name: Name of the key.

    Returns:
        The pattern of a line with the key and a plain or quoted value of
        letters, digits and `._+-` (as versions and dates have), and
        optionally a comment; group `value` is the value without quotes.
    """
    return re.compile(
        rf"^{re.escape(name)}:[ \t]*(?P<quote>[\"']?)(?P<value>[0-9A-Za-z._+-]+)"
        # Not continued on the next line (as YAML continues plain scalars)
        r"(?P=quote)(?:[ \t]+#.*)?[ \t]*$(?!\n[ \t]+[^ \t#\n])",
        re.MULTILINE,
    )


def _any_key(name: str) -> re.Pattern[str]:
    """Return the pattern of any top-level line with a key, quoted or not.

    Args:
        name: Name of the key.

    Returns:
        The pattern.
    """
    escaped = re.escape(name)
    return re.compile(rf"^(?:{escaped}|\"{escaped}\"|'{escaped}')[ \t]*:", re.MULTILINE)


def _value(text: str, name: str) -> str | None:
    """Return the value of a top-level key.

    Args:
        text: Content of the citation file.
        name: Name of the key.

    Returns:
        The value, or `None` if the key is missing.

    Raises:
        ValueError: If the key occurs more than once, or its value is not a
            plain or quoted single-line value.
    """
    lines = _any_key(name).findall(text)
    if len(lines) > 1:
        raise ValueError(f"'{name}' occurs more than once")
    match = _key(name).search(text)
    if lines and match is None:
        raise ValueError(
            f"'{name}' has an unsupported format: use `{name}: \"<value>\"`"
        )
    return match["value"] if match else None


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


def _parse_date(value: str) -> datetime.date:
    """Return the date of a value in the format YYYY-MM-DD.

    Args:
        value: Date, e.g., `2026-10-23`.

    Returns:
        The date.

    Raises:
        ValueError: If the value is not a valid date in that format.
    """
    try:
        if DATE.fullmatch(value) is None:
            raise ValueError(value)
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"'{value}' is not a date (YYYY-MM-DD)") from None


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
            more than once or has an unsupported format.
    """
    _check_version(version)
    for name, value in (("version", version), ("date-released", date.isoformat())):
        line = f'{name}: "{value}"'
        if _value(text, name) is None:
            body = text.rstrip("\n")
            text = f"{body}\n{line}\n"
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
        ValueError: If the version is not a release version, `version`
            differs or is missing, or `date-released` is missing, invalid or
            later than `today`.
    """
    _check_version(version)
    actual = _value(text, "version")
    if actual != version:
        raise ValueError(
            f"'version' is '{actual}', not the release version '{version}'"
            f" (run `python scripts/citation.py set {version}`)"
        )
    released = _value(text, "date-released")
    if released is None:
        raise ValueError("'date-released' is missing")
    if _parse_date(released) > today:
        raise ValueError(f"'date-released' {released} is in the future (UTC)")


def _write(path: Path, text: str) -> None:
    """Replace a file atomically, so that it is never left half-written.

    The temporary file next to it gets a new, unpredictable name, so that
    no existing file or link is written to and concurrent writes don't share
    it, and the permissions of the file.

    Args:
        path: File, which must exist.
        text: New content.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
        type=_parse_date,
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
            date = args.date or today
            # `check` would reject it
            if date > today:
                raise ValueError(f"'{date.isoformat()}' is in the future (UTC)")
            text = set_release(text, args.version, date)
            _write(path, text)
        else:
            check_release(text, args.version, today)
    except (OSError, ValueError) as error:
        print(f"::error file={path.name}::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
