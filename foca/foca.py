"""Class for setting up and initializing a FOCA-based microservice."""

import logging
from pathlib import Path
from typing import Optional

from celery import Celery
from connexion import App

from foca.security.access_control.register_access_control import (
    register_access_control,
)
from foca.security.access_control.constants import (
    DEFAULT_SPEC_CONTROLLER,
    DEFAULT_ACCESS_CONTROL_DB_NAME,
    DEFAULT_ACESS_CONTROL_COLLECTION_NAME,
)
from foca.api.register_openapi import register_openapi
from foca.config.config_parser import ConfigParser
from foca.database.register_mongodb import register_mongodb
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.factories.celery_app import create_celery_app
from foca.security.cors import enable_cors

# Get logger instance
logger = logging.getLogger(__name__)


class Foca:
    def __init__(
        self,
        config_file: Optional[Path] = None,
        custom_config_model: Optional[str] = None,
    ) -> None:
        """Instantiate FOCA class.

        Args:
            config_file: Path to application configuration file in YAML
                format.  Cf. :py:class:`foca.models.config.Config` for
                required file structure.
            custom_config_model: Path to model to be used for custom config
                parameter validation, supplied in "dot notation", e.g.,
                ``myapp.config.models.CustomConfig`, where ``CustomConfig``
                is the actual importable name of a `pydantic` model for
                your custom configuration, deriving from ``BaseModel``.
                FOCA will attempt to instantiate the model with the values
                passed to the ``custom`` section in the application's
                configuration, if present. Wherever possible, make sure
                that default values are supplied for each config
                parameters, so as to make it easier for others to
                write/modify their app configuration.

        Attributes:
            config_file: Path to application configuration file in YAML
                format.  Cf. :py:class:`foca.models.config.Config` for
                required file structure.
            custom_config_model: Path to model to be used for custom config
                parameter validation, supplied in "dot notation", e.g.,
                ``myapp.config.models.CustomConfig`, where ``CustomConfig``
                is the actual importable name of a `pydantic` model for
                your custom configuration, deriving from ``BaseModel``.
                FOCA will attempt to instantiate the model with the values
                passed to the ``custom`` section in the application's
                configuration, if present. Wherever possible, make sure
                that default values are supplied for each config
                parameters, so as to make it easier for others to
                write/modify their app configuration.
            conf: App configuration. Instance of
                :py:class:`foca.models.config.Config`.
        """
        self.config_file: Optional[Path] = (
            Path(config_file) if config_file is not None else None
        )
        self.custom_config_model: Optional[str] = custom_config_model
        self.conf = ConfigParser(
            config_file=self.config_file,
            custom_config_model=self.custom_config_model,
            format_logs=True,
        ).config
        logger.info("Log formatting configured.")
        if self.config_file is not None:
            logger.info(f"Configuration file '{self.config_file}' parsed.")
        else:
            logger.info("Default app configuration used.")

    def create_app(self) -> App:
        """Set up and initialize FOCA-based Connexion app.

        Returns:
            Connexion application instance.
        """
        # Create Connexion app
        cnx_app = create_connexion_app(self.conf)
        logger.info("Connexion app created.")

        # Register error handlers
        cnx_app = register_exception_handler(cnx_app)
        logger.info("Error handler registered.")

        # Enable cross-origin resource sharing
        if self.conf.security.cors.enabled is True:
            enable_cors(cnx_app.app)
            logger.info("CORS enabled.")
        else:
            logger.info("CORS not enabled.")

        # Register OpenAPI specs
        if self.conf.api.specs:
            cnx_app = register_openapi(
                app=cnx_app,
                specs=self.conf.api.specs,
            )
        else:
            logger.info("No OpenAPI specifications provided.")

        # Register MongoDB
        if self.conf.db:
            cnx_app.app.config.foca.db = register_mongodb(
                app=cnx_app.app,
                conf=self.conf.db,
            )
            logger.info("Database registered.")
        else:
            logger.info("No database support configured.")

        # Register permission management and casbin enforcer
        if self.conf.security.auth.required:
            if (
                self.conf.security.access_control.api_specs is None
                or self.conf.security.access_control.api_controllers is None
            ):
                self.conf.security.access_control.api_controllers = (
                    DEFAULT_SPEC_CONTROLLER
                )

            if self.conf.security.access_control.db_name is None:
                self.conf.security.access_control.db_name = (
                    DEFAULT_ACCESS_CONTROL_DB_NAME
                )

            if self.conf.security.access_control.collection_name is None:
                self.conf.security.access_control.collection_name = (
                    DEFAULT_ACESS_CONTROL_COLLECTION_NAME
                )

            cnx_app = register_access_control(
                cnx_app=cnx_app,
                mongo_config=self.conf.db,
                access_control_config=self.conf.security.access_control,
            )
        else:
            if (
                self.conf.security.access_control.api_specs
                or self.conf.security.access_control.api_controllers
            ):
                logger.error(
                    "Please enable security config to register access control."
                )

        return cnx_app

    def create_celery_app(self) -> Celery:
        """Set up and initialize FOCA-based Celery app.

        Returns:
            Celery application instance.
        """
        # Create Connexion app
        cnx_app = create_connexion_app(self.conf)
        logger.info("Connexion app created.")

        # Register error handlers
        cnx_app = register_exception_handler(cnx_app)
        logger.info("Error handler registered.")

        # Register MongoDB
        if self.conf.db:
            cnx_app.app.config.foca.db = register_mongodb(
                app=cnx_app.app,
                conf=self.conf.db,
            )
            logger.info("Database registered.")
        else:
            logger.info("No database support configured.")

        # Create Celery app
        if self.conf.jobs:
            celery_app = create_celery_app(cnx_app.app)
            logger.info("Support for background tasks set up.")
        else:
            raise ValueError(
                "No support for background tasks configured. Please use the "
                "'jobs' keyword section in your configuration file."
            )

        return celery_app
