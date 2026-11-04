"""Paginate lists of resources.

Offset-based pagination for controllers: :py:func:`get_page` checks the
parameters of a request, and :py:func:`get_page_links` builds links to other
pages from the public URL of the app (`server.public_url`), not from the
request, whose `Host` header clients control.
"""

from typing import NamedTuple
from urllib.parse import urlencode

from flask import request
from werkzeug.exceptions import BadRequest

from foca.utils.urls import get_public_url

# Items per page unless set otherwise, and the most that clients can ask for
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000
# Largest value, as MongoDB has 8-byte integers
_MAX_VALUE = 2**63 - 1
# Longest strings that are converted, as `int()` refuses very long ones
_MAX_DIGITS = len(str(_MAX_VALUE))


class Page(NamedTuple):
    """Page of a list.

    Attributes:
        offset: Number of items before the page.
        limit: Largest number of items on the page.
    """

    offset: int
    limit: int


def _get_integer(value: int | str, name: str, minimum: int) -> int:
    """Get the integer value of a pagination parameter.

    Args:
        value: Value, e.g., of a query parameter.
        name: Name of the parameter, for the error message.
        minimum: Smallest allowed value.

    Returns:
        Value as an integer.

    Raises:
        werkzeug.exceptions.BadRequest: The value is not an integer from
            `minimum` to the largest one of 8 bytes. The message doesn't quote
            the value.
    """
    if isinstance(value, str) and (
        value.isascii() and value.isdigit() and len(value) <= _MAX_DIGITS
    ):
        value = int(value)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= _MAX_VALUE
    ):
        raise BadRequest(f"'{name}' must be an integer from {minimum} to {_MAX_VALUE}.")
    return value


def get_page(
    offset: int | str | None = None,
    limit: int | str | None = None,
    *,
    default_limit: int = DEFAULT_PAGE_LIMIT,
    max_limit: int = MAX_PAGE_LIMIT,
) -> Page:
    """Get the page that a request asks for.

    Args:
        offset: Number of items to skip; 0 if `None`.
        limit: Largest number of items to return; `default_limit` if `None`.
            Larger values than `max_limit` are reduced to it, so that clients
            learn the actual limit from the links.
        default_limit: Limit if `limit` is `None`.
        max_limit: Largest limit.

    Returns:
        The page.

    Raises:
        werkzeug.exceptions.BadRequest: `offset` is not an integer from 0, or
            `limit` from 1, to the largest one of 8 bytes.
        ValueError: `default_limit` or `max_limit` is less than 1, or more than
            the largest integer of 8 bytes.
    """
    if not (1 <= default_limit <= _MAX_VALUE and 1 <= max_limit <= _MAX_VALUE):
        raise ValueError(
            f"`default_limit` and `max_limit` must be from 1 to {_MAX_VALUE}"
        )
    return Page(
        offset=0 if offset is None else _get_integer(offset, "offset", 0),
        limit=min(
            default_limit if limit is None else _get_integer(limit, "limit", 1),
            max_limit,
        ),
    )


def get_page_links(page: Page, total: int | None = None) -> dict[str, str]:
    """Get the links to other pages of the list of the current request.

    Must be called in a request context of an app with a public URL. Links
    have the path and the other query parameters of the request.

    Args:
        page: Current page.
        total: Number of items of the list, if known. Without it, there is no
            link to the last page, and a link to the next page even if the
            current page is the last one.

    Returns:
        Public URLs by relation (RFC 8288): `first`, `prev` (not on the first
        page), `next` (not on the last page) and `last` (if `total` is given).

    Raises:
        RuntimeError: The app has no public URL (`server.public_url`), or
            there is no request context.
    """
    query = [
        (name, value)
        for name, value in request.args.items(multi=True)
        if name not in ("offset", "limit")
    ]
    url = get_public_url(request.path)
    # Removed by `get_public_url()`, but routes differ by it
    if request.path.endswith("/") and not url.endswith("/"):
        url = f"{url}/"

    def link(offset: int) -> str:
        """Get the link to the page at an offset."""
        params = [*query, ("offset", offset), ("limit", page.limit)]
        return f"{url}?{urlencode(params)}"

    last = None if total is None else max(total - 1, 0) // page.limit * page.limit
    links = {"first": link(0)}
    if page.offset > 0:
        previous = max(page.offset - page.limit, 0)
        # Not an empty page, if the current one starts past the end
        links["prev"] = link(previous if last is None else min(previous, last))
    following = page.offset + page.limit
    if following <= _MAX_VALUE and (total is None or following < total):
        links["next"] = link(following)
    if last is not None:
        links["last"] = link(last)
    return links


def get_link_header(links: dict[str, str]) -> str:
    """Format links to pages as the value of a `Link` header (RFC 8288).

    Args:
        links: Public URLs by relation (cf. :py:func:`get_page_links`).

    Returns:
        Value of the header.
    """
    return ", ".join(f'<{url}>; rel="{rel}"' for rel, url in links.items())
