"""Tests for foca.factories.flask_app."""
from flask import Flask

from foca.factories.flask_app import create_flask_app
from foca.models.config import Config


def test_create_flask_app():
    """Test Connexion app creation without config."""
    flask_app = create_flask_app()
    assert isinstance(flask_app, Flask)
    flask_app.config.foca = Config()
    assert isinstance(flask_app.config.foca, Config)
