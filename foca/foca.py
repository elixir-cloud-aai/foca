""" Definition of an entry-point function for setting up and initializing a
FOCA based microservice."""

from connexion import App
from foca.api.register_openapi import register_openapi
from foca.config.config_parser import YAMLConfigParser
from foca.config.log_config import configure_logging
from foca.database.register_mongodb import register_mongodb
from foca.errors.errors import register_error_handlers
from foca.factories.connexion_app import create_connexion_app
from foca.factories.celery_app import create_celery_app
from foca.security.cors import enable_cors


def foca(config: str
         ) -> App:
    """ Initialization of a FOCA based microservice.

    Args:
        config: An absolute path to a YAML application configuration file that
                follows the proper format. To properly structure the YAML file,
                see :py:class:`foca.models.config.Config()`

    Returns:
        A Connexion app instance

    Example:

        app = foca('/path/to/config.yaml')
    """

    # Configure logger
    configure_logging(config_var='FOCA_CONFIG_LOG')

    # Create a Config class instance for parameters validation
    conf = YAMLConfigParser(config)

    # Create Connexion app
    connexion_app = create_connexion_app(conf.dict())

    # Register MongoDB
    connexion_app.app = register_mongodb(connexion_app.app)

    # Register error handlers
    connexion_app = register_error_handlers(connexion_app)

    # Create Celery app and register background task monitoring service
    create_celery_app(connexion_app.app)

    # Register OpenAPI specs
    connexion_app = register_openapi(
        app=connexion_app,
        specs=conf.foca.api
    )

    # Enable cross-origin resource sharing
    enable_cors(connexion_app.app)

    return connexion_app
