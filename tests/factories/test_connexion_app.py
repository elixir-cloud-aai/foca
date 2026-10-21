"""Tests for foca.factories.connexion_app."""

import pytest
from connexion import App
from flask import request
from pydantic import ValidationError

from foca.config.access import get_app_config
from foca.factories import connexion_app as module
from foca.factories.connexion_app import (
    __add_config_to_connexion_app,
    _is_loopback,
    create_connexion_app,
)
from foca.models.config import Config, ServerConfig

CONFIG = Config()
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
