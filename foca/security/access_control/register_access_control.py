"""Methods to manage permission management configuration"""

import logging
from connexion import App
from connexion.exceptions import Forbidden
from flask_authz import CasbinEnforcer
from pkg_resources import resource_filename
from typing import (Callable, Optional, Tuple)
from functools import wraps
from flask import current_app
from flask.wrappers import Response

from foca.models.config import (
    DBConfig,
    MongoConfig,
    SpecConfig,
    CollectionConfig,
    AccessControlConfig
)
from foca.database.register_mongodb import add_new_database
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_API_SPEC_PATH
)

logger = logging.getLogger(__name__)


def register_access_control(
    cnx_app: App,
    mongo_config: Optional[MongoConfig],
    access_control_config: AccessControlConfig
) -> App:
    """Register access control configuration with flask app.

    Args:
        cnx_app: Connexion application instance.
        mongo_config: :py:class:`foca.models.config.AccessControlConfig`
            instance describing databases and collections to be registered
            with `app`.
        access_control_config: :py:class:
            `foca.models.config.AccessControlConfig` instance describing
            access control to be registered with `app`.

    Returns:
        Connexion application instance with registered access control config.
    """
    # Register access control database and collection.
    access_db_conf = DBConfig(
        collections={
            access_control_config.collection_name: CollectionConfig()
        },
        client=None
    )

    # Set default db attributes if config not present.
    if mongo_config is None:
        mongo_config = MongoConfig()

    access_control_db = str(access_control_config.db_name)
    if mongo_config.dbs is None:
        mongo_config.dbs = {access_control_db: access_db_conf}
    else:
        mongo_config.dbs[access_control_db] = access_db_conf

    cnx_app.app.config.foca.db = mongo_config

    # Register new database for access control.
    add_new_database(
        app=cnx_app.app,
        conf=mongo_config,
        db_conf=access_db_conf,
        db_name=access_control_db
    )

    # Register access control api specs and corresponding controller.
    cnx_app = register_casbin_enforcer(
        app=cnx_app,
        mongo_config=mongo_config,
        access_control_config=access_control_config
    )

    cnx_app = register_permission_specs(
        app=cnx_app,
        access_control_config=access_control_config
    )

    return cnx_app


def register_permission_specs(
    app: App,
    access_control_config: AccessControlConfig
):
    """Register open api specs for permission management.

    Args:
        app: Connexion application instance.
        access_control_config: :py:class:
            `foca.models.config.AccessControlConfig` instance describing
            access control to be registered with `app`.

    Returns:
        Connexion app with updated permission specifications.
    """
    # Check if default, get package path variables for specs.
    if access_control_config.api_specs is None:
        spec_path = str(resource_filename(
            ACCESS_CONTROL_BASE_PATH, DEFAULT_API_SPEC_PATH
        ))
    else:
        spec_path = access_control_config.api_specs

    spec = SpecConfig(
        path=spec_path,
        add_operation_fields={
            "x-openapi-router-controller": (
                access_control_config.api_controllers
            )
        },
        connexion={
            "strict_validation": True,
            "validate_responses": True,
            "options": {
                "swagger_ui": True,
                "serve_spec": True
            }
        }
    )

    app.add_api(
        specification=spec.path[0],  # type: ignore[index]
        **spec.dict().get("connexion", {}),
    )
    return app


def register_casbin_enforcer(
    app: App,
    access_control_config: AccessControlConfig,
    mongo_config: MongoConfig
) -> App:
    """Method to add casbin permission enforcer.

    Args:
        app: Connexion app.
        access_control_config: :py:class:
            `foca.models.config.AccessControlConfig` instance describing
            access control to be registered with `app`.
        mongo_config: :py:class:`foca.models.config.MongoConfig` instance
            describing databases and collections to be registered with `app`.

    Returns:
        Connexion application instance with registered casbin configuration.
    """
    # Check if default, get package path variables for model.
    access_control_config_model = str(access_control_config.model)
    if access_control_config.api_specs is None:
        casbin_model = str(resource_filename(
            ACCESS_CONTROL_BASE_PATH, access_control_config_model
        ))
    else:
        casbin_model = access_control_config_model

    logger.info("Setting casbin model.")
    app.app.config["CASBIN_MODEL"] = casbin_model

    logger.info("Setting headers for owner operations.")
    app.app.config["CASBIN_OWNER_HEADERS"] = (
        access_control_config.owner_headers
    )

    logger.info("Setting headers for user operations.")
    app.app.config["CASBIN_USER_NAME_HEADERS"] = (
        access_control_config.user_headers
    )

    logger.info("Setting up casbin enforcer.")
    adapter = Adapter(
        uri=f"mongodb://{mongo_config.host}:{mongo_config.port}/",
        dbname=str(access_control_config.db_name),
        collection=access_control_config.collection_name
    )
    app.app.config["casbin_adapter"] = adapter

    return app


def check_permissions(
    _fn: Optional[Callable] = None,
) -> Callable:
    """Decorator for checking user access to resource.

    Returns:
        The decorated function.
    """

    def _decorator_check_permissions(fn):
        """User access decorator. Used to facilitate optional decorator arguments.

        Args:
            fn: The function to be decorated.

        Returns:
            The response returned from the input function.
        """
        @wraps(fn)
        def _wrapper(*args, **kwargs):
            """Wrapper for permissions decorator.

            Args:
                args: positional arguments passed through from
                    `check_permissions`.
                kwargs: keyword arguments passed through from
                    `check_permissions`.

            Returns:
                Wrapper function.
            """
            adapter = current_app.config["casbin_adapter"]
            casbin_enforcer = CasbinEnforcer(current_app, adapter)
            response: Tuple[Response, int] = casbin_enforcer.enforcer(
                func=fn
            )(*args, **kwargs)
            if (
                len(response) == 2 and response[0].status_code == 200
                and response[1] == 401
            ):
                raise Forbidden
            return response

        return _wrapper

    if _fn is None:
        return _decorator_check_permissions
    else:
        return _decorator_check_permissions(_fn)
