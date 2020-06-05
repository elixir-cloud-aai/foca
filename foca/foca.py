"""FOCA class definition"""

from foca.api.register_openapi import (register_openapi, OpenAPIConfig)
from foca.config.config_parser import (get_conf, get_conf_type)
from foca.config.log_config import configure_logging
from foca.database.register_mongodb import register_mongodb
from foca.errors.errors import register_error_handlers
from foca.factories.connexion_app import create_connexion_app
from foca.security.cors import enable_cors
from connexion import App
from foca.config.config_handler import Config
from typing import Iterable


class FOCA():
    """Description"""

    def __init__(self,
                 config_in: Iterable[str, OpenAPIConfig]
                 ) -> None:
        """Description"""

        self._config = config_in

        # Configure logger
        configure_logging(config_var='FOCA_CONFIG_LOG')

        # TODO: validate configuration object's sections

        # Create Connexion app
        self._connexion_app = create_connexion_app(self._config)

        # Register MongoDB
        self._connexion_app.app = register_mongodb(self._connexion_app.app)

        # Register error handlers
        self._connexion_app = register_error_handlers(self._connexion_app)

        # Create Celery app and register background task monitoring service
        # register_task_service(self._connexion_app.app)

        # Register OpenAPI specs
        self._connexion_app = register_openapi(
            app=self._connexion_app,
            specs_in=self._config['specs']
        )

        # Enable cross-origin resource sharing
        enable_cors(self._connexion_app.app)

    def __new__(cls,
                *args,
                **kwargs):
        """__new__ method override for returning the Connexion app object at FOCA class instance creation"""
        
        instance = super(FOCA, cls).__new__(cls, *args, **kwargs)
        return instance._connexion_app
