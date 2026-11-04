"""Methods to manage permission management configuration."""

import logging
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

import pymongo
import yaml
from casbin.util import key_match2
from connexion import App
from connexion.resolver import Resolver
from pymongo.errors import PyMongoError

from foca.api.register_openapi import (
    _connexion_add_api_kwargs,
    _get_error_media_type,
    _iter_operations,
    _iter_resolved_operations,
    _rejects_invalid_tokens,
    _set_error_media_type,
)
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
    ConnexionOptions,
    CORSConfig,
    DBConfig,
    MongoConfig,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    ADMIN_ROLE,
    BASE_PATH_PLACEHOLDER,
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
from foca.security.auth import InvalidTokenResolver
from foca.utils.logging import escape_log_value
from foca.utils.urls import _get_api_base_paths, _get_apis, _register_api

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
    # empty one; that is a problem only if rules rely on group subjects
    if conf.group_mapping is not None and not conf.keep_group_subjects:
        logger.log(
            logging.WARNING if _has_group_subject(conf) else logging.INFO,
            "'security.access_control.group_mapping' is set, but "
            "'security.access_control.keep_group_subjects' is not, so group "
            "claim values are not used as group subjects: policy rules and "
            "roles on resources granted to groups ('group:<value>') don't "
            "apply, and the resource ownership helpers refuse to grant them.",
        )


def _has_group_subject(access_control_config: AccessControlConfig) -> bool:
    """Check whether a configured policy rule has a group as its subject.

    Args:
        access_control_config: Access control configuration.

    Returns:
        Whether the subject of a rule of `policies`, which follows the policy
        type (empty rules fail validation later), has the group prefix.
    """
    return any(
        subject.startswith(GROUP_PREFIX)
        for rule in access_control_config.policies or []
        for subject in rule[1:2]
    )


