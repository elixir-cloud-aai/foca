"""Factory for creating and configuring Connexion app instances."""

from inspect import stack
import logging
from typing import Optional

from connexion import App
from werkzeug.exceptions import BadRequest

from foca.errors.exceptions import handle_problem
from foca.models.config import Config

# Get logger instance
logger = logging.getLogger(__name__)


def create_connexion_app(config: Optional[Config] = None) -> App:
    """Create and configure Connexion app."""
    # Instantiate Connexion app
    app = App(__name__)

    calling_module = ':'.join([stack()[1].filename, stack()[1].function])
    logger.debug(f"Connexion app created from '{calling_module}'.")

    # Workaround for adding a custom handler for `connexion.problem` responses
    # Responses from request and paramater validators are not raised and
    # cannot be intercepted by `add_error_handler`; see here:
    # https://github.com/zalando/connexion/issues/138
    #@app.app.after_request
    #def _rewrite_bad_request(response):
    #    if (
    #        response.status_code == 400 and
    #        response.data.decode('utf-8').find('"title":') is not None
    #    ):
    #        response = handle_problem(BadRequest)
    #    return response

    # Configure Connexion app
    if config is not None:
        app = __add_config_to_connexion_app(
            app=app,
            config=config,
        )

    return app


def __add_config_to_connexion_app(
    app: App,
    config: Config,
) -> App:
    """Replace default Connexion and Flask app settings and add FOCA- and user-
    defined configuration parameters to Flask app.

    Args:
        app: Connexion app.
        config: App configuration.

    Returns:
        Connexion app.
    """
    conf = config.server

    # replace Connexion app settings
    app.host = conf.host
    app.port = conf.port
    app.debug = conf.debug

    # replace Flask app settings
    app.app.config['DEBUG'] = conf.debug
    app.app.config['ENV'] = conf.environment
    app.app.config['TESTING'] = conf.testing

    logger.debug('Flask app settings:')
    for (key, value) in app.app.config.items():
        logger.debug('* {}: {}'.format(key, value))

    # Add user configuration to Flask app config
    app.app.config['FOCA'] = config

    logger.debug('Connexion app configured.')
    return app
