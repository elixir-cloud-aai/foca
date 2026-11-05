"""Tests for `foca.context`."""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import pytest
from flask import Flask
from pydantic import ValidationError
from werkzeug.test import Client

from foca import context
from foca.context import Headers, RequestIdMiddleware, current_request, request_id
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, RequestIdConfig, ServerConfig


def _echo_app(
    environ: dict[str, Any], start_response: Callable[..., Any]
) -> Iterable[bytes]:
    """WSGI application responding with the identifier of the request.

    Args:
        environ: WSGI environment.
        start_response: Function starting the response.

    Returns:
        Identifier of the request.
    """
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [request_id().encode()]


def _is_generated(value: str) -> bool:
    """Check whether an identifier looks generated.

    Args:
        value: Identifier.

    Returns:
        Whether it's a hexadecimal string of 32 characters.
    """
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


class TestMiddleware:
    """Tests for `RequestIdMiddleware`."""

    @pytest.mark.parametrize("value", ["abc-1", "a.b_c:d-E", "x" * 128])
    def test_valid_incoming(self, value: str) -> None:
        """Valid identifiers are used and echoed, if trusted."""
        response = Client(RequestIdMiddleware(_echo_app, trust_incoming=True)).get(
            headers={"X-Request-ID": value}
        )
        assert response.headers["X-Request-ID"] == value
        assert response.text == value

    @pytest.mark.parametrize(
        "value", ["", "a b", "a\tb", "x" * 129, "ä", "a,b", "a\\nb", "a/b"]
    )
    def test_invalid_incoming(self, value: str) -> None:
        """Invalid identifiers are replaced, even if trusted."""
        response = Client(RequestIdMiddleware(_echo_app, trust_incoming=True)).get(
            headers={"X-Request-ID": value}
        )
        assert _is_generated(response.headers["X-Request-ID"])
        assert response.text == response.headers["X-Request-ID"]

    def test_missing(self) -> None:
        """Requests without an identifier get one, each its own."""
        client = Client(RequestIdMiddleware(_echo_app))
        first = client.get().headers["X-Request-ID"]
        second = client.get().headers["X-Request-ID"]
        assert _is_generated(first)
        assert first != second

    @pytest.mark.parametrize("kwargs", [{}, {"trust_incoming": False}])
    def test_not_trusted(self, kwargs: dict[str, Any]) -> None:
        """By default or without trust, incoming identifiers are replaced."""
        response = Client(RequestIdMiddleware(_echo_app, **kwargs)).get(
            headers={"X-Request-ID": "abc-1"}
        )
        assert _is_generated(response.headers["X-Request-ID"])
        assert response.text == response.headers["X-Request-ID"]

    def test_other_header(self) -> None:
        """The name of the header can be configured."""
        response = Client(
            RequestIdMiddleware(_echo_app, header="X-Trace", trust_incoming=True)
        ).get(headers={"X-Trace": "abc-1", "X-Request-ID": "other"})
        assert response.headers["X-Trace"] == "abc-1"
        assert response.text == "abc-1"
        assert "X-Request-ID" not in response.headers

    def test_replaces_header_of_app(self) -> None:
        """The response has one header with the identifier, in any case."""

        def _app(
            environ: dict[str, Any], start_response: Callable[..., Any]
        ) -> Iterable[bytes]:
            """Respond with a header of the same name."""
            start_response("200 OK", [("x-request-id", "of-app")])
            return []

        response = Client(RequestIdMiddleware(_app, trust_incoming=True)).get(
            headers={"X-Request-ID": "abc-1"}
        )
        assert response.headers.getlist("X-Request-ID") == ["abc-1"]

    def test_passes_exc_info(self) -> None:
        """Further arguments of `start_response` are passed on."""

        def _app(
            environ: dict[str, Any], start_response: Callable[..., Any]
        ) -> Iterable[bytes]:
            """Start a response with `exc_info`."""
            start_response("500 ERROR", [], None)
            return []

        start_response = MagicMock()
        list(RequestIdMiddleware(_app)({}, start_response))
        assert start_response.call_args.args[2] is None

    def test_available_while_streaming(self) -> None:
        """The identifier is available until the response is closed."""
        seen = []

        def _app(
            environ: dict[str, Any], start_response: Callable[..., Any]
        ) -> Iterator[bytes]:
            """Stream a response."""
            start_response("200 OK", [])
            seen.append(request_id())
            yield b"body"
            seen.append(request_id())

        body = RequestIdMiddleware(_app, trust_incoming=True)(
            {"HTTP_X_REQUEST_ID": "abc-1"}, MagicMock()
        )
        assert list(body) == [b"body"]
        assert seen == ["abc-1", "abc-1"]
        assert request_id() == "abc-1"
        body.close()  # type: ignore[attr-defined]
        assert request_id() == ""

    def test_body_without_close(self) -> None:
        """Bodies need no `close()`."""
        body = RequestIdMiddleware(MagicMock(return_value=[b"body"]))({}, MagicMock())
        assert list(body) == [b"body"]
        body.close()  # type: ignore[attr-defined]
        assert request_id() == ""

    def test_cleared_on_error(self) -> None:
        """The identifier doesn't outlive a failed request."""
        with pytest.raises(ZeroDivisionError):
            RequestIdMiddleware(MagicMock(side_effect=ZeroDivisionError))(
                {}, MagicMock()
            )
        assert request_id() == ""

    def test_cleared_if_body_is_invalid(self) -> None:
        """The identifier doesn't outlive a response that isn't iterable."""
        app = MagicMock(return_value=1)
        with pytest.raises(TypeError):
            RequestIdMiddleware(app)({}, MagicMock())
        assert request_id() == ""

    def test_cleared_if_closing_fails(self) -> None:
        """The identifier doesn't outlive a body failing to close."""
        response_body = MagicMock()
        response_body.__iter__.return_value = iter([b"body"])
        response_body.close.side_effect = ZeroDivisionError
        body = RequestIdMiddleware(MagicMock(return_value=response_body))(
            {}, MagicMock()
        )
        assert list(body) == [b"body"]
        with pytest.raises(ZeroDivisionError):
            body.close()  # type: ignore[attr-defined]
        assert request_id() == ""

    def test_nested(self) -> None:
        """A request made during another keeps the identifier of the outer one."""
        inner = RequestIdMiddleware(_echo_app, trust_incoming=False)
        seen = []

        def _app(
            environ: dict[str, Any], start_response: Callable[..., Any]
        ) -> Iterable[bytes]:
            """Call another app."""
            Client(inner).get().close()
            seen.append(request_id())
            start_response("200 OK", [])
            return []

        body = RequestIdMiddleware(_app, trust_incoming=True)(
            {"HTTP_X_REQUEST_ID": "abc-1"}, MagicMock()
        )
        assert seen == ["abc-1"]
        body.close()  # type: ignore[attr-defined]

    def test_file_wrapper_passed_on(self) -> None:
        """File wrappers of servers stay as they are."""

        class _FileWrapper:
            """File wrapper of a server."""

        wrapper = _FileWrapper()
        body = RequestIdMiddleware(MagicMock(return_value=wrapper))(
            {"wsgi.file_wrapper": _FileWrapper}, MagicMock()
        )
        assert body is wrapper
        assert request_id() == ""


