"""Factory for creating and configuring Connexion application instances."""

import logging
from ipaddress import ip_address

from connexion import App

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
    if conf.debug and not _is_loopback(conf.host):
        logger.warning(
            f"Debug mode is enabled on host '{conf.host}': the interactive "
            "debugger allows executing arbitrary code. Only use debug mode "
            "for local development."
        )

    logger.debug("Flask app settings:")
    for key, value in app.app.config.items():
        logger.debug(f"* {key}: {value}")

    # Add user configuration to Flask app config
    set_app_config(app.app, config)

    logger.debug("Connexion app configured.")
    return app
