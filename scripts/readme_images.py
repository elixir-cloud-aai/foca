"""Make the image paths of the README absolute, for PyPI.

The README shows the logo via relative paths (e.g., `docs/images/logo.png`),
which GitHub and the documentation resolve, but PyPI does not. The release
workflow (`.github/workflows/release.yml`) therefore rewrites them to
absolute URLs, pinned to the release tag, before it builds the distributions,
so that the package's long description shows the images. Errors are printed
as GitHub annotations to standard error.

Commands:
    `rewrite <base-url> [<file>]`: rewrite the relative paths of images in
        `<file>` (default: `README.md`) in place to `<base-url>/<path>`,
        e.g., `https://raw.githubusercontent.com/<owner>/<repo>/<tag>`.
    `check [<file>]`: fail if `<file>` still has relative image paths.

Images are Markdown images (`![alt](path)`) and the `src` and `srcset`
attributes of HTML `<img>` and `<source>` tags (as used for the logo, which
has a dark mode variant). Reference-style images (`![alt][ref]`) are
handled through their definitions (`[ref]: path`). Paths with a scheme
(`https:`, `data:`), protocol-relative ones (`//host/...`) and fragments are
kept; paths that leave the repository (`../`) are an error.

Only the stdlib is used, so the workflow runs it without installing the
package. See "Versioning and releases" in `docs/contributing/index.md`.
"""

import argparse
import posixpath
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

# Paths with a scheme (`https:`, `data:`), `//host/...` and `#fragment`
_ABSOLUTE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:|//|#)")
# `![alt](path "title")` or `![alt](<path>)`; the path is group 3
_MARKDOWN_IMAGE = re.compile(r"(!\[[^\]]*\]\(\s*)(<?)(?:(?<=<)([^>]+)|([^)\s>]+))")
# `![alt][ref]` (or `![ref][]`), whose path is in a definition (`[ref]: path`);
# the label is group 1 or 2
_REFERENCE_IMAGE = re.compile(r"!\[([^\]]*)\]\[([^\]]*)\]")
# `[ref]: path "title"` or `[ref]: <path>`; the label is group 2, the path
# group 4 or 5
_DEFINITION = re.compile(
    r"^( {0,3}\[([^\]]+)\]:[ \t]*)(<?)(?:(?<=<)([^>]+)|(\S+))", re.MULTILINE
)
# `<img ...>` and `<source ...>`
_HTML_TAG = re.compile(r"<(?:img|source)\b[^>]*>", re.IGNORECASE)
# `src="..."`, `srcset='...'` or `src=a.png` (not `data-src`); the value is
# group 3, 4 or 5
_HTML_ATTRIBUTE = re.compile(
    r"""((?<![\w-])(src|srcset)\s*=\s*)(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""",
    re.IGNORECASE,
)


class ImagePathError(ValueError):
    """An image path that can't be made absolute."""


def is_relative(path: str) -> bool:
    """Return whether an image path is relative to the repository.

    Args:
        path: Image path or URL.

    Returns:
        `False` for URLs with a scheme, protocol-relative ones and
        fragments, `True` for all others (including `/path`, which GitHub
        resolves from the root of the repository).
    """
    return bool(path) and not _ABSOLUTE.match(path)


def absolute(path: str, base: str) -> str:
    """Return the absolute URL of an image path.

    Args:
        path: Image path or URL.
        base: URL that relative paths are joined to, without trailing slash.

    Returns:
        The URL: `path` itself unless it is relative.

    Raises:
        ImagePathError: If the path leaves the repository (`../`).
    """
    if not is_relative(path):
        return path
    normalized = posixpath.normpath(path.lstrip("/"))
    if normalized == ".." or normalized.startswith("../"):
        raise ImagePathError(f"Image path '{path}' leaves the repository")
    return f"{base.rstrip('/')}/{normalized}"


