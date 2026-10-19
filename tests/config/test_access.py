"""Tests for typed access to app configuration."""

import mongomock
import pytest
from flask import Flask

from foca import get_collection, get_config
from foca.config import access
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (
    CollectionConfig,
    Config,
    DBConfig,
    MongoConfig,
)


@pytest.fixture
def no_apps(monkeypatch):
    """Forget configurations of previously created apps."""
    monkeypatch.setattr(access, "_configs", {})


def test_get_config_in_app_context(no_apps):
    """Configuration of the app of the current app context is returned."""
    config = Config()
    app = create_connexion_app(config).app
    create_connexion_app(Config())
    with app.app_context():
        assert get_config() is config


def test_get_config_outside_app_context(no_apps):
    """Configuration of the most recently created app is returned."""
    create_connexion_app(Config())
    config = Config()
    create_connexion_app(config)
    assert get_config() is config


def test_get_config_no_app(no_apps):
    """Error is raised if no app has been created."""
    with pytest.raises(RuntimeError):
        get_config()


def test_get_config_app_without_foca_config(no_apps):
    """Apps without FOCA configuration fall back to the latest app."""
    config = Config()
    create_connexion_app(config)
    with Flask(__name__).app_context():
        assert get_config() is config


def test_get_app_config():
    """Configuration attached to a Flask app is returned or an error."""
    config = Config()
    app = create_connexion_app(config).app
    assert access.get_app_config(app) is config
    with pytest.raises(RuntimeError):
        access.get_app_config(Flask(__name__))


@pytest.fixture
def db_app(no_apps):
    """App with a registered and an unregistered collection."""
    collection = mongomock.MongoClient().db.items
    config = Config(
        db=MongoConfig(
            dbs={
                "db": DBConfig(
                    collections={
                        "items": CollectionConfig(),
                        "other": CollectionConfig(),
                    }
                ),
                "empty": DBConfig(),
            }
        )
    )
    config.db.dbs["db"].collections["items"].client = collection
    create_connexion_app(config)
    return collection


def test_get_collection(db_app):
    """Registered collections are returned."""
    assert get_collection("db", "items") is db_app


@pytest.mark.parametrize(
    "db,collection,error",
    [
        ("missing", "items", KeyError),
        ("empty", "items", KeyError),
        ("db", "missing", KeyError),
        ("db", "other", RuntimeError),
    ],
)
def test_get_collection_errors(db_app, db, collection, error):
    """Unconfigured and unregistered collections raise errors."""
    with pytest.raises(error):
        get_collection(db, collection)


def test_get_collection_no_db(no_apps):
    """Apps without database configuration raise errors."""
    create_connexion_app(Config())
    with pytest.raises(KeyError):
        get_collection("db", "items")