class TestRequestId:
    """Tests for `request_id()`."""

    def test_outside_of_requests(self) -> None:
        """Outside of requests, it's empty."""
        assert request_id() == ""

    def test_celery_task(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In a Celery task, it's the identifier of the task."""
        task = MagicMock()
        task.request.id = "task-1"
        monkeypatch.setattr(context, "current_task", task)
        assert request_id() == "task-1"

    def test_celery_task_without_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tasks called directly have no identifier."""
        task = MagicMock()
        task.request.id = None
        monkeypatch.setattr(context, "current_task", task)
        assert request_id() == ""


class TestApp:
    """Tests of the middleware in FOCA apps."""

    def test_default(self) -> None:
        """Apps don't take identifiers from clients by default."""
        app = create_connexion_app(Config()).app
        response = app.test_client().get("/missing", headers={"X-Request-ID": "abc-1"})
        assert response.status_code == 404
        assert _is_generated(response.headers["X-Request-ID"])

    def test_trusted(self) -> None:
        """Apps that opt in echo identifiers, also for routes that don't exist."""
        config = Config(
            server=ServerConfig(request_id=RequestIdConfig(trust_incoming=True))
        )
        app = create_connexion_app(config).app
        response = app.test_client().get("/missing", headers={"X-Request-ID": "abc-1"})
        assert response.status_code == 404
        assert response.headers["X-Request-ID"] == "abc-1"

    def test_config(self) -> None:
        """The settings are taken from the configuration."""
        config = Config(
            server=ServerConfig(
                request_id=RequestIdConfig(header="X-Trace", trust_incoming=False)
            )
        )
        response = (
            create_connexion_app(config)
            .app.test_client()
            .get("/missing", headers={"X-Trace": "abc-1"})
        )
        assert _is_generated(response.headers["X-Trace"])

    @pytest.mark.parametrize("header", ["", "X Trace", "X-Trace\r\nA: b", "X_Trace"])
    def test_invalid_header_name(self, header: str) -> None:
        """Names of headers are checked."""
        with pytest.raises(ValidationError):
            RequestIdConfig(header=header)


def view_request(**kwargs: Any) -> dict[str, Any]:
    """Serve an operation with the snapshot of the current request.

    Args:
        **kwargs: Parameters of the operation.

    Returns:
        Fields of the snapshot.
    """
    snapshot = current_request()
    return {
        "method": snapshot.method,
        "path": snapshot.path,
        "root_path": snapshot.root_path,
        "url": snapshot.url,
        "headers": snapshot.headers.getlist("x-test"),
        "content_length": snapshot.content_length,
        "client_address": snapshot.client_address,
        "route": snapshot.route,
        "path_params": dict(snapshot.path_params),
    }


def _request_spec() -> dict[str, Any]:
    """Build a specification with an operation returning the current request.

    Returns:
        OpenAPI 3 specification, with base path `/api/v1`.
    """
    operation = {
        "operationId": f"{__name__}.view_request",
        "responses": {"200": {"description": "OK"}},
    }
    return {
        "openapi": "3.0.3",
        "info": {"title": "Test", "version": "1"},
        "servers": [{"url": "/api/v1"}],
        "paths": {
            "/items/{item-id}/{count}": {
                "parameters": [
                    {
                        "name": name,
                        "in": "path",
                        "required": True,
                        "schema": {"type": schema_type},
                    }
                    for name, schema_type in (
                        ("item-id", "string"),
                        ("count", "integer"),
                    )
                ],
                "post": operation,
            },
        },
    }


@pytest.fixture
def request_app() -> Flask:
    """Create an app with an operation returning the current request.

    Returns:
        Flask app.
    """
    cnx_app = create_connexion_app()
    cnx_app.add_api(_request_spec())
    return cnx_app.app


class TestCurrentRequest:
    """Tests for `current_request()`."""

    def test_in_request(self, request_app: Flask) -> None:
        """Controllers get the request, below the root path, with its route."""
        response = request_app.test_client().post(
            "/api/v1/items/a%20b/3",
            base_url="http://example.org/root/",
            headers=[("x-TEST", "one")],
            environ_base={"REMOTE_ADDR": "203.0.113.5"},
            json={"name": "x"},
        )
        assert response.status_code == 200
        assert response.json == {
            "method": "POST",
            "path": "/api/v1/items/a b/3",
            "root_path": "/root",
            "url": "http://example.org/root/api/v1/items/a%20b/3",
            "headers": ["one"],
            "content_length": 13,
            "client_address": "203.0.113.5",
            "route": "/api/v1/items/{item-id}/{count}",
            "path_params": {"item_id": "a b", "count": 3},
        }

    def test_without_root_path(self, request_app: Flask) -> None:
        """Without a root path, the path is the whole path."""
        with request_app.test_request_context("/api/v1/items/a/1", method="POST"):
            snapshot = current_request()
        assert snapshot.root_path == ""
        assert snapshot.path == "/api/v1/items/a/1"
        assert snapshot.content_length is None

    def test_head(self, request_app: Flask) -> None:
        """`HEAD` requests to paths without `GET` operations have no route."""
        with request_app.test_request_context("/api/v1/items/a/1", method="HEAD"):
            snapshot = current_request()
        assert snapshot.method == "HEAD"
        assert snapshot.route is None

    def test_no_route(self, request_app: Flask) -> None:
        """Requests that no operation serves have no route."""
        with request_app.test_request_context("/other", method="GET"):
            snapshot = current_request()
        assert snapshot.route is None
        assert snapshot.path_params == {}

    def test_headers(self, request_app: Flask) -> None:
        """Headers are case-insensitive and read-only."""
        headers = [("X-Test", "one"), ("Accept", "text/plain")]
        with request_app.test_request_context("/", headers=headers):
            snapshot = current_request()
        assert snapshot.headers["X-TEST"] == "one"
        assert snapshot.headers.get("accept") == "text/plain"
        assert "Accept" in snapshot.headers
        assert 1 not in snapshot.headers
        assert snapshot.headers.get("missing") is None
        assert snapshot.headers.getlist("missing") == []
        assert snapshot.headers.getlist(1) == []  # type: ignore[arg-type]  # invalid
        assert {"x-test", "accept"} <= set(snapshot.headers)
        assert len(snapshot.headers) == len(set(snapshot.headers))
        assert "x-test" in repr(snapshot.headers)
        with pytest.raises(TypeError):
            snapshot.headers["X-Test"] = "three"  # type: ignore[index]  # read-only
        with pytest.raises(TypeError):
            snapshot.path_params["x"] = 1  # type: ignore[index]  # read-only
        with pytest.raises(FrozenInstanceError):
            snapshot.path = "/x"  # type: ignore[misc]  # frozen

    def test_snapshot(self, request_app: Flask) -> None:
        """Snapshots don't change with the request."""
        with request_app.test_request_context(
            "/api/v1/items/a/1", method="POST", headers=[("X-Test", "one")]
        ) as ctx:
            snapshot = current_request()
            ctx.request.environ["HTTP_X_TEST"] = "two"
            ctx.request.environ["PATH_INFO"] = "/other"
            assert current_request().headers["x-test"] == "two"
        assert snapshot.headers["x-test"] == "one"
        assert snapshot.path == "/api/v1/items/a/1"

    def test_headers_from_pairs(self) -> None:
        """Headers keep all values, in order."""
        headers = Headers([("A", "1"), ("b", "2"), ("a", "3")])
        assert headers.getlist("a") == ["1", "3"]
        assert list(headers) == ["a", "b"]
        assert dict(headers) == {"a": "1", "b": "2"}
        assert headers == {"a": "1", "b": "2"}

    def test_url_encoded(self, request_app: Flask) -> None:
        """URLs are percent-encoded, also non-ASCII characters."""
        with request_app.test_request_context("/api/v1/items/%E2%82%AC/1?q=%C3%BC"):
            snapshot = current_request()
        assert snapshot.url == "http://localhost/api/v1/items/%E2%82%AC/1?q=%C3%BC"
        assert snapshot.path == "/api/v1/items/\u20ac/1"

    def test_url_invalid_escapes(self, request_app: Flask) -> None:
        """Percent signs that don't start escapes are percent-encoded."""
        with request_app.test_request_context("/a%2525b%zz?x=%zz&y=%&z=%2B%41"):
            snapshot = current_request()
        assert snapshot.url == "http://localhost/a%2525b%25zz?x=%25zz&y=%25&z=%2B%41"

    @pytest.mark.parametrize("host", ["a..b", "a" * 64 + ".org"])
    def test_url_host_as_sent(self, request_app: Flask, host: str) -> None:
        """Hosts are kept, also those that can't be IDNA-encoded."""
        with request_app.test_request_context("/x/%E2%82%AC", headers=[("Host", host)]):
            snapshot = current_request()
        assert snapshot.url == f"http://{host}/x/%E2%82%AC"

    def test_repr_without_credentials(self, request_app: Flask) -> None:
        """Representations leave out header values and the URL."""
        headers = [("Authorization", "Bearer secret"), ("Cookie", "s=secret")]
        with request_app.test_request_context(
            "/api/v1/items/secret/1?token=secret", method="POST", headers=headers
        ):
            snapshot = current_request()
        text = repr(snapshot)
        assert "secret" not in text
        assert "authorization" in text
        assert "/api/v1/items/{item-id}/{count}" in text

    def test_outside_request(self, request_app: Flask) -> None:
        """Outside requests, a clear error is raised."""
        with pytest.raises(RuntimeError, match="only while a request"):
            current_request()
        with request_app.app_context(), pytest.raises(RuntimeError):
            current_request()


class TestRequestKey:
    """Tests for `_get_request_key()`."""

    def test_per_request(self) -> None:
        """Each request has its own key, the same throughout the request."""
        app = Flask(__name__)
        assert context._get_request_key() is None
        with app.app_context():
            assert context._get_request_key() is None
            with app.test_request_context():
                key = context._get_request_key()
                assert key is not None
                assert context._get_request_key() is key
            with app.test_request_context():
                assert context._get_request_key() is not key


class TestTokenInfo:
    """Tests for `_get_token_info()`."""

    def test_outside_request(self) -> None:
        """Outside requests, there is no token info."""
        assert context._get_token_info() == {}
