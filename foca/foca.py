""" Definition of an entry-point function for setting up and initializing a
FOCA based microservice."""

from foca.api.register_openapi import register_openapi
from foca.config.log_config import configure_logging
from foca.database.register_mongodb import register_mongodb
from foca.errors.errors import register_error_handlers
from foca.factories.connexion_app import create_connexion_app
from foca.factories.celery_app import create_celery_app
from foca.security.cors import enable_cors
from foca.config.config_handler import Config
from connexion import App
from typing import Mapping, Any


def foca(config: Mapping[str, Any]
         ) -> App:
    """ Initialization of a FOCA based microservice.

    Args:
        config: A Dict object, parsed from a proper YAML configuration file,
                containing the required FOCA-specific configuration parameters
                (under the reserved `foca` key), along with any
                serivce-specific parameters. It should follow the format shown
                below:

                {
                    foca:
                        db:
                            # Any database parameters
                        api:
                            # Any OpenAPI specifications
                        errors:
                            # Any custom errors
                        celery:
                            # Any Celery parameters for background jobs
                        log:
                            # Any log parameters
                        security:
                            # Any protected endpoints/security parameters
                        server:
                            # Any general Flask/Connexion/Gunicorn parameters
                            # (e.g., host, port...)

                    # Any parameter below this point is service-specifc and
                    will be added to `app.config` without validation

                    service_specific_section_1:
                        param_1: my_param_1
                        param_2: my_param_2
                    service_specific_section_2:
                        param_1: my_param_1
                        param_2: my_param_2
                }

    Returns:
        A Connexion app instance

    Example:

        from foca.foca import foca
        import yaml

        with open('/path/to/config.yaml') as config_file:
            config = yaml.safe_load(config_file)

        app = foca(**config)
    """

    # Configure logger
    configure_logging(config_var='FOCA_CONFIG_LOG')

    # Create a Config class instance for parameters validation
    conf = Config(config)

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
