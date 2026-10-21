"""Methods to manage permission management configuration."""

import logging
from importlib.resources import files
from pathlib import Path

import yaml
from connexion import App

from foca.api.register_openapi import _connexion_add_api_kwargs
from foca.config.access import get_app_config
from foca.database.register_mongodb import (
    _register_database,
    get_mongo_client,
    get_mongo_db_name,
)
from foca.models.config import (
    DEFAULT_ERROR_MEDIA_TYPE,
    AccessControlConfig,
    CollectionConfig,
    Config,
    ConnexionOptions,
    DBConfig,
    MongoConfig,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_API_SPEC_PATH,
)
from foca.security.access_control.enforcement import AccessControlResolver
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    validate_rule,
)

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
    if (
        access_control_config.keep_group_subjects
        and access_control_config.group_mapping is None
    ):
        logger.warning(
            "'security.access_control.keep_group_subjects' has no effect "
            "without 'security.access_control.group_mapping', as group claim "
            "values are then always used as group subjects."
        )

    if len(set(get_app_config(cnx_app.app).security.auth.issuers)) > 1:
        logger.warning(
            "Several trusted token issuers are configured "
            "('security.auth.issuers'): subjects and group claim values are "
            "not scoped by issuer, so policy rules for a subject (e.g., "
            "'user:<sub>' or 'group:<value>') apply to tokens of all "
            "issuers. Only trust several issuers if they cannot issue the "
            "same subjects or group claim values to different users."
        )

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
    _check_collection_clash(
        mongo_config=mongo_config,
        db_name=access_control_db,
        collection_name=access_control_collection,
    )
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
    get_app_config(cnx_app.app).db = mongo_config

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


def _check_collection_clash(
    mongo_config: MongoConfig,
    db_name: str,
    collection_name: str,
) -> None:
    """Ensure that the policy collection is not an app collection.

    Databases are compared by their effective names (cf.
    :py:func:`foca.database.register_mongodb.get_mongo_db_name`), so that
    app collections are also found if all databases are mapped to the same
    database via environment variable `MONGO_DBNAME`.

    Args:
        mongo_config: App's database configuration, without the policy
            collection.
        db_name: Name of the database of the policy collection, as
            configured.
        collection_name: Name of the policy collection.

    Raises:
        ValueError: An app database with the same effective name as the
            policy collection's database has a collection of the same name.
    """
    effective_db_name = get_mongo_db_name(db_name)
    for app_db_name, app_db_conf in (mongo_config.dbs or {}).items():
        same_db = get_mongo_db_name(app_db_name) == effective_db_name
        if same_db and collection_name in (app_db_conf.collections or {}):
            raise ValueError(
                f"Access control collection '{collection_name}' clashes "
                "with app collection of the same name in database "
                f"'{effective_db_name}'. Set a different "
                "'security.access_control.collection_name' or "
                "'security.access_control.db_name'."
            )


def register_permission_specs(
    app: App, access_control_config: AccessControlConfig
):
    """Register open api specs for permission management.

    Access control policies are enforced on all operations of the permissions
    API, irrespective of `access_control.enforce`. The default specification
    declares the configured media type of error responses
    (`exceptions.media_type`).

    Args:
        app: Connexion application instance.
        access_control_config:
            :py:class:`foca.models.config.AccessControlConfig` instance
            describing access control to be registered with `app`.

    Returns:
        Connexion app with updated permission specifications.
    """
    specification: Path | dict
    if access_control_config.api_specs is None:
        spec_path = str(
            files(ACCESS_CONTROL_BASE_PATH).joinpath(DEFAULT_API_SPEC_PATH)
        )
        specification = Path(spec_path)
        # apps without FOCA configuration use the default media type
        config = getattr(app.app.config, "foca", None)
        media_type = (
            config.exceptions.media_type
            if isinstance(config, Config)
            else DEFAULT_ERROR_MEDIA_TYPE
        )
        # as a dictionary only if needed, keeping Connexion's handling of
        # specification files otherwise
        if media_type != DEFAULT_ERROR_MEDIA_TYPE:
            specification = _with_error_media_type(
                spec_path=specification, media_type=media_type
            )
    else:
        spec_path = access_control_config.api_specs
        specification = Path(spec_path)

    options = ConnexionOptions(
        strict_validation=True,
        validate_responses=True,
        swagger_ui=access_control_config.api_swagger_ui,
        serve_spec=access_control_config.api_serve_spec,
        resolver=AccessControlResolver(
            enforce_all=True,
            self_service=access_control_config.self_service,
        ),
    )
    app.add_api(
        specification=specification,
        **_connexion_add_api_kwargs(options),
    )
    logger.info(f"Permission management API registered from '{spec_path}'.")
    return app


# Fields of OpenAPI 3.x Path Item Objects holding Operation Objects
_HTTP_METHODS = frozenset(
    ("get", "put", "post", "delete", "options", "head", "patch", "trace")
)


def _with_error_media_type(spec_path: Path, media_type: str) -> dict:
    """Load an API specification with another error response media type.

    Args:
        spec_path: Path to an OpenAPI 3.x specification declaring
            `application/problem+json` for its error responses.
        media_type: Media type of error responses.

    Returns:
        Specification, with `media_type` instead of `application/problem+json`
        for all `4xx` and `5xx` responses of its operations (defined in the
        operations, not referenced via `$ref`).
    """
    with spec_path.open(encoding="utf-8") as spec_file:
        spec = yaml.safe_load(spec_file)
    operations = (
        operation
        for path_item in spec["paths"].values()
        for method, operation in path_item.items()
        if method in _HTTP_METHODS
    )
    for operation in operations:
        for status, response in operation.get("responses", {}).items():
            content = response.get("content", {})
            if str(status)[0] in "45" and DEFAULT_ERROR_MEDIA_TYPE in content:
                content[media_type] = content.pop(DEFAULT_ERROR_MEDIA_TYPE)
    return spec


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
    if access_control_config.policies is not None:
        _add_policies(adapter=adapter, policies=access_control_config.policies)
    logger.info("Casbin enforcer registered.")

    return app


def _add_policies(adapter: Adapter, policies: list[list[str]]) -> None:
    """Make the stored configured policy rules match the configured ones.

    All rules are validated before any stored rule is changed, so that
    invalid configurations leave stored rules unchanged.

    Args:
        adapter: Casbin adapter of the app.
        policies: Policy rules, each given as its policy type followed by its
            values.

    Raises:
        ValueError: A rule is invalid for the adapter's model (cf.
            :py:func:`validate_rule`).
    """
    for ptype, *rule in policies:
        validate_rule(ptype, rule, adapter.model)
    adapter.set_configured_policies(policies)
