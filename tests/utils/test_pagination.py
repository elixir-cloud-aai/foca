"""Tests for `foca.utils.pagination`."""

from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest
from flask import Flask
from werkzeug.exceptions import BadRequest

from foca.config.access import set_app_config
from foca.models.config import Config, ServerConfig
from foca.utils.pagination import (
    MAX_PAGE_LIMIT,
    Page,
    get_link_header,
    get_page,
    get_page_links,
)


class TestGetPage:
    """Tests for `get_page()`."""

    def test_defaults(self) -> None:
        """Without parameters, the first page has the default limit."""
        assert get_page() == Page(0, 100)
        assert get_page(default_limit=7) == Page(0, 7)

    @pytest.mark.parametrize(
        ("offset", "limit", "expected"),
        [
            (0, 1, Page(0, 1)),
            (20, 10, Page(20, 10)),
            ("20", "10", Page(20, 10)),
            ("007", "010", Page(7, 10)),
        ],
    )
    def test_values(self, offset: int | str, limit: int | str, expected: Page) -> None:
        """Integers and their strings are accepted."""
        assert get_page(offset, limit) == expected

    def test_largest_offset(self) -> None:
        """The largest integer of MongoDB is accepted."""
        assert get_page(2**63 - 1).offset == 2**63 - 1
        assert get_page(str(2**63 - 1)).offset == 2**63 - 1

    @pytest.mark.parametrize("kwargs", [{"default_limit": 0}, {"max_limit": -1}])
    def test_invalid_limits_of_app(self, kwargs: dict[str, int]) -> None:
        """Limits of the app must allow items."""
        with pytest.raises(ValueError, match="at least 1"):
            get_page(**kwargs)

    def test_max_limit(self) -> None:
        """Larger limits are reduced."""
        assert get_page(limit=MAX_PAGE_LIMIT + 1).limit == MAX_PAGE_LIMIT
        assert get_page(limit=11, max_limit=10).limit == 10
        assert get_page(default_limit=11, max_limit=10).limit == 10

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"offset": -1},
            {"offset": "-1"},
            {"offset": "not-a-number"},
            {"offset": "\u0661"},
            {"offset": "1" * 5000},
            {"offset": True},
            {"offset": 1.5},
            {"offset": ""},
            {"limit": 0},
            {"limit": "0"},
            {"limit": "not-a-number"},
            {"limit": False},
            {"offset": 2**63},
            {"offset": str(2**63)},
            {"limit": 2**63},
        ],
    )
    def test_invalid(self, kwargs: dict[str, object]) -> None:
        """Invalid values are rejected, without quoting them."""
        with pytest.raises(BadRequest) as exc_info:
            get_page(**kwargs)  # type: ignore[arg-type]
        name, value = next(iter(kwargs.items()))
        description = str(exc_info.value.description)
        assert description.startswith(f"'{name}' must be an integer of at least")
        if isinstance(value, str) and value not in ("", "0", "1"):
            assert value not in description


@pytest.fixture
def app() -> Iterator[Flask]:
    """App with a public URL."""
    flask_app = Flask(__name__)
    set_app_config(
        flask_app,
        Config(server=ServerConfig(public_url="https://api.example.org/prefix")),
    )
    yield flask_app


def _params(url: str) -> dict[str, list[str]]:
    """Get the query parameters of a URL."""
    return parse_qs(urlsplit(url).query)


class TestGetPageLinks:
    """Tests for `get_page_links()`."""

    def test_first_page(self, app: Flask) -> None:
        """The first page has no link to the previous one."""
        with app.test_request_context("/pets"):
            links = get_page_links(Page(0, 10), total=25)
        assert links == {
            "first": "https://api.example.org/prefix/pets?offset=0&limit=10",
            "next": "https://api.example.org/prefix/pets?offset=10&limit=10",
            "last": "https://api.example.org/prefix/pets?offset=20&limit=10",
        }

    def test_middle_page(self, app: Flask) -> None:
        """Previous pages don't start before the first item."""
        with app.test_request_context("/pets"):
            links = get_page_links(Page(5, 10), total=25)
        assert _params(links["prev"])["offset"] == ["0"]
        assert _params(links["next"])["offset"] == ["15"]

    @pytest.mark.parametrize(
        ("offset", "total", "last"),
        [(20, 25, "20"), (10, 20, "10"), (0, 10, "0"), (0, 0, "0"), (50, 25, "20")],
    )
    def test_last_page(self, app: Flask, offset: int, total: int, last: str) -> None:
        """The last page has no link to the next one."""
        with app.test_request_context("/pets"):
            links = get_page_links(Page(offset, 10), total=total)
        assert "next" not in links
        assert _params(links["last"])["offset"] == [last]

    def test_past_the_end(self, app: Flask) -> None:
        """The previous page of a page past the end has items."""
        with app.test_request_context("/pets"):
            links = get_page_links(Page(50, 10), total=25)
        assert _params(links["prev"])["offset"] == ["20"]

    def test_trailing_slash_kept(self, app: Flask) -> None:
        """Routes with and without trailing slash differ."""
        with app.test_request_context("/pets/"):
            links = get_page_links(Page(0, 10))
        assert urlsplit(links["first"]).path == "/prefix/pets/"

    def test_unknown_total(self, app: Flask) -> None:
        """Without the total, there is a next page, and no last one."""
        with app.test_request_context("/pets"):
            links = get_page_links(Page(0, 10))
        assert set(links) == {"first", "next"}

    def test_query_kept(self, app: Flask) -> None:
        """Other query parameters are kept, encoded, and the old page isn't."""
        with app.test_request_context("/pets?q=a%26b%3E&tag=x&tag=y&offset=3&limit=9"):
            links = get_page_links(Page(3, 10))
        assert _params(links["next"]) == {
            "q": ["a&b>"],
            "tag": ["x", "y"],
            "offset": ["13"],
            "limit": ["10"],
        }
        assert ">" not in links["next"]

    def test_host_of_request_ignored(self, app: Flask) -> None:
        """Links don't use the `Host` header of the request."""
        with app.test_request_context("/pets", headers={"Host": "evil.example.org"}):
            links = get_page_links(Page(0, 10))
        assert "evil" not in links["first"]

    def test_no_public_url(self) -> None:
        """Without public URL, links can't be built."""
        flask_app = Flask(__name__)
        set_app_config(flask_app, Config())
        with (
            flask_app.test_request_context("/pets"),
            pytest.raises(RuntimeError, match="public URL"),
        ):
            get_page_links(Page(0, 10))


def test_get_link_header() -> None:
    """Links are formatted as per RFC 8288."""
    assert get_link_header({"first": "https://a/?x=1", "next": "https://a/?x=2"}) == (
        '<https://a/?x=1>; rel="first", <https://a/?x=2>; rel="next"'
    )
