"""Resources for cross-origin resource sharing (CORS)."""

import logging
from flask import Flask

from flask_cors import CORS

# Get logger instance
logger = logging.getLogger(__name__)


def enable_cors(app: Flask) -> None:
    """Enables cross-origin resource sharing (CORS) for Flask application
    instance.

    Args:
        app: Flask application instance.
    """
    CORS(app)
    logger.debug('Enabled CORS for Flask app.')
