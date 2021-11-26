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
from foca.permission_management.config_utils import _create_permission_config, _register_permission_specs, _register_casbin_enforcer

# Get logger instance
logger = logging.getLogger(__name__)


def foca(config: Optional[str] = None) -> App:
    """Set up and initialize FOCA-based microservice.

    Args:
        config: Path to application configuration file in YAML format. Cf.
            :py:class:`foca.models.config.Config` for required file structure.

    Returns:
        Connexion application instance.
    """

    # Parse config parameters and format logging
    conf = ConfigParser(config, format_logs=True).config
    logger.info(f"Log formatting configured.")
    if config:
        logger.info(f"Configuration file '{config}' parsed.")
    else:
        logger.info(f"Default app configuration used.")

    # Add permission specs
    conf = _create_permission_config(conf)

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
        
    if conf.access and conf.access.enable:
        cnx_app = _register_permission_specs(cnx_app)
        logger.info(f"Permission management specs registered.")
    else:
        logger.info(f"No access specifications provided.")

    # Register MongoDB
    if conf.db:
        cnx_app.app.config['FOCA'].db = register_mongodb(
            app=cnx_app.app,
            conf=conf.db,
        )
        logger.info(f"Database registered.")
    else:
        logger.info(f"No database support configured.")

    # Register permission management and casbin enforcer
    if conf.access and conf.access.enable:
        cnx_app = _register_casbin_enforcer(
            app=cnx_app,
            policy_path=conf.access.policy_path,
            owner_headers=conf.access.owner_headers,
            user_headers=conf.access.user_headers,
            db_name="access_db",
            db_host=conf.db.host,
            db_port=conf.db.port
        )
        logger.info(f"Casbin enforcer registered.")

    # Create Celery app
    if conf.jobs:
        create_celery_app(cnx_app.app)
        logger.info(f"Support for background tasks set up.")
    else:
        logger.info(f"No support for background tasks configured.")

    return cnx_app
