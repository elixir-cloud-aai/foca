"""Resources for enabling cross-origin resource sharing (CORS) for a Flask
app."""

import logging
from flask import Flask

from flask_cors import CORS

# Get logger instance
logger = logging.getLogger(__name__)


def enable_cors(app: Flask) -> None:
    """Enables cross-origin resource sharing for Flask app.

    Args:
        app: Flask application.
    """
    CORS(app)
    logger.debug('Enabled CORS for Flask app.')
