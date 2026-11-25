"""Paginate lists of resources.

Offset-based pagination for controllers: :py:func:`get_page` checks the
parameters of a request, and :py:func:`get_page_links` builds links to other
pages from the public URL of the app (`server.public_url`), not from the
request, whose `Host` header clients control.

Page tokens (keyset pagination), which skip or repeat no items when items are
added or deleted between requests: :py:func:`get_page_size` checks the page
size of a request, :py:class:`PageKey` sorts by fields of the items and
issues and checks opaque tokens for the page after an item, and
:py:func:`find_page` gets a page from a MongoDB collection.
"""

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple
from urllib.parse import urlencode

from bson.objectid import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.collection import Collection

from foca.context import current_request
from foca.errors import BadRequest, FieldError, InvalidFieldsProblem
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
        foca.errors.BadRequest: The value is not an integer from
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
        foca.errors.BadRequest: `offset` is not an integer from 0, or
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
    request = current_request()
    query = [
        (name, value)
        for name in request.query
        if name not in ("offset", "limit")
        for value in request.query.getlist(name)
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


# Page sizes in strings: optional sign and at most as many digits as the
# largest value, as `int()` refuses very long strings
_INTEGER = re.compile(rf"-?[0-9]{{1,{_MAX_DIGITS}}}")
# Longest page token; longer ones are not issued, and refused unread
_MAX_TOKEN_LENGTH = 4096
# Tokens: URL-safe base64 without padding (RFC 4648, section 5)
_TOKEN = re.compile(rf"[A-Za-z0-9_-]{{1,{_MAX_TOKEN_LENGTH}}}")
_OBJECT_ID = re.compile(r"[0-9a-f]{24}")
# Times in tokens are microseconds since the epoch, in UTC
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_MIN_TIME = (datetime.min.replace(tzinfo=UTC) - _EPOCH) // _MICROSECOND
_MAX_TIME = (datetime.max.replace(tzinfo=UTC) - _EPOCH) // _MICROSECOND
_MIN_VALUE = -(2**63)
# Field names: dot notation of non-empty names that don't start with `$`
_FIELD = re.compile(r"[^.$][^.]*(?:\.[^.$][^.]*)*")


def _invalid_parameter(detail: str, code: str, parameter: str) -> InvalidFieldsProblem:
    """Get the problem of an invalid pagination parameter.

    Args:
        detail: Message, without the value.
        code: Code of the error.
        parameter: Name of the parameter.

    Returns:
        `400 Bad Request` for the parameter.
    """
    return InvalidFieldsProblem(
        [FieldError(detail=detail, code=code, parameter=parameter)]
    )


def get_page_size(
    value: int | str | None,
    *,
    parameter: str,
    default: int = DEFAULT_PAGE_LIMIT,
    maximum: int = MAX_PAGE_LIMIT,
) -> int:
    """Get the page size that a request asks for.

    Unlike :py:func:`get_page`, sizes above the maximum are refused, as pages
    with tokens have no links that would tell clients the actual size.

    Args:
        value: Page size, e.g., of a query parameter; `default` if `None`.
        parameter: Name of the parameter, for errors (e.g., `pageSize`).
        default: Size if `value` is `None`.
        maximum: Largest size.

    Returns:
        The page size.

    Raises:
        foca.errors.InvalidFieldsProblem: The size is not an integer
            (`invalid-type`), is less than 1 (`too-small`) or more than
            `maximum` (`too-large`), naming the parameter, without quoting the
            value.
        ValueError: `default` or `maximum` is less than 1, or not less than
            the largest integer of 8 bytes, or `default` is more than
            `maximum`.
    """
    # `find_page()` reads one more item, within the integers of 8 bytes
    if not 1 <= default <= maximum < _MAX_VALUE:
        raise ValueError(
            f"`default` and `maximum` must be from 1 to {_MAX_VALUE - 1}, "
            "and `default` at most `maximum`"
        )
    if value is None:
        return default
    if isinstance(value, str) and _INTEGER.fullmatch(value):
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _invalid_parameter("Must be an integer.", "invalid-type", parameter)
    if value < 1:
        raise _invalid_parameter("Must be at least 1.", "too-small", parameter)
    if value > maximum:
        raise _invalid_parameter(f"Must be at most {maximum}.", "too-large", parameter)
    return value


def _encode_value(value: object, field: str) -> list[str | int]:
    """Encode the value of a sort field for a token.

    Args:
        value: Value of the field.
        field: Name of the field, for errors.

    Returns:
        Type tag and value: `o` and the hexadecimal identifier, `d` and the
        microseconds since the epoch (times without zone are in UTC, as
        `pymongo` returns them), `i` and the integer, or `s` and the string.

    Raises:
        ValueError: The value is not an `ObjectId`, a `datetime`, an integer
            of 8 bytes or a string.
    """
    if isinstance(value, ObjectId):
        return ["o", str(value)]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return ["d", (value - _EPOCH) // _MICROSECOND]
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and _MIN_VALUE <= value <= _MAX_VALUE
    ):
        return ["i", value]
    if isinstance(value, str):
        return ["s", value]
    raise ValueError(
        f"Sort field '{field}' must hold an ObjectId, a datetime, an integer of "
        f"8 bytes or a string, not {type(value).__name__}"
    )


def _decode_value(entry: object) -> object:
    """Decode the value of a sort field from a token.

    Args:
        entry: Type tag and value (cf. :py:func:`_encode_value`).

    Returns:
        The value.

    Raises:
        ValueError: The entry is not one that :py:func:`_encode_value`
            returns (`UnicodeEncodeError` for strings with lone surrogates).
    """
    if not isinstance(entry, list) or len(entry) != 2:
        raise ValueError("not a tagged value")
    tag, value = entry
    if tag == "s" and isinstance(value, str):
        # JSON escapes can give lone surrogates, which BSON can't encode
        value.encode()
        return value
    if tag == "o" and isinstance(value, str) and _OBJECT_ID.fullmatch(value):
        return ObjectId(value)
    if isinstance(value, int) and not isinstance(value, bool):
        if tag == "i" and _MIN_VALUE <= value <= _MAX_VALUE:
            return value
        if tag == "d" and _MIN_TIME <= value <= _MAX_TIME:
            return _EPOCH + value * _MICROSECOND
    raise ValueError("not a tagged value")


def _get_field(item: Mapping[str, Any], field: str) -> object:
    """Get the value of a field of an item.

    Args:
        item: Item, e.g., a MongoDB document.
        field: Name of the field, in dot notation for nested fields.

    Returns:
        The value.

    Raises:
        ValueError: The item has no such field, or the path crosses an array
            (MongoDB sorts by an element of the array, which has no single
            value for the token).
    """
    value: object = item
    for name in field.split("."):
        if isinstance(value, list):
            raise ValueError(f"Sort field '{field}' must not cross an array")
        if not isinstance(value, Mapping) or name not in value:
            raise ValueError(f"Item has no sort field '{field}'")
        value = value[name]
    return value


@dataclass(frozen=True)
class PageKey:
    """Sort key of lists with page tokens.

    Tokens hold the values of the sort fields of the last item of a page,
    encoded (URL-safe base64 of JSON), so they are opaque to clients, but
    not secret or signed: clients may fabricate tokens, which then only
    select another starting point of the same list. Apply access control in
    the query, not by tokens.

    Args:
        fields: Names of the fields to sort by, in dot notation for nested
            fields. Items must have a value for each field that is an
            `ObjectId`, a `datetime`, an integer of 8 bytes or a string, of
            the same type in all items (MongoDB compares values of one type
            only), and the values of all fields together must be unique; end
            with a unique field as tie-breaker, e.g., `("created", "_id")`.
        descending: Whether to sort in descending order (all fields), e.g.,
            newest first.

    Attributes:
        fields: Names of the fields to sort by.
        descending: Whether to sort in descending order.

    Raises:
        ValueError: The fields are a string instead of a tuple, there are no
            fields, a field name is empty, has empty parts or starts with
            `$`, or fields are named twice.

    Examples:
        >>> key = PageKey(("name",))
        >>> key.after(key.token({"name": "Rex"}), parameter="pageToken")
        {'name': {'$gt': 'Rex'}}
    """

    fields: tuple[str, ...]
    descending: bool = False

    def __post_init__(self) -> None:
        """Check the fields."""
        if (
            # A string, e.g., `("name")`, would sort by its characters
            isinstance(self.fields, str)
            or not self.fields
            or len(set(self.fields)) != len(self.fields)
            or not all(_FIELD.fullmatch(field) for field in self.fields)
        ):
            raise ValueError(
                "Page keys need distinct fields with non-empty names in dot "
                f"notation that don't start with '$': {self.fields}"
            )

    @property
    def sort(self) -> list[tuple[str, int]]:
        """Sort specification for `pymongo` (`Cursor.sort()`)."""
        direction = DESCENDING if self.descending else ASCENDING
        return [(field, direction) for field in self.fields]

    def token(self, item: Mapping[str, Any]) -> str:
        """Get the token of the page after an item.

        Args:
            item: Last item of a page, with all sort fields.

        Returns:
            Opaque token.

        Raises:
            ValueError: The item lacks a sort field, a value has a type that
                isn't supported (cf. `fields`), or the token would be longer
                than 4096 characters (e.g., for long strings).
        """
        values = [_encode_value(_get_field(item, f), f) for f in self.fields]
        raw = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        token = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        if len(token) > _MAX_TOKEN_LENGTH:
            raise ValueError(
                f"Values of the sort fields {self.fields} are too long for a page token"
            )
        return token

    def after(self, token: str, *, parameter: str) -> dict[str, Any]:
        """Get the MongoDB filter for the items after a token.

        Args:
            token: Token of a request (cf. :py:meth:`token`).
            parameter: Name of the parameter, for errors (e.g., `pageToken`).

        Returns:
            Filter for the items after the item of the token, in the sort
            order.

        Raises:
            foca.errors.InvalidFieldsProblem: The token is not one that
                :py:meth:`token` returns for this key, or not a string
                (`invalid-format`), naming the parameter, without quoting the
                token.
        """
        try:
            # Tokens from JSON bodies may not be strings
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise ValueError("not a token")
            padding = "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(token + padding)
            # Unused bits of the last character must be 0, as `token()` sets
            if base64.urlsafe_b64encode(raw).decode().rstrip("=") != token:
                raise ValueError("not a canonical token")
            entries = json.loads(raw)
            if not isinstance(entries, list) or len(entries) != len(self.fields):
                raise ValueError("not a token of this key")
            values = [_decode_value(entry) for entry in entries]
        except (ValueError, binascii.Error, RecursionError):
            # `UnicodeDecodeError` and `json.JSONDecodeError` are
            # `ValueError`s; `RecursionError` for deeply nested JSON (Python
            # 3.11)
            raise _invalid_parameter(
                "Must be a page token that this service returned.",
                "invalid-format",
                parameter,
            ) from None
        operator = "$lt" if self.descending else "$gt"
        conditions = [
            {
                **dict(zip(self.fields[:index], values[:index], strict=True)),
                self.fields[index]: {operator: values[index]},
            }
            for index in range(len(self.fields))
        ]
        return conditions[0] if len(conditions) == 1 else {"$or": conditions}


class TokenPage(NamedTuple):
    """Page of a list with page tokens.

    Attributes:
        items: Items of the page.
        next_token: Token of the next page; `None` on the last page.
    """

    items: list[dict[str, Any]]
    next_token: str | None


def find_page(
    collection: Collection[dict[str, Any]],
    query: Mapping[str, Any],
    *,
    key: PageKey,
    size: int,
    parameter: str,
    token: str | None = None,
    projection: Mapping[str, Any] | None = None,
) -> TokenPage:
    """Get a page of the items of a MongoDB collection.

    Reads one item more than the page has, to tell whether a next page has
    items, so that the last page has no next token.

    Args:
        collection: Collection of the items.
        query: Filter of the list, e.g., the items the user may see.
        key: Sort key.
        size: Largest number of items on the page (cf.
            :py:func:`get_page_size`).
        parameter: Name of the token parameter, for errors (e.g.,
            `pageToken`).
        token: Token of the page; the first page if `None`. An empty token
            is invalid, as no page has one.
        projection: Fields of the items to return; must include the sort
            fields.

    Returns:
        The page.

    Raises:
        foca.errors.InvalidFieldsProblem: The token is malformed (cf.
            :py:meth:`PageKey.after`).
        ValueError: `size` is less than 1 or not less than the largest
            integer of 8 bytes, or the last item of a page that isn't the last
            one has no valid token (cf. :py:meth:`PageKey.token`: missing or
            unsupported sort values, or values too long for a token).
    """
    # One more item than the page is read, within the integers of 8 bytes
    if not 1 <= size < _MAX_VALUE:
        raise ValueError(f"Page size must be from 1 to {_MAX_VALUE - 1}: {size}")
    conditions = [dict(query)] if query else []
    if token is not None:
        conditions.append(key.after(token, parameter=parameter))
    if len(conditions) > 1:
        filter_ = {"$and": conditions}
    else:
        filter_ = conditions[0] if conditions else {}
    found = list(collection.find(filter_, projection).sort(key.sort).limit(size + 1))
    items = found[:size]
    next_token = key.token(items[-1]) if len(found) > size else None
    return TokenPage(items, next_token)
