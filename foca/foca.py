"""Entry point for setting up and initializing a FOCA-based microservice."""

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


def foca(
    config: Optional[str] = None,
    custom_config_model: Optional[str] = None,
) -> App:
    """Set up and initialize FOCA-based microservice.

    Args:
        config: Path to application configuration file in YAML format. Cf.
            :py:class:`foca.models.config.Config` for required file structure.
        custom_config_model: Path to model to be used for custom config
            parameter validation, supplied in "dot notation", e.g.,
            ``myapp.config.models.CustomConfig`, where ``CustomConfig`` is the
            actual importable name of a `pydantic` model for your custom
            configuration, deriving from ``BaseModel``. FOCA will attempt to
            instantiate the model with the values passed to the ``custom``
            section in the application's configuration, if present. Wherever
            possible, make sure that default values are supplied for each
            config parameters, so as to make it easier for others to
            write/modify their app configuration.

    Returns:
        Connexion application instance.
    """

    # Parse config parameters and format logging
    conf = ConfigParser(
        config_file=config,
        custom_config_model=custom_config_model,
        format_logs=True,
    ).config
    logger.info("Log formatting configured.")
    if config is not None:
        logger.info(f"Configuration file '{config}' parsed.")
    else:
        logger.info("Default app configuration used.")

    # Create Connexion app
    cnx_app = create_connexion_app(conf)
    logger.info("Connexion app created.")

    # Register error handlers
    cnx_app = register_exception_handler(cnx_app)
    logger.info("Error handler registered.")

    # Enable cross-origin resource sharing
    if(conf.security.cors.enabled is True):
        enable_cors(cnx_app.app)
        logger.info("CORS enabled.")
    else:
        logger.info("CORS not enabled.")

    # Register OpenAPI specs
    if conf.api.specs:
        cnx_app = register_openapi(
            app=cnx_app,
            specs=conf.api.specs,
        )
    else:
        logger.info("No OpenAPI specifications provided.")

    # Register MongoDB
    if conf.db:
        cnx_app.app.config.foca.db = register_mongodb(
            app=cnx_app.app,
            conf=conf.db,
        )
        logger.info("Database registered.")
    else:
        logger.info("No database support configured.")

    # Create Celery app
    if conf.jobs:
        create_celery_app(cnx_app.app)
        logger.info("Support for background tasks set up.")
    else:
        logger.info("No support for background tasks configured.")

    return cnx_app
