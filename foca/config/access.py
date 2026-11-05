"""Typed access to the configuration of FOCA apps."""

from __future__ import annotations

import copyreg
import threading
import warnings
from typing import TYPE_CHECKING, Any, SupportsIndex, TypeVar, cast

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

# Marks the subclasses of Flask configuration classes with the deprecated
# attribute
_DEPRECATING_MARKER = "_foca_deprecates_attribute"

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


def _delete_foca_attribute(flask_config: FlaskConfig) -> None:
    """Remove FOCA's configuration from a Flask configuration, deprecated.

    Args:
        flask_config: Flask configuration.

    Raises:
        AttributeError: No configuration is attached.
    """
    warnings.warn(
        "Deleting 'app.config.foca' is deprecated.",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        del vars(flask_config)[_ATTRIBUTE]
    except KeyError:
        raise AttributeError(_ATTRIBUTE) from None


def _reduce_ex(flask_config: FlaskConfig, protocol: SupportsIndex) -> Any:
    """Pickle a Flask configuration as one of its original class.

    Args:
        flask_config: Flask configuration, of a deprecating subclass.
        protocol: Pickle protocol.

    Returns:
        What the original class would return, with that class instead of the
        subclass; what its own `__reduce__()` returns otherwise.
    """
    cls = type(flask_config)
    original = cls.__mro__[1]
    reduced = super(cls, flask_config).__reduce_ex__(protocol)
    if isinstance(reduced, str):
        return reduced
    function, args, *rest = cast("tuple[Any, ...]", reduced)
    # E.g., `__reduce__()` returning `type(self)` and its arguments
    if function is cls:
        return (original, args, *rest)
    # The class is the first argument of `copyreg._reconstructor` (protocols 0
    # and 1) and of `copyreg.__newobj__` (2+), which insists on the class of
    # the instance
    if function is copyreg.__newobj__:  # type: ignore[attr-defined]  # not in typeshed
        return (_new_instance, (original, *args[1:]), *rest)
    # With keyword arguments, from `__getnewargs_ex__()` (protocols 4+)
    if function is copyreg.__newobj_ex__:  # type: ignore[attr-defined]  # not in typeshed
        return (_new_instance_ex, (original, *args[1:]), *rest)
    if function is copyreg._reconstructor and args[0] is cls:  # type: ignore[attr-defined]  # not in typeshed
        return (function, (original, *args[1:]), *rest)
    return reduced


def _new_instance(cls: type[Any], *args: Any) -> Any:
    """Create an instance of a class without initializing it, for unpickling.

    Args:
        cls: Class.
        *args: Arguments of `__new__()`.

    Returns:
        New instance.
    """
    return cls.__new__(cls, *args)


def _new_instance_ex(
    cls: type[Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """Create an instance of a class without initializing it, for unpickling.

    Args:
        cls: Class.
        args: Positional arguments of `__new__()`.
        kwargs: Keyword arguments of `__new__()`.

    Returns:
        New instance.
    """
    return cls.__new__(cls, *args, **kwargs)


def _deprecate_attribute(app: Flask) -> None:
    """Make access to `app.config.foca` emit a deprecation warning.

    The class of the Flask configuration is replaced by a subclass of it
    whose attribute `foca` is a property, which takes precedence over the
    attribute of the instance, where the configuration stays. A class of
    FOCA's for Flask configurations (`Flask.config_class`) can't do this, as
    Connexion creates the app, with its configuration, and apps may have
    classes of their own. The subclass is pickled as the original class.

    Args:
        app: Flask application instance.
    """
    cls = type(app.config)
    if getattr(cls, _DEPRECATING_MARKER, False):
        return
    with _deprecating_classes_lock:
        if cls not in _deprecating_classes:
            _deprecating_classes[cls] = type(
                cls.__name__,
                (cls,),
                {
                    "__module__": cls.__module__,
                    "__qualname__": cls.__qualname__,
                    "__reduce_ex__": _reduce_ex,
                    _DEPRECATING_MARKER: True,
                    _ATTRIBUTE: property(
                        _get_foca_attribute,
                        _set_foca_attribute,
                        _delete_foca_attribute,
                    ),
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
    # Apps without FOCA configuration get that of the latest FOCA app
    config = _find_app_config(current_app) if has_app_context() else None
    if config is not None:
        return config
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
