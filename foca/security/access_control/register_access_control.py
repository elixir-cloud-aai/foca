"""Methods to manage permission management configuration."""

import logging
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

import pymongo
import yaml
from connexion import App
from pymongo.errors import PyMongoError

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
    AuthConfig,
    CollectionConfig,
    Config,
    ConnexionOptions,
    CORSConfig,
    DBConfig,
    MongoConfig,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    ADMIN_ROLE,
    CHANGES_COLLECTION_SUFFIX,
    CONFIGURED_POLICIES_CHECK_TIMEOUT,
    DEFAULT_API_SPEC_PATH,
    GROUP_PREFIX,
)
from foca.security.access_control.enforcement import AccessControlResolver
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    validate_rule,
)
from foca.security.access_control.ownership_server import _check_routes_enforced
from foca.utils.logging import escape_log_value

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

    Raises:
        ValueError: Operations of the permissions API routed to the
            controllers of
            :py:mod:`foca.security.access_control.ownership_server` do not
            enforce access control policies, group settings keep all group
            mappings from applying (cf. :py:func:`_check_group_config`), or
            no group or configured policy rule grants the role `admin` and
            `access_control.require_admin` is set (cf.
            :py:func:`_check_admin_role`).
    """
    auth_config = get_app_config(cnx_app.app).security.auth
    _check_group_config(access_control_config, auth_config)
    _check_admin_role(access_control_config, auth_config)

    # Group claim values of several issuers could collide
    if sum(map(bool, _get_group_claims(access_control_config, auth_config))) > 1:
        logger.warning(
            "Several trusted token issuers with group claims are configured "
            "('security.auth.issuers'): subjects are namespaced by issuer, "
            "but group claim values are not, so policy rules for a group "
            "(e.g., 'group:<value>') and group mappings apply to tokens of "
            "all these issuers. Only take groups from several issuers if "
            "they cannot issue the same group claim values to different "
            "users; otherwise set 'claim_groups: \"\"' for the others."
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
            policy_collection.client = app_db_conf.client[access_control_collection]
        logger.info(
            "Added access control collection '%s' to database '%s'.",
            access_control_collection,
            access_control_db,
        )

    # Remember what was added, once registration of the database succeeded
    access_control_config._registered_collection = (
        access_control_db,
        access_control_collection,
        added_db,
    )
    get_app_config(cnx_app.app).db = mongo_config

    # Register access control api specs and corresponding controller, and
    # check them before configured policies are stored, so that a failed
    # startup does not change them
    cnx_app = register_permission_specs(
        app=cnx_app, access_control_config=access_control_config
    )
    _check_routes_enforced(cnx_app.app)

    cnx_app = register_casbin_enforcer(
        app=cnx_app,
        mongo_config=mongo_config,
        access_control_config=access_control_config,
    )

    return cnx_app


def _get_group_claims(
    access_control_config: AccessControlConfig, auth_config: AuthConfig | None
) -> list[str | None]:
    """Get the group claims of the tokens of all trusted issuers.

    Args:
        access_control_config: Access control configuration.
        auth_config: Authentication configuration; if `None`, without
            settings of issuers.

    Returns:
        Group claim of each trusted issuer (its `claim_groups`, by default
        `access_control.claim_groups`), or only `access_control.claim_groups`
        if no issuers are trusted.
    """
    default = access_control_config.claim_groups
    issuers = {} if auth_config is None else auth_config.get_issuer_configs()
    if not issuers:
        return [default]
    return [
        default if conf.claim_groups is None else conf.claim_groups
        for conf in issuers.values()
    ]


def _check_group_config(
    access_control_config: AccessControlConfig,
    auth_config: AuthConfig | None = None,
) -> None:
    """Check that groups and their roles can apply.

    Settings that keep all group mappings from applying make the app refuse
    to start; settings that keep groups or their roles from applying in
    other ways are warned about.

    Args:
        access_control_config: Access control configuration.
        auth_config: Authentication configuration, for group claims of
            issuers (`auth.issuers[].claim_groups`).

    Raises:
        ValueError: `claim_groups` has surrounding whitespace, or
            `group_mapping` maps groups, but no group claim is set, neither
            `claim_groups` nor that of any trusted issuer.
    """
    conf = access_control_config
    if conf.keep_group_subjects and conf.group_mapping is None:
        logger.warning(
            "'security.access_control.keep_group_subjects' has no effect "
            "without 'security.access_control.group_mapping', as group claim "
            "values are then always used as group subjects."
        )
    claim_groups = conf.claim_groups
    # FOCA looks the claim up as written
    if claim_groups is not None and claim_groups != claim_groups.strip():
        raise ValueError(
            "'security.access_control.claim_groups' has surrounding "
            f"whitespace ({claim_groups!r}), but is looked up as written, so "
            "users would have no groups: remove the whitespace."
        )
    has_group_claim = any(_get_group_claims(conf, auth_config))
    if conf.group_mapping and not has_group_claim:
        raise ValueError(
            "Groups are mapped to roles "
            "('security.access_control.group_mapping'), but no group claim "
            "is set, so no mapping would apply: set "
            "'security.access_control.claim_groups' (or "
            "'security.auth.issuers[].claim_groups') to the claim with the "
            "groups of users (and add it to 'security.auth.userinfo_claims', "
            "or those of the issuer, if the identity provider releases it via "
            "user info only), or unset "
            "'security.access_control.group_mapping'."
        )
    # An empty mapping without a group claim is allowed
    elif conf.group_mapping is not None and not has_group_claim:
        logger.warning(
            "'security.access_control.group_mapping' is set (empty), but no "
            "group claim is set ('security.access_control.claim_groups' or "
            "'security.auth.issuers[].claim_groups'), so users have no "
            "groups."
        )
    # Unmapped group claim values are dropped once a mapping is set, also an
    # empty one
    if conf.group_mapping is not None and not conf.keep_group_subjects:
        logger.warning(
            "'security.access_control.group_mapping' is set, but "
            "'security.access_control.keep_group_subjects' is not, so group "
            "claim values are not used as group subjects: policy rules and "
            "roles on resources granted to groups ('group:<value>') don't "
            "apply, and the resource ownership helpers refuse to grant them."
        )


def check_opt_in_roles(
    access_control_config: AccessControlConfig, cors_config: CORSConfig
) -> None:
    """Log the opt-in roles, and warn if nobody or no browser can ask for them.

    Run by `Foca.create_app()` also without access control, as gateway
    plugins use the roles, too. Warns of opt-in roles that no group is mapped
    to (`group_mapping`), which nobody can opt in to, and of a header of
    opt-in roles that cross-origin requests may not send.

    Args:
        access_control_config: Access control configuration.
        cors_config: CORS configuration.
    """
    conf = access_control_config
    header = conf.opt_in_header
    logger.info("Opt-in roles: %s (header '%s').", ", ".join(conf.opt_in_roles), header)
    mapped = set((conf.group_mapping or {}).values())
    unmapped = [role for role in conf.opt_in_roles if role not in mapped]
    if unmapped:
        logger.warning(
            "No group is mapped to the opt-in roles "
            "('security.access_control.group_mapping'): %s. No user can opt "
            "in to them until groups are mapped to them (service clients "
            "mapped to them always have them).",
            ", ".join(unmapped),
        )
    if not cors_config.enabled:
        return
    if not any(_allows(value, header) for value in cors_config.allow_headers):
        logger.warning(
            "The header of opt-in roles ('security.access_control.opt_in_header') "
            "is not allowed in cross-origin requests: add it to "
            "'security.cors.allow_headers', so that browser apps can ask for "
            "opt-in roles."
        )


# Characters that make entries of `allow_headers` patterns, as for flask-cors
_REGEX_CHARS = frozenset("*\\]?$^[()")


def _allows(entry: str, header: str) -> bool:
    """Check whether an entry of `security.cors.allow_headers` allows a header.

    As flask-cors does, entries with characters of regular expressions are
    patterns, and others are header names.

    Args:
        entry: Entry: `*`, a header name or a pattern (regular expression).
        header: Header name.

    Returns:
        Whether the entry is `*`, the header (ignoring case), or a pattern
        matching it (ignoring case; an invalid pattern matches nothing).
    """
    if entry == "*" or entry.lower() == header.lower():
        return True
    if not set(entry) & _REGEX_CHARS:
        return False
    try:
        return re.match(entry, header, re.IGNORECASE) is not None
    except re.error:
        return False


def _check_admin_role(
    access_control_config: AccessControlConfig,
    auth_config: AuthConfig | None = None,
) -> None:
    """Check that groups, service clients or configured rules grant `admin`.

    The groups and service clients mapped to the role are logged. Expects
    group settings that keep all group mappings from applying to be rejected
    already (cf. :py:func:`_check_group_config`). Configured rules for groups
    (`group:<value>`) count only if a group claim is set (`claim_groups`) and
    its values are also used as group subjects, as they don't apply
    otherwise. Only direct grants
    are checked: rules granting the role to roles or service clients count,
    whether or not any request can have them. Roles granted via
    stored policy rules (e.g., via the permissions API) are not checked, as
    the database may not be available yet, and still apply.

    Args:
        access_control_config: Access control configuration.
        auth_config: Authentication configuration, for group claims of
            issuers (`auth.issuers[].claim_groups`).

    Raises:
        ValueError: No group (`group_mapping`) or service client
            (`client_mapping`) is mapped to the role, no rule of `policies`
            grants it (e.g., `["g", "user:alice", "admin"]`), and
            `require_admin` is set.
    """
    conf = access_control_config
    keeps_group_subjects = any(_get_group_claims(conf, auth_config)) and (
        conf.group_mapping is None or conf.keep_group_subjects
    )
    found = False
    for name, mapping in (
        ("Groups", conf.group_mapping),
        ("Service clients", conf.client_mapping),
    ):
        mapped = sorted(
            key for key, role in (mapping or {}).items() if role == ADMIN_ROLE
        )
        if mapped:
            found = True
            logger.info(
                "%s mapped to the role '%s': %s.",
                name,
                ADMIN_ROLE,
                ", ".join(escape_log_value(key) for key in mapped),
            )
    # Direct global role rules (`g, <subject>, admin`), also for roles,
    # without checking that any request can have the subject
    found = found or any(
        len(rule) == 3
        and rule[0] == "g"
        and rule[2] == ADMIN_ROLE
        and (keeps_group_subjects or not rule[1].startswith(GROUP_PREFIX))
        for rule in conf.policies or []
    )
    if found:
        return
    missing = (
        f"No group or service client is mapped to the role '{ADMIN_ROLE}' "
        "('security.access_control.group_mapping', with "
        "'security.access_control.claim_groups' or "
        "'security.auth.issuers[].claim_groups' set, or "
        "'security.access_control.client_mapping') and no configured policy "
        "rule that applies grants it ('security.access_control.policies')"
    )
    if conf.require_admin:
        raise ValueError(
            f"{missing}, but 'security.access_control.require_admin' is set: "
            f"map an admin group of the identity provider to '{ADMIN_ROLE}', "
            "add a policy rule granting it, or unset 'require_admin' (roles "
            "granted via stored policy rules then still apply)."
        )
    logger.warning(
        "%s, so nobody is an admin via mappings or configured rules; roles "
        "granted via stored policy rules (e.g., via the permissions API) "
        "still apply.",
        missing,
    )


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
            policy collection's database has a collection of the same name,
            or of the name of the collection recording changes of rules
            (`<collection_name>_changes`).
    """
    effective_db_name = get_mongo_db_name(db_name)
    for app_db_name, app_db_conf in (mongo_config.dbs or {}).items():
        same_db = get_mongo_db_name(app_db_name) == effective_db_name
        if not same_db:
            continue
        # The adapter records changes of rules in a collection next to them
        for name in (collection_name, f"{collection_name}{CHANGES_COLLECTION_SUFFIX}"):
            if name in (app_db_conf.collections or {}):
                raise ValueError(
                    f"Access control collection '{name}' clashes with app "
                    "collection of the same name in database "
                    f"'{effective_db_name}'. Set a different "
                    "'security.access_control.collection_name' or "
                    "'security.access_control.db_name'."
                )


