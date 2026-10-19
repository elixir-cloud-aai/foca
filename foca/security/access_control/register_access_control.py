"""Methods to manage permission management configuration."""

import logging
from importlib.resources import files
from pathlib import Path

from connexion import App

from foca.database.register_mongodb import (
    _register_database,
    get_mongo_client,
    get_mongo_db_name,
)
from foca.models.config import (
    AccessControlConfig,
    CollectionConfig,
    DBConfig,
    MongoConfig,
    SpecConfig,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_API_SPEC_PATH,
)
from foca.security.access_control.enforcement import AccessControlResolver
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter

logger = logging.getLogger(__name__)


def register_access_control(
    cnx_app: App,
    mongo_config: MongoConfig | None,
    access_control_config: AccessControlConfig,
) -> App:
    """Register access control configuration with flask app.

    Args:
        cnx_app: Connexion application instance.
        mongo_config: :py:class:`foca.models.config.AccessControlConfig`
            instance describing databases and collections to be registered
            with `app`.
        access_control_config:
            :py:class:`foca.models.config.AccessControlConfig` instance
            describing access control to be registered with `app`.

    Returns:
        Connexion application instance with registered access control config.
    """
    # Set default db attributes if config not present.
    if mongo_config is None:
        mongo_config = MongoConfig()
    if mongo_config.dbs is None:
        mongo_config.dbs = {}

    access_control_db = access_control_config.db_name
    access_control_collection = access_control_config.collection_name

    # Remove policy collection (and database) added by a previous
    # registration with the same configuration (e.g., when creating an app
    # repeatedly), so that it is not mistaken for an app collection
    registered = access_control_config._registered_collection
    if registered is not None:
        registered_db, registered_collection, added_db = registered
        registered_db_conf = mongo_config.dbs.get(registered_db)
        if added_db:
            mongo_config.dbs.pop(registered_db, None)
        elif (
            registered_db_conf is not None
            and registered_db_conf.collections is not None
        ):
            registered_db_conf.collections.pop(registered_collection, None)

    access_control_config._registered_collection = None
    app_db_conf = mongo_config.dbs.get(access_control_db)
    added_db = app_db_conf is None

    if app_db_conf is None:
        # Register new database for access control.
        access_db_conf = DBConfig(
            collections={access_control_collection: CollectionConfig()},
            client=None,
        )
        mongo_config.dbs[access_control_db] = access_db_conf
        _register_database(
            client=get_mongo_client(mongo_config),
            db_name=access_control_db,
            db_conf=access_db_conf,
        )
    else:
        # Add policy collection to app database of the same name, keeping
        # the app's collections.
        if app_db_conf.collections is None:
            app_db_conf.collections = {}
        if access_control_collection in app_db_conf.collections:
            raise ValueError(
                f"Access control collection '{access_control_collection}' "
                f"clashes with app collection of the same name in database "
                f"'{access_control_db}'. Set a different "
                "'security.access_control.collection_name' or "
                "'security.access_control.db_name'."
            )
        policy_collection = CollectionConfig()
        app_db_conf.collections[access_control_collection] = policy_collection
        if app_db_conf.client is None:
            _register_database(
                client=get_mongo_client(mongo_config),
                db_name=access_control_db,
                db_conf=app_db_conf,
            )
        else:
            policy_collection.client = app_db_conf.client[
                access_control_collection
            ]
        logger.info(
            f"Added access control collection '{access_control_collection}' "
            f"to database '{access_control_db}'."
        )

    # Remember what was added, once registration of the database succeeded
    access_control_config._registered_collection = (
        access_control_db,
        access_control_collection,
        added_db,
    )
    cnx_app.app.config.foca.db = mongo_config

    # Register access control api specs and corresponding controller.
    cnx_app = register_casbin_enforcer(
        app=cnx_app,
        mongo_config=mongo_config,
        access_control_config=access_control_config,
    )

    cnx_app = register_permission_specs(
        app=cnx_app, access_control_config=access_control_config
    )

    return cnx_app


def register_permission_specs(
    app: App, access_control_config: AccessControlConfig
):
    """Register open api specs for permission management.

    Access control policies are enforced on all operations of the permissions
    API, irrespective of `access_control.enforce`.

    Args:
        app: Connexion application instance.
        access_control_config:
            :py:class:`foca.models.config.AccessControlConfig` instance
            describing access control to be registered with `app`.

    Returns:
        Connexion app with updated permission specifications.
    """
    if access_control_config.api_specs is None:
        spec_path = str(
            files(ACCESS_CONTROL_BASE_PATH).joinpath(DEFAULT_API_SPEC_PATH)
        )
    else:
        spec_path = access_control_config.api_specs

    spec = SpecConfig(
        path=Path(spec_path),
        connexion={
            "strict_validation": True,
            "validate_responses": True,
            "options": {
                "swagger_ui": access_control_config.api_swagger_ui,
                "serve_spec": access_control_config.api_serve_spec,
            },
        },
    )

    app.add_api(
        specification=spec.path[0],  # type: ignore[index]
        resolver=AccessControlResolver(
            enforce_all=True,
            self_service=access_control_config.self_service,
        ),
        **spec.model_dump().get("connexion", {}),
    )
    logger.info(f"Permission management API registered from '{spec_path}'.")
    return app


def register_casbin_enforcer(
    app: App,
    access_control_config: AccessControlConfig,
    mongo_config: MongoConfig,
) -> App:
    """Method to add casbin permission enforcer.

    Args:
        app: Connexion app.
        access_control_config:
            :py:class:`foca.models.config.AccessControlConfig` instance
            describing access control to be registered with `app`.
        mongo_config: :py:class:`foca.models.config.MongoConfig` instance
            describing databases and collections to be registered with `app`.

    Returns:
        Connexion application instance with registered casbin configuration.
    """
    # Check if default, get package path variables for model.
    casbin_model = access_control_config.model

    logger.info("Setting casbin model.")
    app.app.config["CASBIN_MODEL"] = casbin_model

    logger.info("Setting up casbin enforcer.")
    # Same client and collection as registered for the app's databases
    collection = get_mongo_client(mongo_config)[
        get_mongo_db_name(access_control_config.db_name)
    ][access_control_config.collection_name]
    adapter = Adapter(
        collection=collection,
        id_charset=access_control_config.id_charset,
        id_length=access_control_config.id_length,
        model=casbin_model,
    )
    app.app.config["casbin_adapter"] = adapter
    logger.info("Casbin enforcer registered.")

    return app
