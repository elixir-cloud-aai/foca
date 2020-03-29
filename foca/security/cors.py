"""Function enabling cross-origin resource sharing for a Flask app
instance."""

import logging
from flask import Flask

from flask_cors import CORS


# Get logger instance
logger = logging.getLogger(__name__)


def enable_cors(app: Flask) -> None:
    """Enables cross-origin resource sharing for Flask app."""
    CORS(app)
    logger.info('Enabled CORS for Flask app.')
