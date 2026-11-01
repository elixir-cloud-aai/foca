"""Tests of the README image path rewriting (`scripts/readme_images.py`)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/readme_images.py"

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location("readme_images", SCRIPT)
assert _spec and _spec.loader
readme_images = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(readme_images)

BASE = "https://raw.githubusercontent.com/some-org/some-repo/v1.2.3"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/images/logo.png", True),
        ("./logo.png", True),
        ("/docs/logo.png", True),
        ("https://example.org/logo.png", False),
        ("http://example.org/logo.png", False),
        ("data:image/png;base64,AAAA", False),
        ("//example.org/logo.png", False),
        ("#logo", False),
        ("", False),
    ],
)
def test_is_relative(path: str, expected: bool) -> None:
    """Only paths without a scheme (and not `//` or `#`) are relative."""
    assert readme_images.is_relative(path) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/logo.png", f"{BASE}/docs/logo.png"),
        ("./docs/logo.png", f"{BASE}/docs/logo.png"),
        ("docs/../logo.png", f"{BASE}/logo.png"),
        ("https://example.org/logo.png", "https://example.org/logo.png"),
    ],
)
def test_absolute(path: str, expected: str) -> None:
    """Relative paths are joined to the base, others are kept."""
    assert readme_images.absolute(path, f"{BASE}/") == expected


@pytest.mark.parametrize("path", ["../logo.png", "docs/../../logo.png", ".."])
def test_absolute_outside_repository(path: str) -> None:
    """Paths that leave the repository are an error."""
    with pytest.raises(readme_images.ImagePathError, match="leaves the repository"):
        readme_images.absolute(path, BASE)


def test_rewrite_markdown() -> None:
    """Markdown images, with a title or in angle brackets, are rewritten."""
    text = (
        '![a](docs/a.png) ![b](<docs/b.png>) ![c](docs/c.png "C") ![d](http://x/d.png)'
    )
    assert readme_images.rewrite(text, BASE) == (
        f"![a]({BASE}/docs/a.png) ![b](<{BASE}/docs/b.png>) "
        f'![c]({BASE}/docs/c.png "C") ![d](http://x/d.png)'
    )


def test_rewrite_html() -> None:
    """`src` and `srcset` of `img` and `source` tags are rewritten."""
    text = (
        "<picture>\n"
        '  <source media="(prefers-color-scheme: dark)" srcset="docs/night.png">\n'
        '  <img src=\'docs/day.png\' alt="x" srcset="a.png 1x, b.png 2x, '
        'https://x/c.png 3x">\n'
        "</picture>"
    )
    assert readme_images.rewrite(text, BASE) == (
        "<picture>\n"
        '  <source media="(prefers-color-scheme: dark)"'
        f' srcset="{BASE}/docs/night.png">\n'
        f'  <img src=\'{BASE}/docs/day.png\' alt="x" srcset="{BASE}/a.png 1x,'
        f' {BASE}/b.png 2x, https://x/c.png 3x">\n'
        "</picture>"
    )


def test_rewrite_leaves_other_text() -> None:
    """Links, non-image tags and plain text are kept as they are."""
    text = '[doc](docs/index.md) <a href="docs/x.png">x</a> docs/y.png <img alt="n">'
    assert readme_images.rewrite(text, BASE) == text


def test_relative_paths() -> None:
    """All relative paths of images are found, and only those."""
    text = '![a](a.png) ![b](https://x/b.png)\n<img src="c.png"><source srcset="d.png">'
    assert readme_images.relative_paths(text) == ["a.png", "c.png", "d.png"]


def test_readme_has_no_relative_image_paths_after_rewrite() -> None:
    """The rewritten README of the repository has no relative image paths."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme_images.relative_paths(text)  # the test is meaningful
    rewritten = readme_images.rewrite(text, BASE)
    assert readme_images.relative_paths(rewritten) == []
    assert f"{BASE}/docs/images/" in rewritten


