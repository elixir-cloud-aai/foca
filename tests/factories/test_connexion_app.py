"""Tests for foca.factories.connexion_app."""

from connexion import App

from foca.models.config import Config
from foca.factories.connexion_app import (
    __add_config_to_connexion_app,
    create_connexion_app,
    )

CONFIG = Config()
ERROR_CODE = 400
ERROR_ORIGINAL = {
    'title': 'BAD REQUEST',
    'status_code': str(ERROR_CODE),
}
ERROR_REWRITTEN = {
    "msg": "The request is malformed.",
    "status_code": str(ERROR_CODE),
}


def test_add_config_to_connexion_app():
    """Test if app config is updated."""
    cnx_app = App(__name__)
    cnx_app = __add_config_to_connexion_app(cnx_app, CONFIG)
    assert isinstance(cnx_app, App)
    assert cnx_app.app.config['FOCA'] == CONFIG


def test_create_connexion_app_without_config():
    """Test Connexion app creation without config."""
    cnx_app = create_connexion_app()
    assert isinstance(cnx_app, App)


def test_create_connexion_app_with_config():
    """Test Connexion app creation with config."""
    cnx_app = create_connexion_app(CONFIG)
    assert isinstance(cnx_app, App)