def _srcset(value: str, convert: Callable[[str], str]) -> str:
    """Convert the URLs of a `srcset` value (`url [descriptor], ...`)."""
    candidates = []
    # Candidates are separated by a comma and whitespace (or end the value);
    # commas inside URLs (`data:image/png;base64,...`) are kept
    for candidate in re.split(r",\s+|,$", value.strip()):
        parts = candidate.split()
        if not parts:
            continue
        parts[0] = convert(parts[0])
        candidates.append(" ".join(parts))
    return ", ".join(candidates)


def convert_images(text: str, convert: Callable[[str], str]) -> str:
    """Apply a function to the path of every image in Markdown text.

    Args:
        text: Markdown (with inline HTML).
        convert: Function that returns the new path for a path.

    Returns:
        The text with the converted paths.
    """

    def markdown(match: re.Match[str]) -> str:
        path = match[3] if match[3] is not None else match[4]
        return f"{match[1]}{match[2]}{convert(path)}"

    def attribute(match: re.Match[str]) -> str:
        value = next(group for group in match.groups()[2:] if group is not None)
        value = (
            _srcset(value, convert) if match[2].lower() == "srcset" else convert(value)
        )
        quote = "'" if match[4] is not None else '"' if match[3] is not None else ""
        return f"{match[1]}{quote}{value}{quote}"

    def tag(match: re.Match[str]) -> str:
        return _HTML_ATTRIBUTE.sub(attribute, match[0])

    def definition(match: re.Match[str]) -> str:
        if match[2].strip().lower() not in labels:
            return match[0]
        path = match[4] if match[4] is not None else match[5]
        return f"{match[1]}{match[3]}{convert(path)}"

    # Labels of reference-style images, normalized as in CommonMark
    labels = {
        (ref or alt).strip().lower() for alt, ref in _REFERENCE_IMAGE.findall(text)
    }
    text = _DEFINITION.sub(definition, text)
    return _HTML_TAG.sub(tag, _MARKDOWN_IMAGE.sub(markdown, text))


def rewrite(text: str, base: str) -> str:
    """Make the relative image paths of Markdown text absolute.

    Args:
        text: Markdown (with inline HTML).
        base: URL that relative paths are joined to.

    Returns:
        The text with absolute image paths.

    Raises:
        ImagePathError: If an image path leaves the repository.
    """
    return convert_images(text, lambda path: absolute(path, base))


def relative_paths(text: str) -> list[str]:
    """Return the relative image paths of Markdown text.

    Args:
        text: Markdown (with inline HTML).

    Returns:
        The relative image paths.
    """
    found: list[str] = []

    def collect(path: str) -> str:
        if is_relative(path):
            found.append(path)
        return path

    convert_images(text, collect)
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """Rewrite or check the image paths of a README (see the module's docstring).

    Args:
        argv: Command-line arguments, without the program name; those of the
            process if `None`.

    Returns:
        The exit status: 0 on success, 1 on errors.
    """
    parser = argparse.ArgumentParser(description="Make README image paths absolute.")
    commands = parser.add_subparsers(dest="command", required=True)
    rewrite_parser = commands.add_parser("rewrite", help="rewrite relative paths")
    rewrite_parser.add_argument("base", help="URL to join relative paths to")
    check_parser = commands.add_parser("check", help="fail on relative paths")
    for command_parser in (rewrite_parser, check_parser):
        command_parser.add_argument(
            "file", type=Path, nargs="?", default=Path("README.md")
        )
    args = parser.parse_args(argv)
    try:
        text = args.file.read_text(encoding="utf-8")
        if args.command == "rewrite":
            args.file.write_text(rewrite(text, args.base), encoding="utf-8")
            return 0
        remaining = relative_paths(text)
    except (OSError, ImagePathError) as error:
        print(f"::error::Cannot process {args.file}: {error}", file=sys.stderr)
        return 1
    for path in remaining:
        print(f"::error::Relative image path in {args.file}: {path}", file=sys.stderr)
    return 1 if remaining else 0


if __name__ == "__main__":
    sys.exit(main())