def test_main_rewrite_and_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rewrite` makes `check` pass; `check` fails before."""
    readme = tmp_path / "README.md"
    readme.write_text("![a](docs/a.png)\n", encoding="utf-8")
    assert readme_images.main(["check", str(readme)]) == 1
    assert "Relative image path" in capsys.readouterr().err
    assert readme_images.main(["rewrite", BASE, str(readme)]) == 0
    assert readme.read_text(encoding="utf-8") == f"![a]({BASE}/docs/a.png)\n"
    assert readme_images.main(["check", str(readme)]) == 0


def test_main_default_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default file is `README.md` of the current directory."""
    (tmp_path / "README.md").write_text("![a](a.png)", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert readme_images.main(["rewrite", BASE]) == 0
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == f"![a]({BASE}/a.png)"


def test_main_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Missing files and paths outside the repository are errors."""
    assert readme_images.main(["check", str(tmp_path / "missing.md")]) == 1
    readme = tmp_path / "README.md"
    readme.write_text("![a](../a.png)", encoding="utf-8")
    assert readme_images.main(["rewrite", BASE, str(readme)]) == 1
    assert "leaves the repository" in capsys.readouterr().err


def test_rewrite_srcset_with_empty_candidate() -> None:
    """Empty candidates of a `srcset` (e.g., a trailing comma) are dropped."""
    assert readme_images.rewrite('<img srcset="a.png 1x, ,">', BASE) == (
        f'<img srcset="{BASE}/a.png 1x">'
    )


def test_rewrite_unquoted_and_data_attributes() -> None:
    """Unquoted attributes are rewritten; `data-src` and data URIs are not."""
    text = '<img src=a.png data-src="b.png" srcset="data:image/png;base64,AAAA 1x">'
    assert readme_images.rewrite(text, BASE) == (
        f'<img src={BASE}/a.png data-src="b.png"'
        ' srcset="data:image/png;base64,AAAA 1x">'
    )


def test_rewrite_angle_bracket_path_with_space() -> None:
    """A path in angle brackets may contain spaces."""
    assert readme_images.rewrite("![a](<my file.png>)", BASE) == (
        f"![a](<{BASE}/my file.png>)"
    )


def test_reference_style_images() -> None:
    """Definitions of reference-style images are rewritten, others are not."""
    text = (
        "![a][ref] ![Other][] [![b][badge]][link]\n\n"
        '[ref]: docs/a.png "A"\n[other]: <docs/o.png>\n'
        "[badge]: https://x/b.svg\n[link]: docs/index.md\n"
    )
    rewritten = readme_images.rewrite(text, BASE)
    assert rewritten == text.replace("docs/a.png", f"{BASE}/docs/a.png").replace(
        "docs/o.png", f"{BASE}/docs/o.png"
    )
    assert readme_images.relative_paths(text) == ["docs/a.png", "docs/o.png"]
    assert readme_images.relative_paths(rewritten) == []


def test_rewrite_srcset_without_space_after_comma() -> None:
    """Candidates may be separated by a comma alone, or end with one."""
    assert readme_images.rewrite('<img srcset="a.png 1x,b.png 2x,c.png,">', BASE) == (
        f'<img srcset="{BASE}/a.png 1x, {BASE}/b.png 2x, {BASE}/c.png">'
    )


def test_rewrite_tag_with_gt_in_attribute() -> None:
    """A `>` inside a quoted attribute value does not end the tag."""
    assert readme_images.rewrite('<img alt="a > b" src="a.png">', BASE) == (
        f'<img alt="a > b" src="{BASE}/a.png">'
    )


def test_absolute_with_leading_slash() -> None:
    """Paths with a leading slash are an error (not relative to the README)."""
    with pytest.raises(readme_images.ImagePathError, match="starts with a slash"):
        readme_images.absolute("/docs/logo.png", BASE)


def test_rewrite_shortcut_reference_image() -> None:
    """`![logo]` with a definition `[logo]: path` is an image."""
    text = "![logo]\n\n[logo]: docs/a.png\n"
    assert (
        readme_images.rewrite(text, BASE) == f"![logo]\n\n[logo]: {BASE}/docs/a.png\n"
    )


def test_main_not_utf8(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A README that is not UTF-8 is an error, not a traceback."""
    readme = tmp_path / "README.md"
    readme.write_bytes(b"\xff")
    assert readme_images.main(["check", str(readme)]) == 1
    assert "Cannot process" in capsys.readouterr().err


def test_rewrite_reference_labels_are_normalized() -> None:
    """Labels match regardless of case and whitespace runs."""
    text = "![a][My  Logo]\n\n[my logo]: a.png\n"
    assert readme_images.rewrite(text, BASE) == (
        f"![a][My  Logo]\n\n[my logo]: {BASE}/a.png\n"
    )
