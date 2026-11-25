"""Tests for the page-token helpers of `foca.utils.pagination`."""

import base64
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import bson
import mongomock
import pytest
from bson.objectid import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.collection import Collection

from foca.errors import FieldError, InvalidFieldsProblem
from foca.utils.pagination import (
    DEFAULT_PAGE_LIMIT,
    PageKey,
    TokenPage,
    find_page,
    get_page_size,
)

_ID = ObjectId("0123456789abcdef01234567")
_TIME = datetime(2026, 11, 25, 4, 5, 6, 789000, tzinfo=UTC)


class TestGetPageSize:
    """Tests for `get_page_size()`."""

    def test_default(self) -> None:
        """Without a value, the size is the default."""
        assert get_page_size(None, parameter="pageSize") == DEFAULT_PAGE_LIMIT
        assert get_page_size(None, parameter="pageSize", default=7) == 7

    @pytest.mark.parametrize(
        ("value", "expected"), [(1, 1), (50, 50), ("50", 50), ("007", 7)]
    )
    def test_values(self, value: int | str, expected: int) -> None:
        """Integers and their strings are accepted."""
        assert get_page_size(value, parameter="pageSize") == expected

    def test_maximum(self) -> None:
        """The maximum is accepted, larger sizes are not."""
        assert get_page_size(10, parameter="page_size", default=5, maximum=10) == 10
        with pytest.raises(InvalidFieldsProblem) as exc_info:
            get_page_size(11, parameter="page_size", default=5, maximum=10)
        (error,) = exc_info.value.field_errors
        assert error == FieldError(
            detail="Must be at most 10.", code="too-large", parameter="page_size"
        )

    @pytest.mark.parametrize(
        ("value", "code"),
        [
            (0, "too-small"),
            ("0", "too-small"),
            (-1, "too-small"),
            ("-1", "too-small"),
            ("not-a-number", "invalid-type"),
            ("\u0661", "invalid-type"),
            ("1" * 5000, "invalid-type"),
            ("", "invalid-type"),
            (True, "invalid-type"),
            (1.5, "invalid-type"),
            (2**63, "too-large"),
            (str(2**63), "too-large"),
        ],
    )
    def test_invalid(self, value: object, code: str) -> None:
        """Invalid sizes are rejected with the parameter, without the value."""
        with pytest.raises(InvalidFieldsProblem) as exc_info:
            get_page_size(value, parameter="pageSize")  # type: ignore[arg-type]
        (error,) = exc_info.value.field_errors
        assert (error.code, error.parameter) == (code, "pageSize")
        if isinstance(value, str) and len(value) > 1:
            assert value not in error.detail

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"default": 0},
            {"maximum": 0},
            {"default": 2**63},
            {"maximum": 2**63},
            {"maximum": 2**63 - 1},
            {"default": 11, "maximum": 10},
        ],
    )
    def test_invalid_bounds_of_app(self, kwargs: dict[str, int]) -> None:
        """Defaults and maximums of the app must allow items."""
        with pytest.raises(ValueError, match="must be from 1"):
            get_page_size(None, parameter="pageSize", **kwargs)


