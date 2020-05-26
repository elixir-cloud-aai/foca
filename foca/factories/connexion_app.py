"""Factory for creating and configuring Connexion app instances."""

from inspect import stack
import logging
from typing import (Mapping, Optional)

from connexion import App

from foca.errors.errors import handle_bad_request
from foca.config.config_parser import get_conf


# Get logger instance
logger = logging.getLogger(__name__)


def create_connexion_app(config: Optional[Mapping] = None) -> App:
    """Creates and configure Connexion app."""
    # Instantiate Connexion app
    app = App(__name__)
    logger.info("Connexion app created from '{calling_module}'.".format(
        calling_module=':'.join([stack()[1].filename, stack()[1].function])
    ))

    # Workaround for adding a custom handler for `connexion.problem` responses
    # Responses from request and paramater validators are not raised and
    # cannot be intercepted by `add_error_handler`; see here:
    # https://github.com/zalando/connexion/issues/138
    @app.app.after_request
    def _rewrite_bad_request(response):
        if (
            response.status_code == 400 and
            response.data.decode('utf-8').find('"title":') is not None
        ):
            response = handle_bad_request(400)
        return response

    # Configure Connexion app
    if config is not None:
        app = __add_config_to_connexion_app(
            app=app,
            config=config,
        )

    return app


def __add_config_to_connexion_app(
    app: App,
    config: Mapping
) -> App:
    """Adds configuration to Flask app and replaces default Connexion and Flask
    settings."""
    # Replace Connexion app settings
    app.host = get_conf(config, 'server', 'host')
    app.port = get_conf(config, 'server', 'port')
    app.debug = get_conf(config, 'server', 'debug')

    # Replace Flask app settings
    app.app.config['DEBUG'] = app.debug
    app.app.config['ENV'] = get_conf(config, 'server', 'environment')
    app.app.config['TESTING'] = get_conf(config, 'server', 'testing')

    # Log Flask config
    logger.debug('Flask app settings:')
    for (key, value) in app.app.config.items():
        logger.debug('* {}: {}'.format(key, value))

    # Add user configuration to Flask app config
    app.app.config.update(config)

    logger.info('Connexion app configured.')
    return app
