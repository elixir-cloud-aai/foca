"""Tests for `foca.context`."""

from collections.abc import Callable, Iterable, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from werkzeug.test import Client

from foca import context
from foca.context import RequestIdMiddleware, request_id
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
        """Valid identifiers are used and echoed."""
        response = Client(RequestIdMiddleware(_echo_app)).get(
            headers={"X-Request-ID": value}
        )
        assert response.headers["X-Request-ID"] == value
        assert response.text == value

    @pytest.mark.parametrize(
        "value", ["", "a b", "a\tb", "x" * 129, "ä", "a,b", "a\\nb", "a/b"]
    )
    def test_invalid_incoming(self, value: str) -> None:
        """Invalid identifiers are replaced."""
        response = Client(RequestIdMiddleware(_echo_app)).get(
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

    def test_not_trusted(self) -> None:
        """Without trust, incoming identifiers are replaced."""
        response = Client(RequestIdMiddleware(_echo_app, trust_incoming=False)).get(
            headers={"X-Request-ID": "abc-1"}
        )
        assert _is_generated(response.headers["X-Request-ID"])

    def test_other_header(self) -> None:
        """The name of the header can be configured."""
        response = Client(RequestIdMiddleware(_echo_app, header="X-Trace")).get(
            headers={"X-Trace": "abc-1", "X-Request-ID": "other"}
        )
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

        response = Client(RequestIdMiddleware(_app)).get(
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

        body = RequestIdMiddleware(_app)({"HTTP_X_REQUEST_ID": "abc-1"}, MagicMock())
        assert list(body) == [b"body"]
        assert seen == ["abc-1", "abc-1"]
        assert request_id() == "abc-1"
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

        body = RequestIdMiddleware(_app)({"HTTP_X_REQUEST_ID": "abc-1"}, MagicMock())
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
        """Apps echo identifiers, also for routes that don't exist."""
        app = create_connexion_app(Config()).app
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
