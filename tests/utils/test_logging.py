"""Tests for the logging utilties module."""

import logging

from connexion import App
from flask import Flask

from foca.utils import logging as module
from foca.utils.logging import log_traffic

app = Flask(__name__)
REQ = {
    "REQUEST_METHOD": "GET",
    "PATH_INFO": "/",
    "SERVER_PROTOCOL": "HTTP/1.1",
    "REMOTE_ADDR": "192.168.1.1",
}

# Get logger instance
logger = logging.getLogger(__name__)


def test_logging_decorator(caplog):
    """Verify the default settings work properly"""
    caplog.set_level(logging.INFO)

    @log_traffic
    def mock_func():
        return {"foo": "bar"}

    with app.test_request_context(environ_base=REQ):
        mock_func()
    assert (
        "Incoming request" in caplog.text
        and "Response to request" in caplog.text
    )


def test_logging_decorator_log_level(caplog):
    """Verify that the 'log_level argument modifies the log level properly"""

    @log_traffic(log_level=30)
    def mock_func():
        return {"foo": "bar"}

    with app.test_request_context(environ_base=REQ):
        mock_func()
    assert "WARNING" in caplog.text


def test_logging_decorator_req_only(caplog):
    """Verify that only the request gets logged"""
    caplog.set_level(logging.INFO)

    @log_traffic(log_response=False)
    def mock_func():
        return {"foo": "bar"}

    with app.test_request_context(environ_base=REQ):
        mock_func()
    assert (
        "Incoming request" in caplog.text
        and "Response to request" not in caplog.text
    )


def test_logging_decorator_res_only(caplog):
    """Verify that only the response gets logged"""
    caplog.set_level(logging.INFO)

    @log_traffic(log_request=False)
    def mock_func():
        return {"foo": "bar"}

    with app.test_request_context(environ_base=REQ):
        mock_func()
    assert (
        "Incoming request" not in caplog.text
        and "Response to request" in caplog.text
    )


@log_traffic
def get_items():
    """Controller of test API."""
    return "ok"


def test_logging_decorator_in_app(monkeypatch):
    """Requests and responses of a running app are logged."""
    messages = []
    monkeypatch.setattr(
        module.logger, "log", lambda level, msg: messages.append(msg)
    )
    app = App(__name__, skip_error_handlers=True)
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
                }
            },
        }
    )
    response = app.app.test_client().get(
        "/items", environ_base={"REMOTE_ADDR": "10.0.0.1"}
    )
    assert response.status_code == 200
    assert messages == [
        'Incoming request: "GET /items HTTP/1.1" from 10.0.0.1',
        'Response to request "GET /items HTTP/1.1" from 10.0.0.1: ok',
    ]
