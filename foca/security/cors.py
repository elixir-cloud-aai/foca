"""Resources for cross-origin resource sharing (CORS)."""

import logging

from flask import Flask
from flask_cors import CORS

from foca.deprecation import warn_deprecated
from foca.models.config import CORSConfig

# Get logger instance
logger = logging.getLogger(__name__)


def enable_cors(app: Flask, conf: CORSConfig | None = None) -> None:
    """Enable cross-origin resource sharing (CORS) for Flask app.

    Deprecated, removed in the ASGI release: FOCA enables CORS for the apps it
    creates, as set in `security.cors` of their configuration.

    Args:
        app: Flask application instance.
        conf: CORS settings. Defaults to
            :py:class:`foca.models.config.CORSConfig` defaults.
    """
    warn_deprecated(
        "'enable_cors()' is deprecated and removed in the ASGI release; FOCA "
        "enables CORS as set in 'security.cors' of the configuration.",
        stacklevel=2,
    )
    _enable_cors(app, conf)


def _enable_cors(app: Flask, conf: CORSConfig | None = None) -> None:
    """Enable cross-origin resource sharing (CORS) for Flask app.

    For FOCA's own use.

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
    logger.debug("Enabled CORS for Flask app: %s", conf)
