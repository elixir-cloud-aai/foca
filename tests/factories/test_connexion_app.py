"""Tests for foca.factories.connexion_app."""

import io
import json
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


def _read_data() -> bytes:
    """Return the request body, read via `get_data()`."""
    return request.get_data()


def _read_stream() -> bytes:
    """Return the request body, read via `stream`."""
    return request.stream.read()


def _read_form() -> dict[str, str]:
    """Return the form data of the request."""
    return request.form.to_dict()


def _read_files() -> dict[str, str]:
    """Return the contents of the uploaded files of the request."""
    return {key: file.read().decode() for key, file in request.files.items()}


# Views returning the request body, read in different ways
VIEWS = {
    "data": _read_data,
    "stream": _read_stream,
    "form": _read_form,
    "files": _read_files,
}


def _limited_app(limit: int | None = LIMIT):
    """Create a Flask app with the given size limit of request bodies, with
    routes that read the body in different ways (returning what they read)
    or don't (`/ignore`), and FOCA's problem handler.
    """
    cnx_app = create_connexion_app(
        Config(server=ServerConfig(max_content_length=limit))
    )
    register_exception_handler(
        cnx_app, ExceptionConfig(media_type="application/json")
    )
    app = cnx_app.app
    for name, view in VIEWS.items():
        app.add_url_rule(f"/{name}", name, view, methods=["POST"])
    app.add_url_rule(
        "/ignore", "ignore", lambda: "ok", methods=["GET", "POST"]
    )
    return app