def _encode(data: object) -> str:
    """Encode data like a token, e.g., to fabricate one.

    Args:
        data: Data to encode.

    Returns:
        Token.
    """
    raw = json.dumps(data).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class TestPageKey:
    """Tests for `PageKey`."""

    @pytest.mark.parametrize(
        "fields",
        [(), ("",), ("_id", "_id"), ("a..b",), (".a",), ("a.",), ("$a",), ("a.$b",)],
    )
    def test_invalid_fields(self, fields: tuple[str, ...]) -> None:
        """Keys need distinct, non-empty field names."""
        with pytest.raises(ValueError, match="fields"):
            PageKey(fields)

    @pytest.mark.parametrize("fields", ["name", ["name"], (1,)])
    def test_fields_not_tuple_of_strings(self, fields: object) -> None:
        """Fields other than a tuple of strings are refused, e.g., `("name")`."""
        with pytest.raises(ValueError, match="fields"):
            PageKey(fields)  # type: ignore[arg-type]

    def test_sort(self) -> None:
        """Pages are sorted by all fields, in one direction."""
        assert PageKey(("t", "_id")).sort == [("t", ASCENDING), ("_id", ASCENDING)]
        assert PageKey(("_id",), descending=True).sort == [("_id", DESCENDING)]

    @pytest.mark.parametrize(
        "value",
        [
            _ID,
            _TIME,
            datetime(1, 1, 1, tzinfo=UTC),
            datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
            0,
            -(2**63),
            2**63 - 1,
            "",
            "näme $gt {}",
        ],
    )
    def test_round_trip(self, value: object) -> None:
        """Values of the last item come back from its token."""
        key = PageKey(("v",))
        token = key.token({"v": value})
        after = key.after(token, parameter="pageToken")
        assert after == {"v": {"$gt": value}}
        # The filter can be sent to MongoDB, which mongomock doesn't check
        bson.encode(after)

    def test_naive_times_are_utc(self) -> None:
        """Times without zone, as `pymongo` returns them, are in UTC."""
        key = PageKey(("t",))
        token = key.token({"t": _TIME.replace(tzinfo=None)})
        assert key.after(token, parameter="pageToken") == {"t": {"$gt": _TIME}}

    def test_times_in_other_zones(self) -> None:
        """Times in other zones are the same instant in UTC."""
        key = PageKey(("t",))
        token = key.token({"t": _TIME.astimezone(timezone(timedelta(hours=-5)))})
        assert key.after(token, parameter="pageToken") == {"t": {"$gt": _TIME}}

    def test_changed_unused_bits(self) -> None:
        """Tokens with other unused bits in the last character are refused."""
        key = PageKey(("v",))
        token = key.token({"v": 12})
        assert len(token) % 4
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        changed = token[:-1] + alphabet[alphabet.index(token[-1]) + 1]
        with pytest.raises(InvalidFieldsProblem):
            key.after(changed, parameter="pageToken")

    def test_token_is_opaque(self) -> None:
        """Tokens are URL-safe and don't show the values in plain text."""
        token = PageKey(("name",)).token({"name": "secret name"})
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
        assert "secret" not in token

    def test_nested_fields(self) -> None:
        """Fields may be nested (dot notation)."""
        key = PageKey(("run.id",), descending=True)
        token = key.token({"run": {"id": "abc"}})
        assert key.after(token, parameter="page_token") == {"run.id": {"$lt": "abc"}}

    def test_tie_breaker(self) -> None:
        """Later fields decide between items with the same earlier values."""
        key = PageKey(("t", "_id"), descending=True)
        token = key.token({"t": _TIME, "_id": _ID, "other": 1})
        assert key.after(token, parameter="pageToken") == {
            "$or": [{"t": {"$lt": _TIME}}, {"t": _TIME, "_id": {"$lt": _ID}}]
        }

    @pytest.mark.parametrize(
        "item",
        [
            {},
            {"v": None},
            {"v": True},
            {"v": 1.5},
            {"v": 2**63},
            {"v": -(2**63) - 1},
            {"v": {"a": 1}},
            {"v": [1]},
            {"v": "x" * 4000},
        ],
    )
    def test_unsupported_values(self, item: dict[str, Any]) -> None:
        """Items need a value of a supported type for each field."""
        with pytest.raises(ValueError, match="'v'"):
            PageKey(("v",)).token(item)

    def test_field_through_array(self) -> None:
        """Paths through arrays have no single value, so they are refused."""
        with pytest.raises(ValueError, match="array"):
            PageKey(("runs.id",)).token({"runs": [{"id": "a"}, {"id": "b"}]})

    def test_missing_nested_field(self) -> None:
        """Items without a nested field are refused."""
        with pytest.raises(ValueError, match=r"'run\.id'"):
            PageKey(("run.id",)).token({"run": "abc"})

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "a",
            "abc=",
            "abc\n",
            "ab cd",
            "+/+/",
            "A" * 5000,
            base64.urlsafe_b64encode(b"[" * 2000 + b"]" * 2000).decode()[:4096],
            base64.urlsafe_b64encode(b"\xff\xfe").decode().rstrip("="),
            base64.urlsafe_b64encode(b"not json").decode().rstrip("="),
        ],
    )
    def test_malformed_tokens(self, token: str) -> None:
        """Malformed tokens are rejected, without quoting them."""
        with pytest.raises(InvalidFieldsProblem) as exc_info:
            PageKey(("v",)).after(token, parameter="pageToken")
        (error,) = exc_info.value.field_errors
        assert (error.code, error.parameter) == ("invalid-format", "pageToken")
        if len(token) > 1:
            assert token not in error.detail

    @pytest.mark.parametrize("token", [123, b"abc", None, ["abc"]])
    def test_tokens_of_other_types(self, token: object) -> None:
        """Tokens that are not strings are rejected, e.g., from JSON bodies."""
        with pytest.raises(InvalidFieldsProblem) as exc_info:
            PageKey(("v",)).after(token, parameter="page_token")  # type: ignore[arg-type]
        (error,) = exc_info.value.field_errors
        assert (error.code, error.parameter) == ("invalid-format", "page_token")
        assert str(token) not in error.detail

    @pytest.mark.parametrize(
        "data",
        [
            {"v": 1},
            [],
            [["i", 1], ["i", 2]],
            ["i", 1],
            [["i"]],
            [["i", 1, 2]],
            [["x", 1]],
            [["i", True]],
            [["i", 1.0]],
            [["i", "1"]],
            [["i", 2**63]],
            [["s", 1]],
            [["s", None]],
            [["s", "\ud800"]],
            [["o", "0123456789ABCDEF01234567"]],
            [["o", "0123456789abcdef0123456"]],
            [["o", {"$gt": ""}]],
            [["d", "2026-11-25"]],
            [["d", False]],
            [["d", 10**18]],
            [["d", -(10**18)]],
        ],
    )
    def test_fabricated_tokens(self, data: object) -> None:
        """Well-formed tokens that the service can't have issued are rejected."""
        with pytest.raises(InvalidFieldsProblem) as exc_info:
            PageKey(("v",)).after(_encode(data), parameter="pageToken")
        (error,) = exc_info.value.field_errors
        assert (error.code, error.parameter) == ("invalid-format", "pageToken")


