"""Resources for cross-origin resource sharing (CORS)."""

import logging

from flask import Flask
from flask_cors import CORS

from foca.models.config import CORSConfig

# Get logger instance
logger = logging.getLogger(__name__)


def enable_cors(app: Flask, conf: CORSConfig | None = None) -> None:
    """Enable cross-origin resource sharing (CORS) for Flask app.

    Args:
        app: Flask application instance.
        conf: CORS settings. Defaults to
            :py:class:`foca.models.config.CORSConfig` defaults.
    """
    conf = CORSConfig() if conf is None else conf
    CORS(
        app,
        origins=conf.allow_origins,
        methods=conf.allow_methods,
        allow_headers=conf.allow_headers,
        expose_headers=conf.expose_headers,
        supports_credentials=conf.allow_credentials,
        max_age=conf.max_age,
    )
    logger.debug(f"Enabled CORS for Flask app: {conf}")
