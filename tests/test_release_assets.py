"""Tests of the release state and asset check (`scripts/release_assets.py`)."""

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release_assets.py"

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location("release_assets", SCRIPT)
assert _spec and _spec.loader
release_assets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_assets)

TAG = "v1.2.3"
# A digest that no file has
ZERO = "sha256:" + "0" * 64


def _asset(
    path: Path, digest: str | None = None, state: str = "uploaded"
) -> dict[str, Any]:
    """Return an asset of a file as the GitHub API returns it.

    Args:
        path: Local file of the asset.
        digest: Digest of the asset; that of the file if `None`.
        state: State of the upload.

    Returns:
        The asset.
    """
    if digest is None:
        digest = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    return {"name": path.name, "digest": digest, "state": state}


def _release(
    tag: str = TAG,
    draft: bool = False,
    assets: list[dict[str, Any]] | None = None,
    immutable: bool = True,
) -> dict[str, Any]:
    """Return a release as the GitHub API returns it.

    Args:
        tag: Tag of the release.
        draft: Whether it is a draft.
        assets: Its assets.
        immutable: Whether it is immutable once published (drafts never are).

    Returns:
        The release.
    """
    return {
        "tag_name": tag,
        "draft": draft,
        "immutable": immutable and not draft,
        "assets": assets or [],
    }


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    """Return the local assets of a release.

    Args:
        tmp_path: Temporary directory.

    Returns:
        A chart and its Sigstore bundle.
    """
    chart = tmp_path / "chart-1.2.3.tgz"
    chart.write_bytes(b"chart")
    bundle = tmp_path / "chart-1.2.3.tgz.sigstore.json"
    bundle.write_text("{}")
    return [chart, bundle]


def test_read_releases_pages() -> None:
    """Pages of releases (`--slurp`) and flat lists give the releases."""
    pages = json.dumps([[{"tag_name": "v1"}, {"tag_name": "v2"}], [{"tag_name": "v3"}]])
    assert [r["tag_name"] for r in release_assets.read_releases(pages)] == [
        "v1",
        "v2",
        "v3",
    ]
    assert release_assets.read_releases('[{"tag_name": "v1"}]') == [{"tag_name": "v1"}]
    assert release_assets.read_releases("[]") == []
    assert release_assets.read_releases("[[]]") == []


@pytest.mark.parametrize("data", ["{}", '"x"', "[1]", "[[1]]", "not json"])
def test_read_releases_invalid(data: str) -> None:
    """Anything but (pages of) release objects is rejected."""
    with pytest.raises(ValueError):
        release_assets.read_releases(data)


def test_state_none(files: list[Path]) -> None:
    """Without a release of the tag, one is to be created."""
    releases = [_release("v1.2.2"), _release("v1.2.4", draft=True)]
    assert release_assets.release_state(TAG, releases, files) == ("none", [], [])


def test_state_draft(files: list[Path]) -> None:
    """A single draft (with any assets) is to be finished."""
    releases = [_release("v1.2.2"), _release(draft=True, assets=[_asset(files[0])])]
    assert release_assets.release_state(TAG, releases, files) == ("draft", [], [])


def test_state_several_drafts(files: list[Path]) -> None:
    """Several drafts of the tag are an error (which one to publish?)."""
    releases = [_release(draft=True), _release(draft=True)]
    state, errors, warnings = release_assets.release_state(TAG, releases, files)
    assert state is None
    assert len(errors) == 1 and "2 draft releases" in errors[0]
    assert warnings == []


def test_state_published_complete(files: list[Path]) -> None:
    """A published release with all assets needs no upload."""
    releases = [_release(assets=[_asset(path) for path in files])]
    assert release_assets.release_state(TAG, releases, files) == (
        "published",
        [],
        [],
    )


def test_state_published_without_digests(files: list[Path]) -> None:
    """Assets without a digest (older uploads) count as changed."""
    assets = [_asset(path) | {"digest": None} for path in files]
    state, errors, warnings = release_assets.release_state(
        TAG, [_release(assets=assets)], files
    )
    assert state is None
    assert len(errors) == 1
    assert f"other versions of {files[0].name} than" in errors[0]
    assert len(warnings) == 1 and files[1].name in warnings[0]


def test_state_published_unexpected(files: list[Path]) -> None:
    """Assets that the run did not build are an error."""
    extra = files[0].with_name("extra.txt")
    extra.write_text("extra")
    assets = [_asset(path) for path in [*files, extra]]
    state, errors, warnings = release_assets.release_state(
        TAG, [_release(assets=assets)], files
    )
    assert state is None
    assert len(errors) == 1
    assert "assets that this run did not build (extra.txt)" in errors[0]
    assert warnings == []


