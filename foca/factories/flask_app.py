"""Factory for creating and configuring Connexion application instances."""

from flask import Config, Flask
from inspect import stack
import logging
from typing import Optional

from foca.models.config import Config as FocaConfig

# Get logger instance
logger = logging.getLogger(__name__)


class FocaFlaskAppConfig(Config):
    """Custom config class wrapper to include foca as an attribute
    within config.
    """
    foca: Optional[FocaConfig]


def create_flask_app() -> Flask:
    """Create and configure Flask application instance for connexion
    context.

    Returns:
        Flask application with custom foca config configured.
    """

    flask_app = Flask(__name__)
    flask_app.config.from_object(FocaFlaskAppConfig)

    calling_module = ':'.join([stack()[1].filename, stack()[1].function])
    logger.debug(f"Flask app created from '{calling_module}'.")

    return flask_app
