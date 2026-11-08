"""Tests for the logging utilties module."""

import logging

import pytest
from connexion import App
from flask import Flask, Response

from foca.api.routes import _install_route_table
from foca.testing import Client, request_context
from foca.utils import logging as module
from foca.utils.logging import (
    _UNKNOWN_ROUTE,
    escape_log_value,
    log_traffic,
    log_traffic_if,
)

app = Flask(__name__)
CLIENT_ADDRESS = "192.168.1.1"

# Get logger instance
logger = logging.getLogger(__name__)


def test_logging_decorator(caplog: pytest.LogCaptureFixture) -> None:
    """Verify the default settings work properly."""
    caplog.set_level(logging.INFO)

    @log_traffic
    def mock_func() -> dict[str, str]:
        return {"foo": "bar"}

    with request_context(client_address=CLIENT_ADDRESS, app=app):
        mock_func()
    assert "Incoming request" in caplog.text and "Response to request" in caplog.text


def test_logging_decorator_log_level(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that the 'log_level argument modifies the log level properly."""

    @log_traffic(log_level=30)
    def mock_func() -> dict[str, str]:
        return {"foo": "bar"}

    with request_context(client_address=CLIENT_ADDRESS, app=app):
        mock_func()
    assert "WARNING" in caplog.text


def test_logging_decorator_req_only(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that only the request gets logged."""
    caplog.set_level(logging.INFO)

    @log_traffic(log_response=False)
    def mock_func() -> dict[str, str]:
        return {"foo": "bar"}

    with request_context(client_address=CLIENT_ADDRESS, app=app):
        mock_func()
    assert (
        "Incoming request" in caplog.text and "Response to request" not in caplog.text
    )


def test_logging_decorator_res_only(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that only the response gets logged."""
    caplog.set_level(logging.INFO)

    @log_traffic(log_request=False)
    def mock_func() -> dict[str, str]:
        return {"foo": "bar"}

    with request_context(client_address=CLIENT_ADDRESS, app=app):
        mock_func()
    assert (
        "Incoming request" not in caplog.text and "Response to request" in caplog.text
    )


@log_traffic
def get_items() -> str:
    """Controller of test API."""
    return "ok"


@log_traffic
def get_item(item_id: str) -> str:
    """Controller of test API, with a path parameter."""
    return item_id


@pytest.fixture
def debugs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Messages logged by `log_traffic` at `DEBUG`."""
    messages: list[str] = []
    monkeypatch.setattr(
        module.logger, "debug", lambda msg, *args: messages.append(msg % args)
    )
    return messages


def _items_app() -> App:
    """App of the test API, with and without path parameters.

    It records its routes, as apps created by FOCA do.
    """
    app = App(__name__, skip_error_handlers=True)
    _install_route_table(app)
    app.add_api(
        {
            "openapi": "3.0.2",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/items": {
                    "get": {
                        "operationId": f"{__name__}.get_items",
                        "responses": {"200": {"description": "OK"}},
                    }
                },
                "/items/{item_id}": {
                    "get": {
                        "operationId": f"{__name__}.get_item",
                        "parameters": [
                            {
                                "name": "item_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "OK"}},
                    }
                },
            },
        }
    )
    return app


def test_logging_decorator_in_app(
    monkeypatch: pytest.MonkeyPatch, debugs: list[str]
) -> None:
    """Requests and responses of a running app are logged."""
    messages: list[str] = []
    monkeypatch.setattr(
        module.logger, "log", lambda level, msg, *args: messages.append(msg % args)
    )
    response = Client(_items_app()).get("/items")
    assert response.status_code == 200
    assert messages == [
        'Incoming request: "GET /items HTTP/1.1" from 127.0.0.1',
        'Response to request "GET /items HTTP/1.1" from 127.0.0.1: str, status 200',
    ]
    assert debugs == ['Path of request "GET /items HTTP/1.1" from 127.0.0.1: /items']


def test_logging_decorator_route(logged: list[str], debugs: list[str]) -> None:
    """The route is logged instead of the path, which is logged at `DEBUG`."""
    response = Client(_items_app()).get("/items/s3cr3t")
    assert response.status_code == 200
    request = '"GET /items/{item_id} HTTP/1.1" from 127.0.0.1'
    assert logged == [
        f"Incoming request: {request}",
        f"Response to request {request}: str, status 200",
    ]
    assert debugs == [f"Path of request {request}: /items/s3cr3t"]


@pytest.mark.parametrize(
    "log_request,log_response,count",
    [(True, False, 1), (False, True, 1), (False, False, 0)],
)
def test_logging_decorator_path_once(
    debugs: list[str], log_request: bool, log_response: bool, count: int
) -> None:
    """The path is logged once if the request or response is logged."""

    @log_traffic(log_request=log_request, log_response=log_response)
    def mock_func() -> str:
        return "ok"

    with request_context("/items/s3cr3t", client_address=CLIENT_ADDRESS, app=app):
        mock_func()
    assert len(debugs) == count


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Messages logged by `log_traffic`."""
    messages: list[str] = []
    monkeypatch.setattr(
        module.logger, "log", lambda level, msg, *args: messages.append(msg % args)
    )
    return messages


SECRET_BODY = {"token": "s3cr3t"}


@pytest.mark.parametrize(
    "response,description",
    [
        (SECRET_BODY, "dict, status 200"),
        ((SECRET_BODY, 201), "dict, status 201"),
        ((SECRET_BODY, 201, {"X-Header": "x"}), "dict, status 201"),
        ((SECRET_BODY,), "dict, status 200"),
        ((SECRET_BODY, "201 CREATED"), "dict, status 201"),
        ((SECRET_BODY, "418"), "dict, status 418"),
        ((SECRET_BODY, "teapot"), "dict, status teapot"),
        ((SECRET_BODY, ""), "dict, status "),
        ((SECRET_BODY, {"X-Header": "x"}), "dict, status 200"),
        ((), "tuple, status 200"),
        (Response("s3cr3t", status=202), "Response, status 202"),
        (None, "NoneType, status 200"),
    ],
)
def test_logging_decorator_no_body(
    logged: list[str], response: object, description: str
) -> None:
    """Response bodies are not logged by default."""

    @log_traffic
    def mock_func() -> object:
        return response

    with request_context(client_address=CLIENT_ADDRESS, app=app):
        mock_func()
    assert logged[1].endswith(f": {description}")
    assert "s3cr3t" not in " ".join(logged)


def test_logging_decorator_body(logged: list[str]) -> None:
    """Response bodies are logged if requested."""

    @log_traffic(log_response_body=True)
    def mock_func() -> dict[str, str]:
        return {"foo": "bar\nbaz"}

    with request_context(client_address=CLIENT_ADDRESS, app=app):
        mock_func()
    assert logged[1].endswith(": dict, status 200: {'foo': 'bar\\nbaz'}")


def test_logging_decorator_control_characters(
    logged: list[str], debugs: list[str]
) -> None:
    """Encoded line breaks in paths are escaped.

    Without a route, a placeholder is logged instead of the path at `INFO`.
    """

    @log_traffic
    def mock_func() -> str:
        return "ok"

    with request_context(
        "/items%0d%0aFAKE%20log%20line", client_address=CLIENT_ADDRESS, app=app
    ):
        mock_func()
    assert len(logged) == 2
    assert all("FAKE" not in msg for msg in logged)
    assert f'"GET {_UNKNOWN_ROUTE} HTTP/1.1"' in logged[0]
    assert len(debugs) == 1
    assert "\n" not in debugs[0] and "\r" not in debugs[0]
    assert debugs[0].endswith(": /items\\r\\nFAKE log line")


@pytest.mark.parametrize("enabled", [True, False])
def test_log_traffic_if(logged: list[str], enabled: bool) -> None:
    """Traffic is logged only if enabled, as decided on each request."""
    calls: list[None] = []
    runs: list[str] = []

    def is_enabled() -> bool:
        calls.append(None)
        return enabled

    @log_traffic_if(is_enabled)
    def mock_func(value: str) -> str:
        runs.append(value)
        return value

    with request_context(client_address=CLIENT_ADDRESS, app=app):
        assert mock_func("ok") == "ok"
        assert mock_func("ok") == "ok"
    assert len(calls) == 2
    assert runs == ["ok", "ok"]
    assert len(logged) == (4 if enabled else 0)


def test_log_traffic_if_wrapped() -> None:
    """The decorated function is kept as `__wrapped__`, with its metadata."""

    def mock_func(value: str) -> str:
        """Mock controller."""
        return value

    decorated = log_traffic_if(lambda: True)(mock_func)
    # set by `functools.wraps()`, unknown to the type of `decorated`
    assert decorated.__wrapped__ is mock_func  # type: ignore[attr-defined]
    assert decorated.__doc__ == "Mock controller."
    assert decorated.__name__ == "mock_func"


def test_log_traffic_if_no_request_context(logged: list[str]) -> None:
    """Without a request context, the function is called without logging.

    Whether to log is not decided either.
    """

    def is_enabled() -> bool:
        raise AssertionError("called")

    runs: list[None] = []

    @log_traffic_if(is_enabled)
    def mock_func() -> str:
        runs.append(None)
        return "ok"

    assert mock_func() == "ok"
    with app.app_context():
        assert mock_func() == "ok"
    assert len(runs) == 2
    assert logged == []


def test_log_traffic_if_options(logged: list[str]) -> None:
    """Options are passed to `log_traffic`."""

    @log_traffic_if(lambda: True, log_request=False, log_response_body=True)
    def mock_func() -> str:
        return "ok"

    with request_context(client_address=CLIENT_ADDRESS, app=app):
        mock_func()
    assert len(logged) == 1
    assert logged[0].startswith("Response to request")
    assert logged[0].endswith(": str, status 200: ok")


def test_log_traffic_if_log_level(caplog: pytest.LogCaptureFixture) -> None:
    """The log level is passed to `log_traffic`."""

    @log_traffic_if(lambda: True, log_level=logging.WARNING)
    def mock_func() -> str:
        return "ok"

    with request_context(client_address=CLIENT_ADDRESS, app=app):
        mock_func()
    assert "WARNING" in caplog.text


@pytest.mark.parametrize(
    "value,expected",
    [
        ("plain text", "plain text"),
        ("a\r\nb", "a\\r\\nb"),
        ("tab\there", "tab\\there"),
        ("\x1b[31mred", "\\x1b[31mred"),
        ("ünïcödé", "ünïcödé"),
        (42, "42"),
    ],
)
def test_escape_log_value(value: object, expected: str) -> None:
    """Control characters are escaped; other values are converted to text."""
    assert escape_log_value(value) == expected


@pytest.mark.parametrize(
    "response,body",
    [
        (({"a": 1}, 201, {"Set-Cookie": "session=s3cr3t"}), "{'a': 1}"),
        (Response("hello", headers={"Set-Cookie": "s3cr3t"}), "hello"),
        (Response(iter(["a", "b"])), "<streamed>"),
        ((), "None"),
    ],
)
def test_logging_decorator_body_without_headers(
    logged: list[str], response: object, body: str
) -> None:
    """Only response bodies are logged, without headers."""

    @log_traffic(log_response_body=True)
    def mock_func() -> object:
        return response

    with request_context(client_address=CLIENT_ADDRESS, app=app):
        mock_func()
    assert logged[1].endswith(f": {body}")
    assert "s3cr3t" not in " ".join(logged)
