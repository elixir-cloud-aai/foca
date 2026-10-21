"""Tests for foca.factories.connexion_app."""

import io
from http import HTTPStatus

import pytest
from connexion import App
from flask import request
from pydantic import ValidationError

from foca.config.access import get_app_config
from foca.errors.exceptions import register_exception_handler
from foca.factories import connexion_app as module
from foca.factories.connexion_app import (
    __add_config_to_connexion_app,
    _is_loopback,
    create_connexion_app,
)
from foca.models.config import Config, ExceptionConfig, ServerConfig

CONFIG = Config()
LIMIT = 10
ERROR_CODE = 400
ERROR_ORIGINAL = {
    "title": "BAD REQUEST",
    "status_code": str(ERROR_CODE),
}
ERROR_REWRITTEN = {
    "msg": "The request is malformed.",
    "status_code": str(ERROR_CODE),
}


def test_add_config_to_connexion_app():
    """Test if app config is updated."""
    cnx_app = App(__name__)
    cnx_app = __add_config_to_connexion_app(cnx_app, CONFIG)
    assert cnx_app.app.config.foca is CONFIG


def test_create_connexion_app_without_config():
    """Test Connexion app creation without config."""
    cnx_app = create_connexion_app()
    with pytest.raises(RuntimeError):
        get_app_config(cnx_app.app)


def test_create_connexion_app_with_config():
    """Test Connexion app creation with config; server settings applied."""
    config = Config(
        server=ServerConfig(host="127.0.0.1", port=1234, testing=True)
    )
    cnx_app = create_connexion_app(config)
    assert get_app_config(cnx_app.app) is config
    assert (cnx_app.host, cnx_app.port, cnx_app.debug) == (
        "127.0.0.1",
        1234,
        False,
    )
    assert cnx_app.app.config["TESTING"] is True


def test_debug_off_by_default():
    """Debug mode is off by default."""
    cnx_app = create_connexion_app(Config())
    assert cnx_app.debug is False
    assert cnx_app.app.config["DEBUG"] is False


@pytest.mark.parametrize(
    "host,warned",
    [
        ("0.0.0.0", True),
        ("::", True),
        ("example.org", True),
        ("10.0.0.1", True),
        ("127.0.0.1", False),
        ("127.1.2.3", False),
        ("::1", False),
        ("localhost", False),
    ],
)
def test_debug_warning(monkeypatch, host, warned):
    """Debug mode on non-loopback hosts is warned about."""
    warnings = []
    monkeypatch.setattr(module.logger, "warning", warnings.append)
    cnx_app = create_connexion_app(
        Config(server=ServerConfig(host=host, debug=True))
    )
    assert cnx_app.app.config["DEBUG"] is True
    assert bool(warnings) is warned
    create_connexion_app(Config(server=ServerConfig(host=host)))
    assert len(warnings) == int(warned)


@pytest.mark.parametrize(
    "host,expected",
    [("localhost", True), ("::1", True), ("0.0.0.0", False), ("x", False)],
)
def test_is_loopback(host, expected):
    assert _is_loopback(host) is expected


@pytest.mark.parametrize("field", ["environment", "use_reloader"])
def test_removed_server_fields(field):
    """Removed server settings fail validation."""
    with pytest.raises(ValidationError):
        ServerConfig(**{field: "x"})


def test_max_content_length_unset_by_default():
    """Request bodies are not limited by default."""
    cnx_app = create_connexion_app(Config())
    assert cnx_app.app.config["MAX_CONTENT_LENGTH"] is None


def test_max_content_length():
    """Larger request bodies are rejected with 413."""
    cnx_app = create_connexion_app(
        Config(server=ServerConfig(max_content_length=10))
    )
    app = cnx_app.app
    assert app.config["MAX_CONTENT_LENGTH"] == 10
    app.add_url_rule(
        "/echo",
        "echo",
        lambda: str(len(request.get_data())),
        methods=["POST"],
    )
    client = app.test_client()
    assert client.post("/echo", data=b"x" * 10).status_code == 200
    assert client.post("/echo", data=b"x" * 11).status_code == 413


@pytest.mark.parametrize("value", [0, -1, "x"])
def test_invalid_max_content_length(value):
    with pytest.raises(ValidationError):
        ServerConfig(max_content_length=value)


