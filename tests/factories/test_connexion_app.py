from foca.factories.connexion_app import (
    __add_config_to_connexion_app,
    create_connexion_app
    )
from connexion import App


DICT_CONF_VALS = {
    'host': "host",
    'port': "port",
    'debug': "debug",
    'environment': "environment",
    'testing': "testing"
}


def test_add_config_to_connexion_app(monkeypatch):
    """Test if app config is being updated"""

    def mock_get_conf(*args, **kwargs):
        return DICT_CONF_VALS[list(args)[2]]

    monkeypatch.setattr(
        'foca.factories.connexion_app.get_conf',
        mock_get_conf
    )

    demo_app = App(__name__)
    res_app = __add_config_to_connexion_app(demo_app, {})
    app_ret_type = type(App(__name__))

    assert type(res_app) == app_ret_type


def test_creation_of_connexion_app(monkeypatch):
    """Test if connexion app is being created"""

    def mock_get_conf(*args, **kwargs):
        return DICT_CONF_VALS[list(args)[2]]

    monkeypatch.setattr(
        'foca.factories.connexion_app.get_conf',
        mock_get_conf
    )

    res_app = create_connexion_app({})
    app_ret_type = type(App(__name__))

    assert type(res_app) == app_ret_type
