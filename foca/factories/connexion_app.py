"""Factory for creating and configuring Connexion application instances."""

import logging
from io import BytesIO
from ipaddress import ip_address

from connexion import App
from flask import current_app, request
from werkzeug.exceptions import RequestEntityTooLarge

from foca.config.access import set_app_config
from foca.models.config import Config

# Get logger instance
logger = logging.getLogger(__name__)


def create_connexion_app(config: Config | None = None) -> App:
    """Create and configure Connexion application instance.

    Args:
        config: Application configuration.

    Returns:
        Connexion application instance.
    """
    # Instantiate Connexion app
    app = App(
        __name__,
        skip_error_handlers=True,
    )
    logger.debug("Connexion app created.")

    # Configure Connexion app
    if config is not None:
        app = __add_config_to_connexion_app(
            app=app,
            config=config,
        )

    return app


def _is_loopback(host: str) -> bool:
    """Check whether a host is a loopback address.

    Args:
        host: Host name or IP address.

    Returns:
        `True` if `host` is `localhost` or a loopback IP address.
    """
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _check_body_size() -> None:
    """Reject request bodies larger than `MAX_CONTENT_LENGTH` (`413`).

    Registered to run before each request if the size of request bodies is
    limited. Werkzeug rejects bodies with a larger declared size
    (`Content-Length`) only when they are read, and reads bodies without a
    declared size (e.g., chunked) up to the limit only, silently dropping the
    rest. Bodies with a larger declared size are thus rejected here without
    reading them, also for operations that don't read their body, and bodies
    without a declared size are read up to one byte more than the limit, to
    tell whether they are larger. Such bodies are kept in memory for the
    request (parsing them, e.g., as form data, may need about as much again)
    and remain readable via `get_data()`, `form`, `files` and `stream`.

    Raises:
        werkzeug.exceptions.RequestEntityTooLarge: The request body is larger
            than `MAX_CONTENT_LENGTH`.
    """
    limit = current_app.config["MAX_CONTENT_LENGTH"]
    if request.content_length is not None:
        if request.content_length > limit:
            raise RequestEntityTooLarge()
        return
    request.max_content_length = limit + 1
    # cached (`cache=True`, the default), so `get_data()`, `form` and
    # `files` (e.g., as read by Connexion) get the same data
    data = request.get_data()
    if len(data) > limit:
        raise RequestEntityTooLarge()
    # `stream` (a cached property) is consumed; clear it, so that it serves
    # the data again from a new input stream
    request.environ["wsgi.input"] = BytesIO(data)
    del request.stream


def __add_config_to_connexion_app(
    app: App,
    config: Config,
) -> App:
    """Replace default Flask and Connexion settings with FOCA config.

    Args:
        app: Connexion application instance.
        config: Application configuration.

    Returns:
        Connexion application instance with updated configuration.
    """
    conf = config.server

    # replace Connexion app settings
    app.host = conf.host
    app.port = conf.port
    app.debug = conf.debug

    # replace Flask app settings
    app.app.config["DEBUG"] = conf.debug
    app.app.config["TESTING"] = conf.testing
    if conf.max_content_length is not None:
        app.app.config["MAX_CONTENT_LENGTH"] = conf.max_content_length
        # before Connexion's handling, i.e., before auth and validation
        app.app.before_request(_check_body_size)
    if conf.debug and not _is_loopback(conf.host):
        logger.warning(
            f"Debug mode is enabled on host '{conf.host}': the interactive "
            "debugger allows executing arbitrary code. Only use debug mode "
            "for local development."
        )

    # Add user configuration to Flask app config
    set_app_config(app.app, config)

    logger.debug("Connexion app configured.")
    return app