def _send_chunked(
    client, body: bytes, path: str = "/data", content_type: str | None = None
):
    """Send a chunked request body (of unknown size), with a stream
    terminated by the server (as by Werkzeug's and Gunicorn's servers);
    return the response and the number of bytes read.
    """
    stream = io.BytesIO(body)
    response = client.post(
        path,
        input_stream=stream,
        content_type=content_type,
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


@pytest.mark.parametrize("path", ["/data", "/ignore"])
def test_max_content_length_chunked(path):
    """Chunked bodies over the limit are rejected (`413`) after reading at
    most one byte more than the limit, also by routes not reading them.
    """
    client = _limited_app().test_client()
    response, read = _send_chunked(client, b"x" * 10 * LIMIT, path=path)
    _assert_too_large(response)
    assert read == LIMIT + 1


@pytest.mark.parametrize("size", [0, LIMIT])
@pytest.mark.parametrize("path", ["/data", "/stream"])
def test_max_content_length_chunked_at_limit(size, path):
    """Chunked bodies up to the limit are passed on in full, both as data
    and as stream.
    """
    client = _limited_app().test_client()
    response, _ = _send_chunked(client, b"x" * size, path=path)
    assert response.status_code == 200
    assert response.data == b"x" * size


@pytest.mark.parametrize(
    "path,content_type,body,expected",
    [
        (
            "/form",
            "application/x-www-form-urlencoded",
            b"a=1&b=2",
            {"a": "1", "b": "2"},
        ),
        (
            "/files",
            "multipart/form-data; boundary=B",
            b"--B\r\nContent-Disposition: form-data; name=f; filename=x"
            b"\r\n\r\nyy\r\n--B--\r\n",
            {"f": "yy"},
        ),
    ],
)
def test_max_content_length_chunked_form(path, content_type, body, expected):
    """Chunked form data up to the limit is parsed in full."""
    app = _limited_app(limit=len(body))
    response, _ = _send_chunked(
        app.test_client(), body, path=path, content_type=content_type
    )
    assert response.status_code == 200
    assert response.get_json() == expected


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
    response = client.post("/data", data=b"x" * size)
    assert response.status_code == 200
    assert response.data == b"x" * size


def _send_declared(client, body: bytes, content_length: str):
    """Send a request body with the given `Content-Length` value, with a
    stream terminated by the server; return the response and the number of
    bytes read.
    """
    stream = io.BytesIO(body)
    response = client.post(
        "/data",
        input_stream=stream,
        environ_overrides={
            "CONTENT_LENGTH": content_length,
            "wsgi.input_terminated": True,
        },
    )
    return response, stream.tell()


@pytest.mark.parametrize(
    "content_length",
    ["abc", "-1", "+5", "5 5", "0" * 4400 + "5"],
    ids=["letters", "negative", "sign", "inner space", "too many digits"],
)
def test_max_content_length_invalid_declared(content_length):
    """Bodies with an invalid declared size are rejected (`400`) without
    reading them.
    """
    client = _limited_app().test_client()
    response, read = _send_declared(client, b"x" * 10 * LIMIT, content_length)
    status = HTTPStatus.BAD_REQUEST
    assert response.status_code == status
    assert response.content_type == "application/json"
    assert response.get_json() == {"title": status.phrase, "status": status}
    assert read == 0


@pytest.mark.parametrize("content_length", [str(LIMIT), f" {LIMIT}\t"])
def test_max_content_length_valid_declared(content_length):
    """Bodies with a valid declared size, also with surrounding whitespace
    (cf. RFC 9110), are handled normally.
    """
    client = _limited_app().test_client()
    response, _ = _send_declared(client, b"x" * LIMIT, content_length)
    assert response.status_code == 200
    assert response.data == b"x" * LIMIT


def test_max_content_length_removed():
    """If the limit is removed from the Flask config after the app was
    created, bodies are no longer checked.
    """
    app = _limited_app()
    app.config["MAX_CONTENT_LENGTH"] = None
    response, read = _send_chunked(app.test_client(), b"x" * 10 * LIMIT)
    assert response.status_code == 200
    assert read == 10 * LIMIT


def test_max_content_length_added():
    """If a limit is set in the Flask config after the app was created,
    bodies are checked.
    """
    app = _limited_app(limit=None)
    app.config["MAX_CONTENT_LENGTH"] = LIMIT
    response, read = _send_chunked(app.test_client(), b"x" * 10 * LIMIT)
    assert response.status_code == 413
    assert read == LIMIT + 1


def test_max_content_length_empty_declared():
    """Bodies with an empty declared size are of unknown size (cf. PEP
    3333), i.e., checked like chunked bodies.
    """
    client = _limited_app().test_client()
    response, read = _send_declared(client, b"x" * 10 * LIMIT, "")
    _assert_too_large(response)
    assert read == LIMIT + 1
    response, _ = _send_declared(client, b"x" * LIMIT, "")
    assert response.status_code == 200
    assert response.data == b"x" * LIMIT


def test_max_content_length_no_body():
    """Requests without a body are handled normally."""
    client = _limited_app().test_client()
    assert client.get("/ignore").status_code == 200


def test_max_content_length_unset_chunked():
    """Without a limit, chunked bodies are passed on in full."""
    app = _limited_app(limit=None)
    response, read = _send_chunked(app.test_client(), b"x" * 10 * LIMIT)
    assert response.status_code == 200
    assert response.data == b"x" * 10 * LIMIT
    assert read == 10 * LIMIT


# Calls of the security handler and operation of `SPEC`
CALLS: list[str] = []


def mock_api_key_info(api_key, required_scopes=None):
    """Accept any API key."""
    CALLS.append("auth")
    return {"sub": "user"}


def post_items(body):
    """Return the request body."""
    CALLS.append("operation")
    return body, 200


SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "Test API", "version": "1.0.0"},
    "paths": {
        "/items": {
            "post": {
                "operationId": f"{__name__}.post_items",
                "security": [{"apiKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                                "required": ["name"],
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
    "components": {
        "securitySchemes": {
            "apiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "x-apikeyInfoFunc": f"{__name__}.mock_api_key_info",
            }
        }
    },
}


class TestMaxContentLengthOperation:
    """Size limit of request bodies of a Connexion operation."""

    BODY = json.dumps({"name": "x" * LIMIT}).encode()

    @pytest.fixture
    def client(self):
        CALLS.clear()
        cnx_app = create_connexion_app(
            Config(server=ServerConfig(max_content_length=len(self.BODY)))
        )
        register_exception_handler(
            cnx_app, ExceptionConfig(media_type="application/json")
        )
        cnx_app.add_api(SPEC)
        return cnx_app.app.test_client()

    def _post(self, client, body: bytes, chunked: bool):
        headers = {"X-API-Key": "key"}
        overrides = {}
        if chunked:
            headers["Transfer-Encoding"] = "chunked"
            overrides["wsgi.input_terminated"] = True
        else:
            overrides["CONTENT_LENGTH"] = str(len(body))
        return client.post(
            "/items",
            input_stream=io.BytesIO(body),
            content_type="application/json",
            headers=headers,
            environ_overrides=overrides,
        )

    @pytest.mark.parametrize("chunked", [False, True])
    def test_too_large(self, client, chunked):
        """Bodies over the limit are rejected before authentication and
        validation, and the operation is not called.
        """
        response = self._post(client, self.BODY + b" ", chunked=chunked)
        _assert_too_large(response)
        assert CALLS == []

    @pytest.mark.parametrize("chunked", [False, True])
    def test_at_limit(self, client, chunked):
        """Bodies up to the limit reach the operation intact."""
        response = self._post(client, self.BODY, chunked=chunked)
        assert response.status_code == 200
        assert response.get_json() == json.loads(self.BODY)
        assert CALLS == ["auth", "operation"]