def _check_stored_group_subjects(
    adapter: Adapter, access_control_config: AccessControlConfig
) -> None:
    """Warn of stored rules for groups that group settings keep from applying.

    Covers rules that are not configured, e.g., written via the permissions
    API or stored before `group_mapping` was set; configured ones are checked
    by :py:func:`_check_group_config`, which warns once of both. Only gives a
    hint, so startup continues if the check fails.

    Args:
        adapter: Casbin adapter of the app.
        access_control_config: Access control configuration.
    """
    conf = access_control_config
    if (
        conf.group_mapping is None
        or conf.keep_group_subjects
        or _has_group_subject(conf)
    ):
        return
    try:
        with pymongo.timeout(CONFIGURED_POLICIES_CHECK_TIMEOUT):
            stored = adapter.has_stored_group_subjects()
    except PyMongoError as exc:
        logger.warning(
            "Could not check for stored policy rules with group subjects: %s", exc
        )
        return
    if stored:
        logger.warning(
            "Stored policy rules have groups as their subjects "
            "('group:<value>'), but 'security.access_control.group_mapping' "
            "is set and 'security.access_control.keep_group_subjects' is not, "
            "so they don't apply to users via their group claims: set "
            "'keep_group_subjects: true' if they should."
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
        media_type = _get_error_media_type(app)
        # as a dictionary only if needed, keeping Connexion's handling of
        # specification files otherwise
        if media_type != DEFAULT_ERROR_MEDIA_TYPE:
            specification = _with_error_media_type(
                spec_path=specification, media_type=media_type
            )
    else:
        spec_path = access_control_config.api_specs
        specification = Path(spec_path)

    resolver: Resolver = AccessControlResolver(
        enforce_all=True,
        self_service=access_control_config.self_service,
    )
    # As for the app's APIs; custom specifications may have optional tokens
    if _rejects_invalid_tokens(app):
        resolver = InvalidTokenResolver(resolver=resolver)
    options = ConnexionOptions(
        strict_validation=True,
        validate_responses=True,
        swagger_ui=access_control_config.api_swagger_ui,
        # the Swagger UI needs the specification
        serve_spec=(
            access_control_config.api_serve_spec or access_control_config.api_swagger_ui
        ),
        resolver=resolver,
    )
    api = app.add_api(
        specification=specification,
        **_connexion_add_api_kwargs(options),
    )
    _register_api(
        app.app,
        api.base_path,
        _iter_resolved_operations(api.specification.raw),
        is_app_api=False,
        paths=list(api.specification.raw.get("paths", {})),
    )
    logger.info("Permission management API registered from '%s'.", spec_path)
    return app


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
    for operation in _iter_operations(spec):
        _set_error_media_type(operation, media_type)
    return spec


def register_casbin_enforcer(
    app: App,
    access_control_config: AccessControlConfig,
    mongo_config: MongoConfig,
) -> App:
    """Method to add casbin permission enforcer.

    The stored configured policy rules are replaced with those of
    `access_control.policies`, with `{base_path}` replaced by the base path of
    each API of the app's specifications. If it is not set, stored configured rules
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
            revision=access_control_config.policies_revision,
            force=access_control_config.policies_force,
            names=None if named is None else list(named),
            base_paths=_get_api_base_paths(app.app),
            protected_paths=[
                f"{api.base_path}{path}"
                for api in _get_apis(app.app)
                if not api.is_app_api
                for path in api.paths
            ],
        )
    else:
        _remove_policies(
            adapter=adapter,
            revision=access_control_config.policies_revision,
            force=access_control_config.policies_force,
        )
    _check_stored_group_subjects(adapter, access_control_config)
    logger.info("Casbin enforcer registered.")

    return app


def _remove_policies(
    adapter: Adapter, revision: int | None = None, force: bool = False
) -> None:
    """Remove stored configured policy rules, if any.

    Apps refuse to start if stored configured rules cannot be checked for or
    removed, e.g., without the database, so that they never start with rules
    removed from their configuration in effect, as with `policies` set. The
    check gives up after `CONFIGURED_POLICIES_CHECK_TIMEOUT` seconds, so that
    startup fails soon (e.g., before the worker timeout of gunicorn) and is
    retried (e.g., by Kubernetes) until the database is available.

    Args:
        adapter: Casbin adapter of the app.
        revision: Revision of the configuration, if any (cf.
            :py:meth:`Adapter.set_configured_policies`).
        force: Remove the rules also if their revision is newer.

    Raises:
        pymongo.errors.PyMongoError: Checking for or removing stored
            configured rules failed, e.g., as the database is not available.
    """
    try:
        # Shorter than the client's server selection timeout (30 s by
        # default)
        with pymongo.timeout(CONFIGURED_POLICIES_CHECK_TIMEOUT):
            configured = adapter.has_configured_policies()
        # E.g., set via a config overlay that was removed since; if forced, also
        # without rules, for the revision
        applied = (configured or force) and adapter.set_configured_policies(
            [], revision, force
        )
        if configured and applied:
            logger.warning(
                "Removed stored configured policy rules, as "
                "'security.access_control.policies' is not set."
            )
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
    revision: int | None = None,
    force: bool = False,
    names: list[str] | None = None,
    base_paths: list[str] | None = None,
    protected_paths: list[str] | None = None,
) -> None:
    """Replace the stored configured policy rules with the configured ones.

    All rules are validated before any stored rule is changed, so that
    invalid configurations leave stored rules unchanged.

    Args:
        adapter: Casbin adapter of the app.
        policies: Policy rules, each given as its policy type followed by its
            values.
        revision: Revision of the configuration, if any (cf.
            :py:meth:`Adapter.set_configured_policies`).
        force: Replace the rules also if their revision is newer.
        names: Names of the rules, if given as named rules; used in errors
            and logged.
        base_paths: Base paths of the app's APIs, for rules with the
            placeholder `{base_path}` (cf. :py:func:`_expand_base_path`).
        protected_paths: Paths that rules with the placeholder must not match
            (cf. :py:func:`_expand_base_path`).

    Raises:
        ValueError: A rule is invalid for the adapter's model (cf.
            :py:func:`validate_rule`), or has the placeholder `{base_path}`
            and the app has no API, or it matches a protected path.
    """
    expanded: list[list[str]] = []
    for index, rule in enumerate(policies):
        try:
            rules = _expand_base_path(rule, base_paths or [], protected_paths or [])
            for _rule in rules:
                validate_rule(_rule[0] if _rule else None, _rule[1:], adapter.model)
        except ValueError as exc:
            if names is None:
                raise
            raise ValueError(f"Invalid policy rule '{names[index]}': {exc}") from exc
        expanded.extend(rules)
    applied = adapter.set_configured_policies(expanded, revision, force)
    if applied and names is not None:
        logger.info("Named policy rules applied: %s", ", ".join(names) or "none")


_PATH_PARAMETER = re.compile(r"\{([^{}]+)\}")


def _expand_base_path(
    rule: list[str], base_paths: list[str], protected_paths: list[str]
) -> list[list[str]]:
    """Replace the placeholder `{base_path}` in the values of a policy rule.

    An API at the root has an empty base path, so a value such as
    `{base_path}/*` then matches all paths of the app. Rules with the
    placeholder are meant for the app's APIs, so they must not match the
    permissions API that way: moving an API to the root must not silently
    open up who may manage policies. The check uses `keyMatch2`, as the default
    model does; custom models that match paths differently aren't covered.

    Args:
        rule: Policy rule: its policy type, followed by its values.
        base_paths: Base paths of the app's APIs.
        protected_paths: Paths that the rule must not match, i.e., those of
            the permissions API, as in its specification (e.g.,
            `/permissions/{id}`).

    Returns:
        The rule if it has no placeholder; otherwise, a rule for each base
        path. A value that is only the placeholder is the path `/` for an API
        at the root.

    Raises:
        ValueError: The rule has the placeholder, but the app has no API, or
            it matches one of `protected_paths`.
    """
    if not any(BASE_PATH_PLACEHOLDER in value for value in rule[1:]):
        return [rule]
    if not base_paths:
        raise ValueError(
            f"Policy rule {rule!r} has the placeholder "
            f"'{BASE_PATH_PLACEHOLDER}', but the app has no API (`api.specs`)"
        )
    expanded = [
        [
            rule[0],
            *(value.replace(BASE_PATH_PLACEHOLDER, base) or "/" for value in rule[1:]),
        ]
        for base in base_paths
    ]
    for path in protected_paths:
        # Any value of a path parameter: the rule may cover all of them (the
        # sample), or one of them (the pattern)
        sample = _PATH_PARAMETER.sub("id", path)
        pattern = _PATH_PARAMETER.sub(r":\1", path)
        for base in base_paths:
            for value in rule[1:]:
                if BASE_PATH_PLACEHOLDER not in value:
                    continue
                expanded_value = value.replace(BASE_PATH_PLACEHOLDER, base)
                if key_match2(sample, expanded_value) or key_match2(
                    expanded_value, pattern
                ):
                    raise ValueError(
                        f"Policy rule {rule!r} has the placeholder "
                        f"'{BASE_PATH_PLACEHOLDER}', but with the base paths of "
                        f"the app's APIs, it also matches the permissions API "
                        f"('{path}'): limit it to the paths it is meant for"
                    )
    return expanded
