"""Tests of the citation metadata (`CITATION.cff`, `scripts/citation.py`)."""

import datetime
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/citation.py"

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location("citation", SCRIPT)
assert _spec and _spec.loader
citation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(citation)

DATE = datetime.date(2026, 10, 23)
TEXT = 'cff-version: 1.2.0\nversion: "0.1.0"\ndate-released: "2026-01-02"\n'


def test_repository_file() -> None:
    """The repository's citation file is the one the script edits."""
    assert citation.CITATION == ROOT / "CITATION.cff"
    text = citation.CITATION.read_text(encoding="utf-8")
    assert text.startswith("cff-version: 1.2.0\n")
    # Contributors' e-mail addresses are never published in it
    assert "email" not in text
    assert "@" not in text


def test_set_release_replaces_keys() -> None:
    """Existing keys get the new values, the rest is kept."""
    text = citation.set_release(TEXT, "1.2.3rc1", DATE)
    assert text == (
        'cff-version: 1.2.0\nversion: "1.2.3rc1"\ndate-released: "2026-10-23"\n'
    )


@pytest.mark.parametrize(
    "line",
    ["version: 0.1.0", "version: '0.1.0'", 'version: "0.1.0"  # comment'],
)
def test_set_release_value_styles(line: str) -> None:
    """Unquoted, single-quoted and commented values are replaced."""
    text = citation.set_release(f"{line}\ndate-released: 2026-01-02\n", "1.0.0", DATE)
    assert text == 'version: "1.0.0"\ndate-released: "2026-10-23"\n'


def test_set_release_appends_missing_keys() -> None:
    """Missing keys are appended, also without a final newline."""
    text = citation.set_release("cff-version: 1.2.0", "1.0.0", DATE)
    assert text == (
        'cff-version: 1.2.0\nversion: "1.0.0"\ndate-released: "2026-10-23"\n'
    )


def test_set_release_ignores_nested_keys() -> None:
    """Only top-level keys are release metadata."""
    text = citation.set_release("references:\n  - version: 0.0.1\n", "1.0.0", DATE)
    assert text.startswith("references:\n  - version: 0.0.1\n")
    assert citation.check_release(text, "1.0.0", DATE) is None


@pytest.mark.parametrize("version", ["", "v1.0.0", "1.0", "1.0.0.dev1", "01.0.0"])
def test_set_release_invalid_version(version: str) -> None:
    """Only release versions are set."""
    with pytest.raises(ValueError, match="not a release version"):
        citation.set_release(TEXT, version, DATE)


def test_set_release_duplicate_key() -> None:
    """Duplicate keys are an error."""
    with pytest.raises(ValueError, match="more than once"):
        citation.set_release(TEXT + "version: 0.2.0\n", "1.0.0", DATE)


@pytest.mark.parametrize("today", [DATE, DATE + datetime.timedelta(days=1)])
def test_check_release(today: datetime.date) -> None:
    """A file with the release version and a past or current date passes."""
    text = citation.set_release(TEXT, "1.0.0", DATE)
    assert citation.check_release(text, "1.0.0", today) is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (TEXT, "'version' is '0.1.0', not the release version '1.0.0'"),
        ("cff-version: 1.2.0\n", "'version' is 'None'"),
        ('version: "1.0.0"\n', "'date-released' is missing"),
        ('version: "1.0.0"\ndate-released: "soon"\n', "not a date"),
        ('version: "1.0.0"\ndate-released: "2026-10-24"\n', "in the future"),
    ],
)
def test_check_release_fails(text: str, message: str) -> None:
    """Wrong or missing versions and dates are errors."""
    with pytest.raises(ValueError, match=message):
        citation.check_release(text, "1.0.0", DATE)


def test_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`set` writes the file, which `check` then accepts."""
    path = tmp_path / "CITATION.cff"
    path.write_text(TEXT, encoding="utf-8")
    assert citation.main(["set", "1.0.0", "--date", "2026-01-03"], path) == 0
    assert 'version: "1.0.0"\ndate-released: "2026-01-03"\n' in path.read_text(
        encoding="utf-8"
    )
    assert citation.main(["check", "1.0.0"], path) == 0
    assert capsys.readouterr().err == ""


def test_main_default_date(tmp_path: Path) -> None:
    """`set` uses the current date (UTC) by default."""
    path = tmp_path / "CITATION.cff"
    path.write_text(TEXT, encoding="utf-8")
    before = datetime.datetime.now(datetime.UTC).date()
    assert citation.main(["set", "1.0.0"], path) == 0
    after = datetime.datetime.now(datetime.UTC).date()
    released = citation._value(path.read_text(encoding="utf-8"), "date-released")
    assert released in {before.isoformat(), after.isoformat()}


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["check", "1.0.0"], "::error file=CITATION.cff::'version' is '0.1.0'"),
        (["set", "v1.0.0"], "::error file=CITATION.cff::'v1.0.0' is not a release"),
    ],
)
def test_main_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    """Errors are GitHub annotations, and the file is left unchanged."""
    path = tmp_path / "CITATION.cff"
    path.write_text(TEXT, encoding="utf-8")
    assert citation.main(argv, path) == 1
    assert capsys.readouterr().err.startswith(message)
    assert path.read_text(encoding="utf-8") == TEXT


def test_main_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing file is an error."""
    assert citation.main(["check", "1.0.0"], tmp_path / "CITATION.cff") == 1
    assert capsys.readouterr().err.startswith("::error file=CITATION.cff::")
