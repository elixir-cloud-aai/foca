"""Typed access to the configuration of FOCA apps."""

from __future__ import annotations

import threading
import warnings
from typing import TYPE_CHECKING, Any, TypeVar

from flask import Config as FlaskConfig
from flask import Flask, current_app, has_app_context
from pydantic import BaseModel
from pymongo.collection import Collection

# Imported lazily at runtime: the config models import modules that use this
# module (e.g., `foca.security.access_control`)
if TYPE_CHECKING:
    from foca.models.config import Config

# Configuration of the most recently created FOCA app (key `latest`)
_configs: dict[str, Config] = {}

_T = TypeVar("_T", bound=BaseModel)


# Name of the attribute of Flask configurations holding FOCA's configuration
_ATTRIBUTE = "foca"

# Subclasses of Flask configuration classes with the deprecated attribute, by
# class
_deprecating_classes: dict[type[FlaskConfig], type[FlaskConfig]] = {}
_deprecating_classes_lock = threading.Lock()


def _get_foca_attribute(flask_config: FlaskConfig) -> Any:
    """Get FOCA's configuration from a Flask configuration, deprecated.

    Args:
        flask_config: Flask configuration.

    Returns:
        FOCA's configuration.

    Raises:
        AttributeError: No configuration is attached.
    """
    try:
        value = vars(flask_config)[_ATTRIBUTE]
    except KeyError:
        raise AttributeError(_ATTRIBUTE) from None
    warnings.warn(
        "'app.config.foca' is deprecated; use 'foca.get_config()' or "
        "'foca.get_custom_config()'.",
        DeprecationWarning,
        stacklevel=2,
    )
    return value


def _set_foca_attribute(flask_config: FlaskConfig, value: Any) -> None:
    """Set FOCA's configuration of a Flask configuration, deprecated.

    Args:
        flask_config: Flask configuration.
        value: FOCA's configuration.
    """
    warnings.warn(
        "Setting 'app.config.foca' is deprecated; change the configuration "
        "that 'foca.get_config()' returns.",
        DeprecationWarning,
        stacklevel=2,
    )
    vars(flask_config)[_ATTRIBUTE] = value


def _deprecate_attribute(app: Flask) -> None:
    """Make access to `app.config.foca` emit a deprecation warning.

    The class of the Flask configuration is replaced by a subclass whose
    attribute `foca` is a property, which takes precedence over the attribute
    of the instance, where the configuration stays.

    Args:
        app: Flask application instance.
    """
    cls = type(app.config)
    if cls in _deprecating_classes.values():
        return
    with _deprecating_classes_lock:
        if cls not in _deprecating_classes:
            _deprecating_classes[cls] = type(
                cls.__name__,
                (cls,),
                {
                    "__module__": cls.__module__,
                    "__qualname__": cls.__qualname__,
                    _ATTRIBUTE: property(_get_foca_attribute, _set_foca_attribute),
                },
            )
    app.config.__class__ = _deprecating_classes[cls]


def set_app_config(app: Flask, config: Config) -> None:
    """Attach configuration to a Flask app.

    The configuration is available via :py:func:`get_config`, also outside an
    app context.

    Args:
        app: Flask application instance.
        config: App configuration.
    """
    vars(app.config)[_ATTRIBUTE] = config
    _deprecate_attribute(app)
    _configs["latest"] = config


def _get_app_config(app: Flask) -> Config:
    """Get the configuration attached to a Flask app.

    For FOCA's own use, where the app is known but not current, e.g., while it
    is created.

    Args:
        app: Flask application instance.

    Returns:
        App configuration.

    Raises:
        RuntimeError: No FOCA configuration is attached to `app`.
    """
    from foca.models.config import Config  # noqa: PLC0415 (import cycle)

    config = vars(app.config).get(_ATTRIBUTE)
    if not isinstance(config, Config):
        raise RuntimeError(f"No FOCA configuration attached to app '{app}'")
    return config


def get_app_config(app: Flask) -> Config:
    """Get the configuration attached to a Flask app.

    Deprecated: use :py:func:`get_config`, in the app context of `app` if it
    isn't the current app.

    Args:
        app: Flask application instance.

    Returns:
        App configuration.

    Raises:
        RuntimeError: No FOCA configuration is attached to `app`.
    """
    warnings.warn(
        "'get_app_config()' is deprecated; use 'foca.get_config()'.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _get_app_config(app)


def get_config() -> Config:
    """Get the configuration of the current FOCA app.

    Use it, or :py:func:`get_custom_config`, to read the configuration in
    apps, rather than `current_app.config.foca` or `get_app_config()`, which
    are deprecated.

    Returns:
        Configuration of the app of the current app context, if any,
        otherwise of the most recently created FOCA app (e.g., in Celery
        workers or scripts).

    Raises:
        RuntimeError: No FOCA app has been created.
    """
    if has_app_context():
        try:
            return _get_app_config(current_app)
        except RuntimeError:
            pass
    if "latest" not in _configs:
        raise RuntimeError(
            "No FOCA configuration available: no FOCA app has been created"
        )
    return _configs["latest"]


def get_custom_config(model: type[_T]) -> _T:
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
