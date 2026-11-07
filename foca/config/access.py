"""Typed access to the configuration of FOCA apps."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from flask import Config as FlaskConfig
from flask import Flask, current_app, has_app_context
from pydantic import BaseModel
from pymongo.collection import Collection

from foca.deprecation import warn_deprecated

# Imported lazily at runtime: the config models import modules that use this
# module (e.g., `foca.security.access_control`)
if TYPE_CHECKING:
    from foca.models.config import Config

# Configuration of the most recently created FOCA app (key `latest`)
_configs: dict[str, Config] = {}

_T = TypeVar("_T", bound=BaseModel)


# Name of the attribute of Flask configurations holding FOCA's configuration
_ATTRIBUTE = "foca"


class _DeprecatingConfig(FlaskConfig):
    """Flask configuration whose attribute `foca` is deprecated.

    The attribute is a property, which takes precedence over the attribute of
    the instance, where the configuration stays (cf.
    :py:func:`_find_app_config`).
    """

    @property
    def foca(self) -> Any:
        """FOCA's configuration, deprecated.

        Raises:
            AttributeError: No configuration is attached.
        """
        try:
            value = vars(self)[_ATTRIBUTE]
        except KeyError:
            raise AttributeError(_ATTRIBUTE) from None
        warn_deprecated(
            "'app.config.foca' is deprecated and removed in the ASGI release; "
            "use 'foca.get_config()' or 'foca.get_custom_config()'.",
            stacklevel=2,
        )
        return value

    @foca.setter
    def foca(self, value: Any) -> None:
        """Set FOCA's configuration, deprecated.

        Args:
            value: FOCA's configuration.
        """
        warn_deprecated(
            "Setting 'app.config.foca' is deprecated and removed in the ASGI "
            "release; change the configuration that 'foca.get_config()' returns.",
            stacklevel=2,
        )
        vars(self)[_ATTRIBUTE] = value

    @foca.deleter
    def foca(self) -> None:
        """Remove FOCA's configuration, deprecated.

        Raises:
            AttributeError: No configuration is attached.
        """
        warn_deprecated(
            "Deleting 'app.config.foca' is deprecated and removed in the ASGI release.",
            stacklevel=2,
        )
        try:
            del vars(self)[_ATTRIBUTE]
        except KeyError:
            raise AttributeError(_ATTRIBUTE) from None


def _deprecate_attribute(app: Flask) -> None:
    """Make access to `app.config.foca` emit a deprecation warning.

    Flask configurations of Flask's own class, as Connexion creates them, get
    a subclass whose attribute `foca` warns. A class for Flask configurations
    (`Flask.config_class`) can't do this, as Connexion creates the app, with
    its configuration. Apps with configuration classes of their own keep
    them, and their attribute doesn't warn.

    Args:
        app: Flask application instance.
    """
    if type(app.config) is FlaskConfig:
        app.config.__class__ = _DeprecatingConfig


def set_app_config(app: Flask, config: Config) -> None:
    """Attach configuration to a Flask app.

    Deprecated, removed in the ASGI release: FOCA attaches the configuration
    to the apps it creates (cf. :py:meth:`foca.Foca.create_app`); scripts
    use a configuration without an app via :py:meth:`foca.Foca.use_config`.

    Args:
        app: Flask application instance.
        config: App configuration.
    """
    warn_deprecated(
        "'set_app_config()' is deprecated and removed in the ASGI release; "
        "FOCA attaches the configuration to the apps it creates, and "
        "scripts use 'Foca.use_config()'.",
        stacklevel=2,
    )
    _set_app_config(app, config)


def _set_app_config(app: Flask, config: Config) -> None:
    """Attach configuration to a Flask app.

    For FOCA's own use. The configuration is available via
    :py:func:`get_config`, also outside an app context.

    Args:
        app: Flask application instance.
        config: App configuration.
    """
    vars(app.config)[_ATTRIBUTE] = config
    _deprecate_attribute(app)
    _use_config(config)


def _use_config(config: Config) -> None:
    """Make a configuration the one used outside app contexts.

    For FOCA's own use (cf. :py:meth:`foca.Foca.use_config`): it becomes
    the configuration that :py:func:`get_config` returns outside app
    contexts.

    Args:
        config: App configuration.
    """
    _configs["latest"] = config


def _find_app_config(app: Flask) -> Config | None:
    """Find the configuration attached to a Flask app.

    For FOCA's own use, where the app is known but not current, e.g., while it
    is created.

    Args:
        app: Flask application instance.

    Returns:
        App configuration, or `None` if none is attached.
    """
    from foca.models.config import Config  # noqa: PLC0415 (import cycle)

    config = vars(app.config).get(_ATTRIBUTE)
    return config if isinstance(config, Config) else None


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
    config = _find_app_config(app)
    if config is None:
        raise RuntimeError(f"No FOCA configuration attached to app '{app}'")
    return config


def get_app_config(app: Flask) -> Config:
    """Get the configuration attached to a Flask app.

    Deprecated, removed in the ASGI release: use :py:func:`get_config`, in
    the app context of `app` if it
    isn't the current app.

    Args:
        app: Flask application instance.

    Returns:
        App configuration.

    Raises:
        RuntimeError: No FOCA configuration is attached to `app`.
    """
    warn_deprecated(
        "'get_app_config()' is deprecated and removed in the ASGI release; "
        "use 'foca.get_config()'.",
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
        workers), or used by :py:meth:`foca.Foca.use_config` (e.g., in
        scripts), whichever came last.

    Raises:
        RuntimeError: No FOCA app has been created, and no configuration
            used.
    """
    # Apps without FOCA configuration get that of the latest FOCA app
    config = _find_app_config(current_app) if has_app_context() else None
    if config is not None:
        return config
    if "latest" not in _configs:
        raise RuntimeError(
            "No FOCA configuration available: no FOCA app has been created,"
            " and no configuration used (cf. 'Foca.use_config()')"
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
            :py:meth:`foca.Foca.use_config` and
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
