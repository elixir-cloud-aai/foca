"""Find the GitHub release of a tag and check the assets of a published one.

Used by the release workflow (`.github/workflows/release.yml`), which
creates the release as a draft, uploads the assets and publishes it last:
assets of a published immutable release can't be added, replaced or
deleted. Prints a `GITHUB_OUTPUT` line (`state=<state>`) to standard
output, and errors and warnings as GitHub annotations to standard error.
See "Versioning and releases" in `docs/contributing/index.md`.

Command:
    `state <tag> --releases <file> <asset>...`: the state of the release of
        `<tag>` among the releases in `<file>` (the JSON of
        `gh api --paginate --slurp "repos/<repo>/releases?per_page=100"`,
        drafts included): `none` (no release yet: create a draft), `draft`
        (a draft of an earlier attempt: upload the assets and publish it) or
        `published` (upload nothing). A published release must have all
        `<asset>` files already, with the same digest (SHA-256), and no
        others, or the command fails, explaining what to do; only Sigstore
        bundles
        (`*.sigstore.json`), which every attempt makes anew, may differ
        (with a warning). Drafts are an error if there are several, as it is
        unclear which one to publish, or if the tag has a published release
        too, as `gh release edit <tag>` might pick a draft.

Only the stdlib is used, so the workflow runs it without installing the app.
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

# Sigstore bundles of assets, which every attempt of a release makes anew
SIGNATURE_SUFFIX = ".sigstore.json"


def read_releases(data: str) -> list[dict[str, Any]]:
    """Return the releases of the JSON that `gh api` printed.

    Args:
        data: JSON of a list of releases, or of a list of pages (lists) of
            releases, as `gh api --paginate --slurp` prints them.

    Returns:
        The releases.

    Raises:
        ValueError: If the JSON is invalid or not a list of (pages of)
            releases.
    """
    parsed = json.loads(data)
    if not isinstance(parsed, list):
        raise ValueError("Releases are not a JSON list")
    releases: list[Any] = []
    for item in parsed:
        releases.extend(item if isinstance(item, list) else [item])
    if not all(isinstance(release, dict) for release in releases):
        raise ValueError("Releases are not JSON objects")
    return releases


def sha256(path: Path) -> str:
    """Return the digest of a file, as GitHub reports those of assets.

    Args:
        path: File.

    Returns:
        `sha256:<hex>`.
    """
    with path.open("rb") as file:
        return f"sha256:{hashlib.file_digest(file, 'sha256').hexdigest()}"


def check_published(
    tag: str, release: dict[str, Any], assets: Iterable[Path]
) -> tuple[list[str], list[str]]:
    """Check that a published release has all assets.

    Args:
        tag: Git tag of the release.
        release: The release, as the GitHub API returns it.
        assets: Local files that the release must have, by name.

    Returns:
        Errors (assets that are missing, have another or no digest than
        the local file, or are unexpected, with what to do) and warnings
        (Sigstore bundles with another digest); both empty if the release
        has exactly the assets, with the same digests.
    """
    uploaded = {
        asset.get("name"): asset.get("digest")
        for asset in release.get("assets") or []
        if asset.get("state") == "uploaded"
    }
    missing: list[str] = []
    changed: list[str] = []
    resigned: list[str] = []
    paths = list(assets)
    for path in paths:
        if path.name not in uploaded:
            missing.append(path.name)
        elif uploaded[path.name] != sha256(path):
            signature = path.name.endswith(SIGNATURE_SUFFIX)
            (resigned if signature else changed).append(path.name)
    names = {path.name for path in paths}
    # Of all assets, whatever their state (an unfinished upload is visible too)
    unexpected = sorted(
        str(asset.get("name"))
        for asset in release.get("assets") or []
        if asset.get("name") not in names
    )
    if release.get("immutable"):
        remedy = (
            "assets of a published immutable release can't be added or"
            " replaced, so release a new patch version (tags can't be reused)"
        )
    else:
        remedy = (
            f"delete the release (gh release delete {tag}; not the tag),"
            " then re-run this job, which creates it anew"
        )
    errors = []
    if missing:
        errors.append(
            f"Release {tag} is published without the assets"
            f" {', '.join(missing)}: {remedy}"
        )
    if changed:
        errors.append(
            f"Release {tag} is published with other versions of"
            f" {', '.join(changed)} than this run built (of an earlier attempt"
            f" or uploaded by hand): {remedy}"
        )
    if unexpected:
        errors.append(
            f"Release {tag} is published with assets that this run did not"
            f" build ({', '.join(unexpected)}): {remedy}"
        )
    warnings = []
    if resigned:
        warnings.append(
            f"Release {tag} is published with other signatures"
            f" ({', '.join(resigned)}) than this run made (every attempt signs"
            " anew); they are kept"
        )
    return errors, warnings


def release_state(
    tag: str, releases: Iterable[dict[str, Any]], assets: Iterable[Path]
) -> tuple[str | None, list[str], list[str]]:
    """Return the state of the release of a tag.

    Args:
        tag: Git tag of the release.
        releases: Releases of the repository, drafts included.
        assets: Local files that the release must have.

    Returns:
        The state (`none`, `draft` or `published`; `None` on errors), errors
        and warnings.
    """
    published: list[dict[str, Any]] = []
    drafts: list[dict[str, Any]] = []
    for release in releases:
        if release.get("tag_name") != tag:
            continue
        (drafts if release.get("draft") else published).append(release)
    if published and drafts:
        # `gh release edit <tag>` might pick a draft
        return (
            None,
            [
                f"Release {tag} is published, but drafts of it are left"
                f" ({len(drafts)}): delete them, then re-run this job"
            ],
            [],
        )
    if published:
        errors, warnings = check_published(tag, published[0], assets)
        return (None if errors else "published"), errors, warnings
    if len(drafts) > 1:
        return (
            None,
            [
                f"{len(drafts)} draft releases for {tag}: delete all but one"
                " (or all of them), then re-run this job"
            ],
            [],
        )
    return ("draft" if drafts else "none"), [], []


def main(argv: Sequence[str] | None = None) -> int:
    """Print the state of a release (see the module's docstring).

    Args:
        argv: Command-line arguments, without the program name; those of the
            process if `None`.

    Returns:
        The exit status: 0 on success, 1 on errors (see the module's
        docstring) or if the releases or an asset can't be read.
    """
    parser = argparse.ArgumentParser(
        description="Find the release of a tag and check a published one."
    )
    parser.add_argument("command", choices=["state"])
    parser.add_argument("tag", help="release tag, e.g., v1.2.3")
    parser.add_argument(
        "--releases",
        type=Path,
        required=True,
        metavar="FILE",
        help="releases of the repository (JSON of `gh api --paginate --slurp`)",
    )
    parser.add_argument("assets", type=Path, nargs="+", help="assets to attach")
    args = parser.parse_args(argv)
    try:
        releases = read_releases(args.releases.read_text(encoding="utf-8"))
        state, errors, warnings = release_state(args.tag, releases, args.assets)
    except (OSError, ValueError) as error:
        print(f"::error::Cannot check release {args.tag}: {error}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"::warning::{warning}", file=sys.stderr)
    for error_message in errors:
        print(f"::error::{error_message}", file=sys.stderr)
    if state is None:
        return 1
    print(f"state={state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