def test_state_published_resigned(files: list[Path]) -> None:
    """Sigstore bundles with another digest are kept, with a warning."""
    assets = [_asset(files[0]), _asset(files[1], digest=ZERO)]
    state, errors, warnings = release_assets.release_state(
        TAG, [_release(assets=assets)], files
    )
    assert (state, errors) == ("published", [])
    assert warnings == [
        f"Release {TAG} is published with other signatures ({files[1].name})"
        " than this run made (every attempt signs anew); they are kept"
    ]


@pytest.mark.parametrize(
    ("immutable", "remedy"),
    [(True, "release a new patch version"), (False, "gh release delete v1.2.3")],
)
def test_state_published_changed(
    files: list[Path], immutable: bool, remedy: str
) -> None:
    """Other assets with another digest are an error, with the remedy."""
    assets = [_asset(files[0], digest=ZERO), _asset(files[1], digest=ZERO)]
    state, errors, warnings = release_assets.release_state(
        TAG, [_release(assets=assets, immutable=immutable)], files
    )
    assert state is None
    assert len(errors) == 1
    assert f"other versions of {files[0].name} than this run built" in errors[0]
    assert remedy in errors[0]
    assert len(warnings) == 1 and files[1].name in warnings[0]


@pytest.mark.parametrize(
    ("immutable", "remedy"),
    [
        (True, "release a new patch version"),
        (False, "gh release delete v1.2.3"),
    ],
)
def test_state_published_missing(
    files: list[Path], immutable: bool, remedy: str
) -> None:
    """A published release without all assets is an error, with the remedy."""
    # An upload that did not finish does not count
    assets = [_asset(files[0], state="starter")]
    state, errors, warnings = release_assets.release_state(
        TAG, [_release(assets=assets, immutable=immutable)], files
    )
    assert state is None
    assert len(errors) == 1
    assert files[0].name in errors[0] and files[1].name in errors[0]
    assert remedy in errors[0]
    assert warnings == []


def test_state_published_missing_and_changed(files: list[Path]) -> None:
    """Missing and changed assets are separate errors, in this order."""
    chart = files[0].with_name("chart-1.2.3.tgz.prov")
    chart.write_bytes(b"provenance")
    assets = [_asset(files[0], digest=ZERO), _asset(files[1])]
    state, errors, warnings = release_assets.release_state(
        TAG, [_release(assets=assets)], [*files, chart]
    )
    assert state is None
    assert len(errors) == 2
    assert "without the assets chart-1.2.3.tgz.prov:" in errors[0]
    assert f"other versions of {files[0].name} than" in errors[1]
    assert warnings == []


def test_state_published_and_drafts(files: list[Path]) -> None:
    """Drafts next to a published release are an error (which one to edit?)."""
    releases = [
        _release(draft=True),
        _release(assets=[_asset(path) for path in files]),
        _release(draft=True),
    ]
    state, errors, warnings = release_assets.release_state(TAG, releases, files)
    assert state is None
    assert len(errors) == 1 and "drafts of it are left (2)" in errors[0]
    assert warnings == []


def _main(tmp_path: Path, files: list[Path], releases: str | None) -> int:
    """Run `main` on releases.

    Args:
        tmp_path: Temporary directory, for the file of releases.
        files: Local assets.
        releases: JSON of the releases; no file if `None`.

    Returns:
        The exit status.
    """
    path = tmp_path / "releases.json"
    if releases is not None:
        path.write_text(releases)
    return release_assets.main(
        ["state", TAG, "--releases", str(path), *map(str, files)]
    )


def test_main_state(
    tmp_path: Path, files: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The state is printed as a `GITHUB_OUTPUT` line."""
    assert _main(tmp_path, files, json.dumps([[_release(draft=True)]])) == 0
    assert capsys.readouterr() == ("state=draft\n", "")


def test_main_warning(
    tmp_path: Path, files: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Warnings are printed as annotations, and don't fail."""
    assets = [_asset(files[0]), _asset(files[1], digest=ZERO)]
    assert _main(tmp_path, files, json.dumps([_release(assets=assets)])) == 0
    captured = capsys.readouterr()
    assert captured.out == "state=published\n"
    assert captured.err.startswith(f"::warning::Release {TAG} is published with")


def test_main_error(
    tmp_path: Path, files: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Errors are printed as annotations, after warnings, and fail."""
    assets = [_asset(files[1], digest=ZERO)]
    assert _main(tmp_path, files, json.dumps([_release(assets=assets)])) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert [line.split("::")[1] for line in lines] == ["warning", "error"]


@pytest.mark.parametrize(
    ("releases", "message"),
    [(None, "No such file"), ("{}", "Releases are not a JSON list")],
)
def test_main_invalid_releases(
    tmp_path: Path,
    files: list[Path],
    capsys: pytest.CaptureFixture[str],
    releases: str | None,
    message: str,
) -> None:
    """Releases that can't be read or parsed fail with an annotation."""
    assert _main(tmp_path, files, releases) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"::error::Cannot check release {TAG}: ")
    assert message in captured.err
