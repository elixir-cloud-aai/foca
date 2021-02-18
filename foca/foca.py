""" Definition of an entry-point function for setting up and initializing a
FOCA based microservice."""

import logging
from typing import Optional

from connexion import App

from foca.api.register_openapi import register_openapi
from foca.config.config_parser import ConfigParser
from foca.database.register_mongodb import register_mongodb
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.factories.celery_app import create_celery_app
from foca.security.cors import enable_cors

# Get logger instance
logger = logging.getLogger(__name__)


def foca(config: Optional[str] = None) -> App:
    """ Initialize a FOCA-based microservice.

    Args:
        config: Path to a YAML application configuration file. For required
            YAML file structure, see :py:class:`foca.models.config.Config()`.

    Returns:
        Connexion app instance.
    """

    # Parse config parameters and format logging
    conf = ConfigParser(config, format_logs=True).config
    logger.info(f"Log formatting configured.")
    if config:
        logger.info(f"Configuration file '{config}' parsed.")
    else:
        logger.info(f"Default app configuration used.")

    # Create Connexion app
    cnx_app = create_connexion_app(conf)
    logger.info(f"Connexion app created.")

    # Register error handlers
    cnx_app = register_exception_handler(cnx_app)
    logger.info(f"Error handler registered.")

    # Enable cross-origin resource sharing
    if(conf.security.cors.enabled is True):
        enable_cors(cnx_app.app)
        logger.info(f"CORS enabled.")
    else:
        logger.info(f"CORS not enabled.")

    # Register OpenAPI specs
    if conf.api.specs:
        cnx_app = register_openapi(
            app=cnx_app,
            specs=conf.api.specs,
        )
    else:
        logger.info(f"No OpenAPI specifications provided.")

    # Register MongoDB
    if conf.db:
        cnx_app.app.config['FOCA'].db = register_mongodb(
            app=cnx_app.app,
            conf=conf.db,
        )
        logger.info(f"Database registered.")
    else:
        logger.info(f"No database support configured.")

    # Create Celery app
    if conf.jobs:
        create_celery_app(cnx_app.app)
        logger.info(f"Support for background tasks set up.")
    else:
        logger.info(f"No support for background tasks configured.")

    return cnx_app