def register_permission_specs(
    app: App, access_control_config: AccessControlConfig
) -> App:
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
    specification: Path | dict[str, Any]
    if access_control_config.api_specs is None:
        spec_path = str(files(ACCESS_CONTROL_BASE_PATH).joinpath(DEFAULT_API_SPEC_PATH))
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
        # the Swagger UI needs the specification
        serve_spec=(
            access_control_config.api_serve_spec or access_control_config.api_swagger_ui
        ),
        resolver=AccessControlResolver(
            enforce_all=True,
            self_service=access_control_config.self_service,
        ),
    )
    app.add_api(
        specification=specification,
        **_connexion_add_api_kwargs(options),
    )
    logger.info("Permission management API registered from '%s'.", spec_path)
    return app


# Fields of OpenAPI 3.x Path Item Objects holding Operation Objects
_HTTP_METHODS = frozenset(
    ("get", "put", "post", "delete", "options", "head", "patch", "trace")
)


def _with_error_media_type(spec_path: Path, media_type: str) -> dict[str, Any]:
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

    The stored configured policy rules are replaced with those of
    `access_control.policies`. If it is not set, stored configured rules
    (e.g., set via a config overlay that was removed since) are removed,
    with a warning.

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
        named = access_control_config.named_policies
        _add_policies(
            adapter=adapter,
            policies=access_control_config.policies,
            names=None if named is None else list(named),
        )
    else:
        _remove_policies(adapter=adapter)
    logger.info("Casbin enforcer registered.")

    return app