@pytest.fixture
def pets() -> Collection[dict[str, Any]]:
    """Collection of ten pets, three born on each day.

    Returns:
        The collection.
    """
    collection: Collection[dict[str, Any]] = mongomock.MongoClient().db.pets
    collection.insert_many(
        [{"name": f"pet{i}", "born": _TIME + timedelta(days=i // 3)} for i in range(10)]
    )
    return collection


def _names(page: TokenPage) -> list[str]:
    """Get the names of the pets on a page.

    Args:
        page: Page of pets.

    Returns:
        Names, in the order of the page.
    """
    return [pet["name"] for pet in page.items]


class TestFindPage:
    """Tests for `find_page()`."""

    KEY = PageKey(("born", "_id"), descending=True)

    def _page(
        self, pets: Collection[dict[str, Any]], token: str | None, size: int = 4
    ) -> TokenPage:
        """Get a page of pets, newest first.

        Args:
            pets: Collection of pets.
            token: Token of the page.
            size: Size of the page.

        Returns:
            The page.
        """
        return find_page(
            pets, {}, key=self.KEY, size=size, token=token, parameter="pageToken"
        )

    def test_all_pages(self, pets: Collection[dict[str, Any]]) -> None:
        """First, middle and last page have all items once, in order."""
        first = self._page(pets, None)
        middle = self._page(pets, first.next_token)
        last = self._page(pets, middle.next_token)
        assert [_names(first), _names(middle), _names(last)] == [
            ["pet9", "pet8", "pet7", "pet6"],
            ["pet5", "pet4", "pet3", "pet2"],
            ["pet1", "pet0"],
        ]
        assert first.next_token
        assert middle.next_token
        assert last.next_token is None

    def test_exactly_full_last_page(self, pets: Collection[dict[str, Any]]) -> None:
        """A full last page has no next token."""
        first = self._page(pets, None, size=5)
        assert first.next_token is not None
        last = self._page(pets, first.next_token, size=5)
        assert _names(last) == ["pet4", "pet3", "pet2", "pet1", "pet0"]
        assert last.next_token is None

    def test_empty_list(self) -> None:
        """An empty list has one empty page."""
        empty: Collection[dict[str, Any]] = mongomock.MongoClient().db.empty
        assert self._page(empty, None) == TokenPage([], None)

    def test_changes_between_pages(self, pets: Collection[dict[str, Any]]) -> None:
        """Items added or deleted between pages are not skipped or repeated."""
        first = self._page(pets, None)
        # With offsets, the next page would start at pet3, after pet4
        pets.insert_one({"name": "old", "born": _TIME - timedelta(days=1)})
        pets.delete_many({"name": {"$in": ["pet6", "pet5"]}})
        rest = self._page(pets, first.next_token, size=10)
        assert _names(rest) == ["pet4", "pet3", "pet2", "pet1", "pet0", "old"]

    def test_query_and_projection(self, pets: Collection[dict[str, Any]]) -> None:
        """Pages are filtered by the query, with the projected fields."""
        first = find_page(
            pets,
            {"name": {"$ne": "pet9"}},
            key=self.KEY,
            size=2,
            parameter="pageToken",
            projection={"name": True, "born": True},
        )
        second = find_page(
            pets,
            {"name": {"$ne": "pet8"}},
            key=self.KEY,
            size=2,
            token=first.next_token,
            parameter="pageToken",
            projection={"name": True, "born": True},
        )
        assert _names(first) == ["pet8", "pet7"]
        assert _names(second) == ["pet6", "pet5"]
        assert set(second.items[0]) == {"_id", "name", "born"}

    def test_ascending(self, pets: Collection[dict[str, Any]]) -> None:
        """Pages can be in ascending order."""
        key = PageKey(("name",))
        first = find_page(pets, {}, key=key, size=3, parameter="p")
        second = find_page(
            pets, {}, key=key, size=3, token=first.next_token, parameter="p"
        )
        assert _names(second) == ["pet3", "pet4", "pet5"]

    def test_malformed_token(self, pets: Collection[dict[str, Any]]) -> None:
        """Malformed tokens are rejected."""
        with pytest.raises(InvalidFieldsProblem) as exc_info:
            self._page(pets, "not a token")
        (error,) = exc_info.value.field_errors
        assert (error.code, error.parameter) == ("invalid-format", "pageToken")

    def test_invalid_size(self, pets: Collection[dict[str, Any]]) -> None:
        """Sizes come from `get_page_size()`, so the app is wrong if not positive."""
        with pytest.raises(ValueError, match="size"):
            self._page(pets, None, size=0)
