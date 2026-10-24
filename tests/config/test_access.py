"""Tests for typed access to app configuration."""

from typing import Any

import mongomock
import pytest
from flask import Flask
from pydantic import BaseModel
from pymongo.collection import Collection

from foca import get_collection, get_config, get_custom_config
from foca.config import access
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (
    CollectionConfig,
    Config,
    DBConfig,
    MongoConfig,
)


class CustomConfig(BaseModel):
    """Custom configuration of an app."""

    name: str = "app"


class OtherConfig(BaseModel):
    """Another model of custom configurations."""


@pytest.fixture
def no_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget configurations of previously created apps."""
    monkeypatch.setattr(access, "_configs", {})


def test_get_config_in_app_context(no_apps: None) -> None:
    """Configuration of the app of the current app context is returned."""
    config = Config()
    app = create_connexion_app(config).app
    create_connexion_app(Config())
    with app.app_context():
        assert get_config() is config


def test_get_config_outside_app_context(no_apps: None) -> None:
    """Configuration of the most recently created app is returned."""
    create_connexion_app(Config())
    config = Config()
    create_connexion_app(config)
    assert get_config() is config


def test_get_config_no_app(no_apps: None) -> None:
    """Error is raised if no app has been created."""
    with pytest.raises(RuntimeError):
        get_config()


def test_get_config_app_without_foca_config(no_apps: None) -> None:
    """Apps without FOCA configuration fall back to the latest app."""
    config = Config()
    create_connexion_app(config)
    with Flask(__name__).app_context():
        assert get_config() is config


def test_get_app_config() -> None:
    """Configuration attached to a Flask app is returned or an error."""
    config = Config()
    app = create_connexion_app(config).app
    assert access.get_app_config(app) is config
    with pytest.raises(RuntimeError):
        access.get_app_config(Flask(__name__))


@pytest.fixture
def db_app(no_apps: None) -> Collection[dict[str, Any]]:
    """App with a registered and an unregistered collection."""
    collection: Collection[dict[str, Any]] = mongomock.MongoClient().db.items
    items = CollectionConfig()
    config = Config(
        db=MongoConfig(
            dbs={
                "db": DBConfig(
                    collections={
                        "items": items,
                        "other": CollectionConfig(),
                    }
                ),
                "empty": DBConfig(),
            }
        )
    )
    items.client = collection
    create_connexion_app(config)
    return collection


def test_get_collection(db_app: Collection[dict[str, Any]]) -> None:
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
def test_get_collection_errors(
    db_app: Collection[dict[str, Any]], db: str, collection: str, error: type[Exception]
) -> None:
    """Unconfigured and unregistered collections raise errors."""
    with pytest.raises(error):
        get_collection(db, collection)


def test_get_collection_no_db(no_apps: None) -> None:
    """Apps without database configuration raise errors."""
    create_connexion_app(Config())
    with pytest.raises(KeyError):
        get_collection("db", "items")


def _app_with_custom(custom: object) -> Flask:
    """Create an app with a custom configuration.

    Args:
        custom: Custom configuration, set as extra field `custom`.

    Returns:
        Flask app.
    """
    config = Config()
    setattr(config, "custom", custom)
    return create_connexion_app(config).app


def test_get_custom_config(no_apps: None) -> None:
    """The custom configuration of the current app is returned, typed."""
    custom = CustomConfig()
    app = _app_with_custom(custom)
    _app_with_custom(CustomConfig())
    with app.app_context():
        config = get_custom_config(CustomConfig)
    assert config is custom
    assert config.name == "app"


def test_get_custom_config_base_class(no_apps: None) -> None:
    """Custom configurations are returned for base classes of their model."""
    custom = CustomConfig()
    _app_with_custom(custom)
    assert get_custom_config(BaseModel) is custom


def test_get_custom_config_outside_app_context(no_apps: None) -> None:
    """The custom configuration of the most recently created app is returned."""
    _app_with_custom(CustomConfig())
    custom = CustomConfig()
    _app_with_custom(custom)
    assert get_custom_config(CustomConfig) is custom


@pytest.mark.parametrize(
    ("custom", "found"),
    [(OtherConfig(), "OtherConfig"), ({"name": "app"}, "dict"), (None, "NoneType")],
)
def test_get_custom_config_other_model(
    no_apps: None, custom: object, found: str
) -> None:
    """Custom configurations of another model raise errors, naming the model."""
    _app_with_custom(custom)
    with pytest.raises(RuntimeError) as exc_info:
        get_custom_config(CustomConfig)
    message = str(exc_info.value)
    assert f"'{__name__}.CustomConfig'" in message
    assert f"'{found}'" in message
    assert "custom_config_model" in message


def test_get_custom_config_missing(no_apps: None) -> None:
    """Missing custom configurations raise errors."""
    create_connexion_app(Config())
    with pytest.raises(RuntimeError, match="custom_config_model"):
        get_custom_config(CustomConfig)


def test_get_custom_config_no_app(no_apps: None) -> None:
    """Errors are raised if no app has been created."""
    with pytest.raises(RuntimeError, match="no FOCA app"):
        get_custom_config(CustomConfig)
