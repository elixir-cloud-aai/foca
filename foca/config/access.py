"""Typed access to the configuration of FOCA apps."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from flask import Flask, current_app, has_app_context
from pydantic import BaseModel
from pymongo.collection import Collection

# Imported lazily at runtime: the config models import modules that use this
# module (e.g., `foca.security.access_control`)
if TYPE_CHECKING:
    from foca.models.config import Config

# Configuration of the most recently created FOCA app (key `latest`)
_configs: dict[str, Config] = {}

T = TypeVar("T", bound=BaseModel)


def set_app_config(app: Flask, config: Config) -> None:
    """Attach configuration to a Flask app.

    The configuration is available as `app.config.foca` and via
    :py:func:`get_config`, also outside an app context.

    Args:
        app: Flask application instance.
        config: App configuration.
    """
    setattr(app.config, "foca", config)
    _configs["latest"] = config


def get_app_config(app: Flask) -> Config:
    """Get the configuration attached to a Flask app.

    Args:
        app: Flask application instance.

    Returns:
        App configuration.

    Raises:
        RuntimeError: No FOCA configuration is attached to `app`.
    """
    from foca.models.config import Config  # noqa: PLC0415 (import cycle)

    config = getattr(app.config, "foca", None)
    if not isinstance(config, Config):
        raise RuntimeError(f"No FOCA configuration attached to app '{app}'")
    return config


def get_config() -> Config:
    """Get the configuration of the current FOCA app.

    Returns:
        Configuration of the app of the current app context, if any,
        otherwise of the most recently created FOCA app (e.g., in Celery
        workers or scripts).

    Raises:
        RuntimeError: No FOCA app has been created.
    """
    from foca.models.config import Config  # noqa: PLC0415 (import cycle)

    if has_app_context():
        config = getattr(current_app.config, "foca", None)
        if isinstance(config, Config):
            return config
    if "latest" not in _configs:
        raise RuntimeError(
            "No FOCA configuration available: no FOCA app has been created"
        )
    return _configs["latest"]


def get_custom_config(model: type[T]) -> T:
    """Get the custom configuration of the current FOCA app, typed.

    The custom configuration (section `custom`) is an instance of the model
    passed as `custom_config_model` when the app was created (cf.
    :py:class:`foca.Foca`), but FOCA keeps it as an untyped extra field of
    its configuration.

    Args:
        model: Model of the custom configuration (or a base class of it).

    Returns:
        Custom configuration of the app of the current app context, if any,
        otherwise of the most recently created FOCA app (cf.
        :py:func:`get_config`).

    Raises:
        RuntimeError: No FOCA app has been created, or its custom
            configuration is missing or not an instance of `model`, e.g., as
            `custom_config_model` is not set, or not the full module path of
            `model` (a model imported under another module path is another
            class).
    """
    custom = (get_config().model_extra or {}).get("custom")
    if not isinstance(custom, model):
        name = f"{model.__module__}.{model.__qualname__}"
        raise RuntimeError(
            f"No custom configuration of type '{name}' (found "
            f"'{type(custom).__name__}'); check that `custom_config_model` is "
            "the full module path of the model"
        )
    return custom


def get_collection(db: str, collection: str) -> Collection[dict[str, Any]]:
    """Get a registered MongoDB collection of the current FOCA app.

    Args:
        db: Name of the database, as configured.
        collection: Name of the collection, as configured.

    Returns:
        MongoDB collection.

    Raises:
        KeyError: The database or collection is not configured.
        RuntimeError: The collection has not been registered yet (cf.
            :py:func:`foca.database.register_mongodb.register_mongodb`).
    """
    conf = get_config()
    dbs = conf.db.dbs if conf.db is not None else None
    if not dbs or db not in dbs:
        raise KeyError(f"Database '{db}' not configured")
    collections = dbs[db].collections
    if not collections or collection not in collections:
        raise KeyError(f"Collection '{collection}' of '{db}' not configured")
    client = collections[collection].client
    if client is None:
        raise RuntimeError(f"Collection '{collection}' of '{db}' not registered yet")
    return client