def _remove_policies(adapter: Adapter) -> None:
    """Remove stored configured policy rules, if any.

    Apps refuse to start if stored configured rules cannot be checked for or
    removed, e.g., without the database, so that they never start with rules
    removed from their configuration in effect, as with `policies` set. The
    check gives up after `CONFIGURED_POLICIES_CHECK_TIMEOUT` seconds, so that
    startup fails soon (e.g., before the worker timeout of gunicorn) and is
    retried (e.g., by Kubernetes) until the database is available.

    Args:
        adapter: Casbin adapter of the app.

    Raises:
        pymongo.errors.PyMongoError: Checking for or removing stored
            configured rules failed, e.g., as the database is not available.
    """
    try:
        # Shorter than the client's server selection timeout (30 s by
        # default)
        with pymongo.timeout(CONFIGURED_POLICIES_CHECK_TIMEOUT):
            configured = adapter.has_configured_policies()
        if configured:
            # E.g., set via a config overlay that was removed since
            logger.warning(
                "Removing stored configured policy rules, as "
                "'security.access_control.policies' is not set."
            )
            adapter.set_configured_policies([])
    except PyMongoError as exc:
        logger.error(
            "Could not check for or remove stored configured policy rules, "
            "which are removed on startup as "
            "'security.access_control.policies' is not set; refusing to start "
            "(is the database available, and may the app access it?): %s",
            exc,
        )
        raise


def _add_policies(
    adapter: Adapter,
    policies: list[list[str]],
    names: list[str] | None = None,
) -> None:
    """Replace the stored configured policy rules with the configured ones.

    All rules are validated before any stored rule is changed, so that
    invalid configurations leave stored rules unchanged.

    Args:
        adapter: Casbin adapter of the app.
        policies: Policy rules, each given as its policy type followed by its
            values.
        names: Names of the rules, if given as named rules; used in errors
            and logged.

    Raises:
        ValueError: A rule is invalid for the adapter's model (cf.
            :py:func:`validate_rule`).
    """
    for index, rule in enumerate(policies):
        try:
            validate_rule(rule[0] if rule else None, rule[1:], adapter.model)
        except ValueError as exc:
            if names is None:
                raise
            raise ValueError(f"Invalid policy rule '{names[index]}': {exc}") from exc
    adapter.set_configured_policies(policies)
    if names is not None:
        logger.info("Named policy rules applied: %s", ", ".join(names) or "none")
