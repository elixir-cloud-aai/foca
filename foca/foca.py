"""Class for setting up and initializing a FOCA-based microservice."""

import logging
from collections.abc import Sequence
from pathlib import Path

from celery import Celery
from connexion import App

from foca.api.register_openapi import register_openapi
from foca.api.service_info import check_service_info
from foca.config.config_parser import ConfigParser
from foca.database.register_mongodb import register_mongodb
from foca.errors.exceptions import register_exception_handler
from foca.factories.celery_app import create_celery_app
from foca.factories.connexion_app import create_connexion_app
from foca.security.access_control.register_access_control import (
    register_access_control,
)
from foca.security.cors import enable_cors

# Get logger instance
logger = logging.getLogger(__name__)


class Foca:
    """Create Connexion and Celery apps from a FOCA configuration."""

    def __init__(
        self,
        config_file: Path | str | Sequence[Path | str] | None = None,
        custom_config_model: str | None = None,
    ) -> None:
        """Instantiate FOCA class.

        Args:
            config_file: Path to application configuration file in YAML
                format, or sequence of paths to configuration files that are
                merged in order. Files listed in environment variable
                `FOCA_CONFIG_OVERLAY` are merged afterwards. Relative file
                paths in the configuration are resolved against the directory
                of the first file. Cf. :py:class:`foca.models.config.Config`
                for required file structure.
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
            config_file: Path(s) to application configuration file(s), as
                passed.
            config_files: Paths to all merged configuration files, including
                overlays from `FOCA_CONFIG_OVERLAY`.
            config_dir: Directory that relative file paths in the
                configuration are resolved against, i.e., the directory of
                the first configuration file, or the current working
                directory if no file was passed. Apps can use it to resolve
                paths in their custom configuration the same way.
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
        self.config_file = config_file
        self.custom_config_model: str | None = custom_config_model
        parser = ConfigParser(
            config_file=self.config_file,
            custom_config_model=self.custom_config_model,
            format_logs=True,
        )
        self.conf = parser.config
        self.config_files: list[Path] = parser.config_files
        self.config_dir: Path = parser.config_dir
        logger.info("Log formatting configured.")
        if not self.config_files:
            logger.info("Default app configuration used.")

    def _create_base_app(self) -> App:
        """Create Connexion app with error handler and databases.

        Returns:
            Connexion application instance.
        """
        # Create Connexion app
        cnx_app = create_connexion_app(self.conf)
        logger.info("Connexion app created.")

        # Register error handlers
        cnx_app = register_exception_handler(cnx_app, self.conf.exceptions)
        logger.info("Error handler registered.")

        # Register MongoDB
        if self.conf.db:
            cnx_app.app.config.foca.db = register_mongodb(self.conf.db)
            logger.info("Database registered.")
        else:
            logger.info("No database support configured.")

        return cnx_app

    def create_app(self) -> App:
        """Set up and initialize FOCA-based Connexion app.

        Returns:
            Connexion application instance.

        Raises:
            foca.api.service_info.InvalidServiceInfo: The service info,
                including fields computed at runtime, is invalid, or the API
                specifications route operations to
                `foca.api.service_info.getServiceInfo`, but no service info is
                configured.
            ValueError: Operations routed to the controllers of
                :py:mod:`foca.security.access_control.ownership_server` do not
                enforce access control policies, group settings keep all
                group mappings from applying, or no admin is configured and
                `security.access_control.require_admin` is set (cf.
                :py:func:`foca.security.access_control.register_access_control.register_access_control`).
        """
        cnx_app = self._create_base_app()

        # Enable cross-origin resource sharing
        if self.conf.security.cors.enabled is True:
            enable_cors(cnx_app.app, self.conf.security.cors)
            logger.info("CORS enabled.")
        else:
            logger.info("CORS not enabled.")

        # Register OpenAPI specs
        if self.conf.api.specs:
            cnx_app = register_openapi(
                app=cnx_app,
                specs=self.conf.api.specs,
                enforce_access_control=(self.conf.security.access_control.enforce),
            )
        else:
            logger.info("No OpenAPI specifications provided.")

        # Register permission management and casbin enforcer
        if self.conf.security.access_control.enabled:
            cnx_app = register_access_control(
                cnx_app=cnx_app,
                mongo_config=self.conf.db,
                access_control_config=self.conf.security.access_control,
            )
            logger.info("Access control registered.")
        else:
            logger.info("Access control not enabled.")

        # Check service info, including fields computed at runtime, and that
        # it is configured if served
        check_service_info(cnx_app.app)

        return cnx_app

    def create_celery_app(self) -> Celery:
        """Set up and initialize FOCA-based Celery app.

        Returns:
            Celery application instance.
        """
        cnx_app = self._create_base_app()

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