def _limited_app(limit: int | None = LIMIT):
    """Create a Flask app with the given size limit of request bodies, with
    routes that read the body (`/echo`, returning its size) or don't
    (`/ignore`), and FOCA's problem handler.
    """
    cnx_app = create_connexion_app(
        Config(server=ServerConfig(max_content_length=limit))
    )
    register_exception_handler(
        cnx_app, ExceptionConfig(media_type="application/json")
    )
    app = cnx_app.app
    app.add_url_rule(
        "/echo",
        "echo",
        lambda: str(len(request.get_data())),
        methods=["POST"],
    )
    app.add_url_rule(
        "/ignore", "ignore", lambda: "ok", methods=["GET", "POST"]
    )
    return app


def _send_chunked(client, body: bytes, path: str = "/echo"):
    """Send a chunked request body (of unknown size), with a stream
    terminated by the server (as by Werkzeug's and Gunicorn's servers);
    return the response and the number of bytes read.
    """
    stream = io.BytesIO(body)
    response = client.post(
        path,
        input_stream=stream,
        headers={"Transfer-Encoding": "chunked"},
        environ_overrides={"wsgi.input_terminated": True},
    )
    return response, stream.tell()


def _assert_too_large(response) -> None:
    """Assert that a response rejects a request body as too large, with
    problem details of the configured media type.
    """
    status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert response.status_code == status
    assert response.content_type == "application/json"
    assert response.get_json() == {"title": status.phrase, "status": status}


@pytest.mark.parametrize("size", [LIMIT + 1, 10 * LIMIT])
@pytest.mark.parametrize("path", ["/echo", "/ignore"])
def test_max_content_length_chunked(size, path):
    """Chunked bodies over the limit are rejected (`413`) after reading at
    most one byte more than the limit, also by routes not reading them.
    """
    client = _limited_app().test_client()
    response, read = _send_chunked(client, b"x" * size, path=path)
    _assert_too_large(response)
    assert read == LIMIT + 1


@pytest.mark.parametrize("size", [0, LIMIT])
def test_max_content_length_chunked_at_limit(size):
    """Chunked bodies up to the limit are passed on in full."""
    client = _limited_app().test_client()
    response, _ = _send_chunked(client, b"x" * size)
    assert response.status_code == 200
    assert response.text == str(size)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_max_content_length_declared_unread(method):
    """Bodies with a declared size over the limit are rejected (`413`)
    without reading them, also by routes not reading them.
    """
    client = _limited_app().test_client()
    stream = io.BytesIO(b"x" * (LIMIT + 1))
    response = client.open(
        "/ignore",
        method=method,
        input_stream=stream,
        environ_overrides={"CONTENT_LENGTH": str(LIMIT + 1)},
    )
    _assert_too_large(response)
    assert stream.tell() == 0


@pytest.mark.parametrize("size", [0, LIMIT - 1, LIMIT])
def test_max_content_length_declared_at_limit(size):
    """Bodies with a declared size up to the limit are handled normally."""
    client = _limited_app().test_client()
    response = client.post("/echo", data=b"x" * size)
    assert response.status_code == 200
    assert response.text == str(size)


def test_max_content_length_no_body():
    """Requests without a body are handled normally."""
    client = _limited_app().test_client()
    response = client.get("/ignore")
    assert response.status_code == 200
    response = client.get(
        "/ignore", environ_overrides={"wsgi.input_terminated": True}
    )
    assert response.status_code == 200


def test_max_content_length_unset_chunked():
    """Without a limit, no check is registered and chunked bodies are passed
    on in full.
    """
    app = _limited_app(limit=None)
    assert not any(app.before_request_funcs.values())
    response, read = _send_chunked(app.test_client(), b"x" * 10 * LIMIT)
    assert response.status_code == 200
    assert response.text == str(10 * LIMIT)
    assert read == 10 * LIMIT


def test_max_content_length_removed():
    """If the limit is removed from the Flask config after the app was
    created, bodies are no longer checked.
    """
    app = _limited_app()
    app.config["MAX_CONTENT_LENGTH"] = None
    response, read = _send_chunked(app.test_client(), b"x" * 10 * LIMIT)
    assert response.status_code == 200
    assert read == 10 * LIMIT
