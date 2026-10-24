"""Enforcement of access control policies and helpers for managing them."""

import inspect
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, Literal

from casbin import Enforcer
from casbin.model import Model
from connexion.exceptions import BadRequestProblem, Forbidden
from connexion.operations import AbstractOperation
from connexion.resolver import Resolution, Resolver
from flask import current_app, has_request_context, request
from flask.globals import request_ctx

from foca.config.access import get_config
from foca.security.access_control.constants import (
    CLIENT_PREFIX,
    GROUP_PREFIX,
    SUBJECT_TYPE_PREFIXES,
)
from foca.security.access_control.enforcer_cache import (
    EnforcerCache,
    ResourceRoleManager,
    get_resource_role_ptypes,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    validate_rule,
    validate_rule_value,
)
from foca.utils.logging import escape_log_value

if TYPE_CHECKING:  # import cycle
    from foca.models.config import AccessControlConfig, ServiceClientConfig

logger = logging.getLogger(__name__)

# Policy type of resource-scoped role rules: subject, role, resource
RESOURCE_ROLE_PTYPE = "g2"

# Key of the app config marking that loaded resource-scoped roles were
# checked for roles that are subjects
_SUBJECT_ROLES_CHECKED_KEY = "FOCA_ACCESS_CONTROL_SUBJECT_ROLES_CHECKED"
_subject_roles_check_lock = threading.Lock()

# Key of the app's extensions holding the app's enforcer cache
_ENFORCER_CACHE_KEY = "foca_enforcer_cache"
_enforcer_cache_lock = threading.Lock()

# Attribute marking the wrappers of `enforce_access_control()`
_ENFORCING_ATTRIBUTE = "_foca_enforces_access_control"

SubjectType = Literal["user", "group", "client"]
"""Types of subjects of resource role helpers: users, groups or service clients."""


def _get_access_control_config() -> "AccessControlConfig":
    """Get access control configuration of current app."""
    return get_config().security.access_control


def _get_claims() -> dict[str, Any]:
    """Get claims of the validated token of the current request.

    Returns:
        Claims of the token validated by Connexion's security handler for the
        current request, or an empty dictionary if not available, i.e., if
        there is no request, the request was not handled by Connexion, or no
        token was validated.
    """
    if not has_request_context():
        return {}
    # Connexion stores the request's context (including validated token
    # info) on Flask's request context (cf. `connexion.context`)
    connexion_context = getattr(request_ctx, "connexion_context", None)
    if connexion_context is None:
        return {}
    token_info = connexion_context.get("token_info")
    if not isinstance(token_info, dict):
        return {}
    # FOCA's `validate_token()` returns claims under key `claims`
    claims = token_info.get("claims", token_info)
    return claims if isinstance(claims, dict) else {}


def get_current_subject() -> str | None:
    """Get subject of the current request.

    Returns:
        Value of the claim configured via `access_control.claim_identity`
        (default: `auth.claim_identity`) of the validated token of the
        current request, or `None` if not available. The value is returned
        as is, i.e., without `access_control.subject_prefix`.
    """
    claim = _get_access_control_config().claim_identity
    if claim is None:
        claim = get_config().security.auth.claim_identity
    subject = _get_claims().get(claim)
    return None if subject is None else str(subject)


def _get_subject_prefix(subject_type: SubjectType) -> str:
    """Get prefix of subjects of a type, as used in policy rules.

    Args:
        subject_type: Type of subjects: `user` for user identifiers (prefixed
            with `access_control.subject_prefix`), `group` for group claim
            values (prefixed with `group:`) or `client` for client IDs of
            service clients (prefixed with `client:`).

    Returns:
        Prefix of subjects of the type.

    Raises:
        ValueError: The subject type is not supported.
    """
    if subject_type == "user":
        return _get_access_control_config().subject_prefix
    if subject_type == "group":
        return GROUP_PREFIX
    if subject_type == "client":
        return CLIENT_PREFIX
    raise ValueError(
        f"Unsupported subject type '{subject_type}': expected 'user', 'group' "
        "or 'client'"
    )


def _prefix_subject(subject: str, subject_type: SubjectType = "user") -> str:
    """Add prefix of a subject type to a subject, as used in policy rules."""
    return f"{_get_subject_prefix(subject_type)}{subject}"


def _get_current_service_client() -> "ServiceClientConfig | None":
    """Get the service client of the current request.

    Returns:
        Service client (cf. `auth.service_clients`) if the validated token of
        the current request is a service token (cf.
        :py:func:`foca.security.auth.get_service_client`), otherwise `None`.
    """
    from foca.security.auth import get_service_client  # noqa: PLC0415 (import cycle)

    claims = _get_claims()
    if not claims:
        return None
    return get_service_client(claims=claims, conf=get_config().security.auth)


def get_current_client() -> str | None:
    """Get the service client of the current request.

    Use this, e.g., to make a service client the owner of a resource it
    creates (with `subject_type="client"`, cf. :py:func:`add_resource_role`),
    unless it acts for a user (cf. :py:func:`get_on_behalf_of_user`).

    Returns:
        Client ID of the service client (cf. `auth.service_clients`) if the
        validated token of the current request is a service token, otherwise
        `None` (e.g., for user tokens, also of service clients).
    """
    client = _get_current_service_client()
    return None if client is None else client.client_id


def get_on_behalf_of_user(user: str | None = None) -> str | None:
    """Get the user that the service client of the current request acts for.

    Service clients allowed to (cf. `auth.service_clients[].on_behalf_of`)
    may name the user they act for, in the header configured via
    `auth.on_behalf_of_header` or in a value that the app passes (e.g.,
    from a request field), so that the app can, e.g., make that user the
    owner of a resource the client creates. Other callers must not name
    users; their requests are rejected rather than the named user ignored,
    so that clients notice. FOCA does not check whether the named user
    exists or consented; the service client is trusted to name the user
    whose request it handles.

    Args:
        user: User identifier named by the caller (e.g., from a request
            field). If `None`, the value of the header configured via
            `auth.on_behalf_of_header`, if any, is used.

    Returns:
        Named user identifier, as is (i.e., without
        `access_control.subject_prefix`), or `None` if the caller names no
        user.

    Raises:
        connexion.exceptions.BadRequestProblem: The named user is not a
            string, is empty or blank, or has surrounding whitespace.
        connexion.exceptions.Forbidden: The caller names a user, but is not a
            service client allowed to act for users.
    """
    if user is None:
        header = get_config().security.auth.on_behalf_of_header
        if header is None or not has_request_context():
            return None
        user = request.headers.get(header)
        if user is None:
            return None
    client = _get_current_service_client()
    if client is None or not client.on_behalf_of:
        logger.info(
            "Request naming user '%s' rejected: not a service client allowed "
            "to act for users",
            escape_log_value(user),
        )
        raise Forbidden(description="Only allowed service clients may act for users")
    # Values passed by the app may come from request fields of any type
    if not isinstance(user, str) or not user.strip():
        raise BadRequestProblem(detail="The user acted for must not be empty")
    if user != user.strip():
        raise BadRequestProblem(
            detail="The user acted for must not have surrounding whitespace"
        )
    logger.debug(
        "Service client '%s' acts for user '%s'",
        escape_log_value(client.client_id),
        escape_log_value(user),
    )
    return user


def _get_current_policy_subjects() -> list[str]:
    """Get the subjects of the current request as used in policy rules.

    Returns:
        For service tokens (cf. :py:func:`get_current_client`), the client
        ID prefixed with `client:` and the role of the client
        (`access_control.client_mapping`), if mapped; for other tokens, the
        subject (cf. :py:func:`get_current_subject`) prefixed with
        `access_control.subject_prefix`. Followed by the roles from group
        claims (cf. :py:func:`get_current_groups`), without duplicates.
    """
    client = _get_current_service_client()
    if client is None:
        subjects = [_prefix_subject(str(get_current_subject()))]
    else:
        subjects = [_prefix_subject(client.client_id, "client")]
        mapping = _get_access_control_config().client_mapping or {}
        if client.client_id in mapping:
            subjects.append(mapping[client.client_id])
    return list(dict.fromkeys([*subjects, *get_current_groups()]))


def get_current_group_values() -> list[str]:
    """Get values of the group claim of the current request.

    Use this, e.g., to only let users grant roles to groups they are members
    of (cf. :py:func:`add_resource_role`).

    Returns:
        Values of the claim configured via `access_control.claim_groups` of
        the validated token of the current request, as is, i.e., neither
        mapped nor prefixed, and without duplicates. Empty if the claim is
        not configured or not available, or if its value is neither a
        string nor a list.
    """
    conf = _get_access_control_config()
    if conf.claim_groups is None:
        return []
    values = _get_claims().get(conf.claim_groups, [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(str(value) for value in values))


def get_current_groups() -> list[str]:
    """Get roles of the subject of the current request from group claims.

    Returns:
        Values of the claim configured via `access_control.claim_groups` of
        the validated token of the current request (cf.
        :py:func:`get_current_group_values`), mapped to roles via
        `access_control.group_mapping`, if set. Values without a mapping are
        dropped if a mapping is set. Without a mapping, values are prefixed
        with `group:` (e.g., `group:vo-members`), so that they cannot
        collide with role names. If a mapping and
        `access_control.keep_group_subjects` are set, all values are
        additionally returned with that prefix, after the mapped roles.
    """
    conf = _get_access_control_config()
    values = get_current_group_values()
    groups = []
    if conf.group_mapping is not None:
        groups = [
            conf.group_mapping[value] for value in values if value in conf.group_mapping
        ]
    if conf.group_mapping is None or conf.keep_group_subjects:
        groups.extend(_prefix_subject(value, "group") for value in values)
    return list(dict.fromkeys(groups))


def _warn_subject_like_roles(enforcer: Enforcer) -> None:
    """Warn about loaded resource-scoped roles that are subjects.

    Granting a subject a resource-scoped role that is itself a subject
    (e.g., `g2, user:alice, group:auditors, /services/1`) gives it the
    rights of that subject on the resource, as Casbin resolves roles
    transitively. Such rules are rejected by :py:func:`add_resource_role`,
    but may have been stored before, or directly. They are not removed
    automatically. Checked on the first policy load of each app instance
    (per worker process), on the policy rules already loaded by
    `enforcer`, i.e., without accessing the database.

    Args:
        enforcer: Casbin enforcer with loaded policy rules.
    """
    # Locked, so that concurrent first requests do not warn twice
    with _subject_roles_check_lock:
        checked = current_app.config.get(_SUBJECT_ROLES_CHECKED_KEY, False)
        current_app.config[_SUBJECT_ROLES_CHECKED_KEY] = True
    if checked:
        return
    prefixes = (_get_subject_prefix("user"), *SUBJECT_TYPE_PREFIXES.values())
    for ptype in get_resource_role_ptypes(enforcer.get_model()):
        for subject, role, resource in enforcer.get_named_grouping_policy(ptype):
            if role.startswith(prefixes):
                # Values are quoted, so that they cannot forge log lines
                logger.warning(
                    "Policy rule (%s, %r, %r, %r) grants a role that is a "
                    "subject, giving %r the rights of %r on %r. Revoke it, if "
                    "not intended, via `remove_resource_role()` or the "
                    "permissions API.",
                    ptype,
                    subject,
                    role,
                    resource,
                    subject,
                    role,
                    resource,
                )


def _get_enforcer_cache() -> EnforcerCache:
    """Get the enforcer cache of the current app instance.

    Returns:
        Cache of the app's enforcer; created on first use, and again if the
        app's model, adapter or `access_control.resource_roles_cover_subpaths`
        changed since.
    """
    model = current_app.config["CASBIN_MODEL"]
    adapter = _get_adapter()
    cover_subpaths = _get_access_control_config().resource_roles_cover_subpaths
    with _enforcer_cache_lock:
        cache = current_app.extensions.get(_ENFORCER_CACHE_KEY)
        if not isinstance(cache, EnforcerCache) or not cache.is_for(
            model, adapter, cover_subpaths
        ):
            cache = EnforcerCache(model, adapter, cover_subpaths)
            current_app.extensions[_ENFORCER_CACHE_KEY] = cache
    return cache


@contextmanager
def locked_enforcer() -> Iterator[Enforcer]:
    """Use the Casbin enforcer of the app, with current policies.

    The enforcer is shared by the threads of the app instance (e.g., of a
    gunicorn worker) and updated when stored policy rules change, so it is
    used while holding a lock: other threads wait for the lock to use the
    enforcer meanwhile. Use it briefly, e.g., without waiting for other
    services. Do not store it for later use, and do not change it
    directly; change policy rules via the adapter (e.g., via FOCA's
    helpers), which records the changes for all app instances.

    Before the enforcer is used, the changes of stored rules made since the
    last use (e.g., via FOCA's helpers, the permissions API, or another app
    instance) are applied, so that a rule granted or revoked applies from
    the next use in any app instance. Rules changed in the database
    directly are only noticed after
    :py:meth:`foca.security.access_control.foca_casbin_adapter.adapter.Adapter.mark_changed`.

    If `access_control.resource_roles_cover_subpaths` is set, roles of
    resource-scoped role definitions (with three fields, e.g.,
    `g2 = _, _, _`) also apply to sub-paths of the resource path they were
    granted on. When policies are first loaded, a warning is logged for
    each stored resource-scoped role that is a subject (cf.
    :py:func:`add_resource_role`).

    Yields:
        Casbin enforcer for the app's model with the policies stored in the
        app's access control database.

    Raises:
        pymongo.errors.PyMongoError: The stored rules could not be read.
    """
    cache = _get_enforcer_cache()
    with cache.lock:
        enforcer = cache.get_enforcer()
        _warn_subject_like_roles(enforcer)
        yield enforcer


def enforce_access_control(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator enforcing access control policies on a controller.

    The subject (cf. :py:func:`get_current_subject`), prefixed with
    `access_control.subject_prefix`, and any roles derived from its group
    claims (cf. :py:func:`get_current_groups`) are checked
    against the request path and HTTP method. Access is granted if any of
    them is allowed.

    Args:
        fn: The function to be decorated.

    Returns:
        The decorated function.

    Raises:
        connexion.exceptions.Unauthorized: The subject could not be
            identified.
        connexion.exceptions.Forbidden: The subject is not allowed to access
            the resource, or it is not a valid value for policy rules (cf.
            :py:func:`validate_rule_value`).
    """

    @wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        if not is_permitted():
            raise Forbidden
        return fn(*args, **kwargs)

    setattr(_wrapper, _ENFORCING_ATTRIBUTE, True)
    return _wrapper


def _enforces_access_control(fn: Callable[..., Any]) -> bool:
    """Check whether a function enforces access control policies.

    Args:
        fn: Function, e.g., the view function of a route.

    Returns:
        Whether the function is a wrapper of :py:func:`enforce_access_control`,
        or wraps one (via `functools.wraps()`, e.g., Connexion's decorators).
    """

    def _is_enforcing(function: Callable[..., Any]) -> bool:
        return getattr(function, _ENFORCING_ATTRIBUTE, False) is True

    return _is_enforcing(inspect.unwrap(fn, stop=_is_enforcing))


def is_permitted() -> bool:
    """Check whether the current request is allowed by the policies.

    The subject (cf. :py:func:`get_current_subject`), prefixed with
    `access_control.subject_prefix`, and any roles derived from its group
    claims (cf. :py:func:`get_current_groups`) are checked against the
    request path and HTTP method. For service tokens (cf.
    :py:func:`get_current_client`), the client ID, prefixed with `client:`,
    and the role of the client (`access_control.client_mapping`), if mapped,
    are checked instead of the prefixed subject.

    Returns:
        `True` if the subject or any of its roles is allowed to access the
        resource, `False` if not, or if the subject is not a valid value for
        policy rules (cf. :py:func:`validate_rule_value`).

    Raises:
        connexion.exceptions.Unauthorized: The subject of a token that is not
            a service token could not be identified.
    """
    client = _get_current_service_client()
    # Service tokens are identified by their client, also if they lack the
    # identity claim of access control (e.g., a claim only users have)
    subject = get_current_subject() if client is None else client.client_id
    if subject is None:
        from foca.security.auth import _unauthorized  # noqa: PLC0415 (import cycle)

        # No `invalid_token` error code for validated tokens lacking the
        # identity claim, as clients cannot get tokens with the claim by
        # logging in again
        raise _unauthorized("Subject could not be identified", invalid_token=False)
    subjects = _get_current_policy_subjects()
    try:
        validate_rule_value(subjects[0])
    except ValueError as exc:
        logger.info("Access denied: invalid subject: %s", exc)
        return False
    with locked_enforcer() as enforcer:
        granted = next(
            (
                _subject
                for _subject in subjects
                if enforcer.enforce(_subject, request.path, request.method)
            ),
            None,
        )
    # Escaped, so that request paths and claims cannot forge log lines
    _request = escape_log_value(f"{request.method} {request.path}")
    if granted is not None:
        logger.debug(
            "Access granted: %s to '%s' as '%s'",
            _request,
            escape_log_value(subject),
            escape_log_value(granted),
        )
        return True
    logger.info("Access denied: %s to '%s'", _request, escape_log_value(subject))
    return False


def _requires_authentication(security: list[dict[str, Any]] | None) -> bool:
    """Check whether security requirements require authentication.

    Args:
        security: Effective security requirements of an operation.

    Returns:
        `True` if there are security requirements and none of the
        alternatives is empty; an empty alternative (e.g., `[{}]`) allows
        anonymous access.
    """
    if not security:
        return False
    return all(security)


class AccessControlResolver(Resolver):
    """Connexion resolver enforcing access control policies.

    Resolved functions are wrapped with :py:func:`enforce_access_control`.
    As Connexion's security handlers wrap the resolved functions, policies are
    enforced after token validation. Whether to enforce policies is decided
    for each operation as resolved by Connexion, i.e., with references
    resolved and security requirements inherited from the specification.

    Args:
        resolver: Resolver to resolve functions with (any object with a
            `resolve()` method), or a function resolving operation
            identifiers to functions, as accepted by Connexion. Defaults to
            Connexion's default resolver.
        enforce_all: Whether to enforce policies on all operations. If
            `False`, policies are only enforced on operations that require
            authentication and do not set `x-foca-access-control: false`.
        self_service: Whether to leave enforcement to the controllers of
            operations that set `x-foca-self-service: true`, even if
            `enforce_all` is set. Such controllers must restrict access
            themselves (e.g., via :py:func:`is_permitted`).

    Attributes:
        resolver: Resolver to resolve functions with.
        enforce_all: Whether to enforce policies on all operations.
        self_service: Whether to leave enforcement to the controllers of
            operations that set `x-foca-self-service: true`.
    """

    def __init__(
        self,
        resolver: Resolver | Callable[..., Any] | None = None,
        enforce_all: bool = False,
        self_service: bool = False,
    ):
        """Construct resolver."""
        super().__init__()
        # Same normalization as Connexion's `App.add_api()`: wrap callables,
        # use any other resolver object (with a `resolve()` method) as is
        self.resolver: Resolver
        if resolver is None:
            self.resolver = Resolver()
        elif callable(resolver):
            self.resolver = Resolver(function_resolver=resolver)
        else:
            self.resolver = resolver
        self.enforce_all = enforce_all
        self.self_service = self_service

    def _is_enforced(self, operation: AbstractOperation) -> bool:
        """Check whether to enforce policies on an operation.

        Args:
            operation: Connexion operation.

        Returns:
            Whether to enforce policies on the operation.
        """
        # Connexion only exposes vendor extensions via the operation object
        if (
            self.self_service
            and operation._operation.get("x-foca-self-service") is True
        ):
            return False
        if self.enforce_all:
            return True
        if operation._operation.get("x-foca-access-control") is False:
            return False
        return _requires_authentication(operation.security)

    def resolve(self, operation: AbstractOperation) -> Resolution:
        """Resolve operation and wrap function with policy enforcement.

        Args:
            operation: Connexion operation to resolve.

        Returns:
            Resolution of the operation.
        """
        resolution = self.resolver.resolve(operation)
        if not self._is_enforced(operation):
            return resolution
        logger.debug(
            "Enforcing access control: %s %s", operation.method.upper(), operation.path
        )
        return Resolution(
            function=enforce_access_control(resolution.function),
            operation_id=resolution.operation_id,
        )


@contextmanager
def _resource_role_enforcer(ptype: str) -> Iterator[Enforcer]:
    """Use Casbin enforcer and check model for resource-scoped roles.

    Args:
        ptype: Policy type of resource-scoped role rules.

    Yields:
        Casbin enforcer (cf. :py:func:`locked_enforcer`).

    Raises:
        ValueError: The model does not define `ptype` as a role definition
            with three fields (subject, role, resource).
    """
    with locked_enforcer() as enforcer:
        _check_resource_role_model(ptype, enforcer.get_model())
        yield enforcer


def _get_role_rules(
    enforcer: Enforcer, ptype: str, field_index: int, *field_values: str
) -> list[list[str]]:
    """Get the role rules of an enforcer that match a filter.

    Args:
        enforcer: Casbin enforcer (cf. :py:func:`locked_enforcer`).
        ptype: Policy type of role rules.
        field_index: Index of the value that `field_values` start at.
        field_values: Values the rules have, starting at `field_index`.

    Returns:
        Values of the matching rules, as returned by Casbin's
        `get_filtered_named_grouping_policy()`, but looked up via the index
        of the role manager, if it has one for the filter (cf.
        :py:meth:`foca.security.access_control.enforcer_cache.ResourceRoleManager.get_rules`).
    """
    role_manager = enforcer.rm_map.get(ptype)
    if isinstance(role_manager, ResourceRoleManager):
        rules = role_manager.get_rules(field_index, *field_values)
        if rules is not None:
            return rules
    return enforcer.get_filtered_named_grouping_policy(
        ptype, field_index, *field_values
    )


def _check_resource_role_model(ptype: str, model: Model) -> None:
    """Check that a model defines resource-scoped roles.

    Args:
        ptype: Policy type of resource-scoped role rules.
        model: Casbin model.

    Raises:
        ValueError: The model does not define `ptype` as a role definition
            with three fields (subject, role, resource).
    """
    if ptype not in get_resource_role_ptypes(model):
        raise ValueError(
            f"Access control model does not define resource-scoped roles: "
            f"expected role definition '{ptype} = _, _, _'"
        )


def _get_resource_role_rule(
    subject: str,
    role: str,
    resource: str,
    ptype: str,
    subject_type: SubjectType,
    enforcer: Enforcer,
) -> list[str]:
    """Get and validate a resource-scoped role rule.

    Args:
        subject: Subject without prefix.
        role: Role.
        resource: Resource, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.
        subject_type: Type of the subject (cf. :py:func:`add_resource_role`).
        enforcer: Casbin enforcer.

    Returns:
        Values of the rule, with the subject prefixed according to its type.

    Raises:
        ValueError: The subject type is not supported, or the rule is not
            valid (cf.
            :py:func:`foca.security.access_control.foca_casbin_adapter.casbin_rule.validate_rule`).
    """
    rule = [_prefix_subject(subject, subject_type), role, resource]
    validate_rule(ptype, rule, enforcer.get_model())
    return rule


def _validate_grantable_role(role: str) -> None:
    """Ensure that a role to grant is not a subject.

    As Casbin resolves roles transitively, granting a subject a role that is
    a subject (e.g., `group:auditors`) would give it all roles of that
    subject.

    Args:
        role: Role to grant.

    Raises:
        ValueError: The role starts with `access_control.subject_prefix`,
            `group:` or `client:`.
    """
    for prefix in [_get_subject_prefix("user"), *SUBJECT_TYPE_PREFIXES.values()]:
        if role.startswith(prefix):
            raise ValueError(
                f"Roles must not start with '{prefix}', the prefix of subjects"
            )


def add_resource_role(
    subject: str,
    role: str,
    resource: str,
    ptype: str = RESOURCE_ROLE_PTYPE,
    subject_type: SubjectType = "user",
) -> bool:
    """Grant a subject a role on a resource.

    Requires a model with resource-scoped roles, i.e., a role definition
    `<ptype> = _, _, _` (e.g., `g2` in `resource_role_model.conf`).

    Roles granted to a group apply to all subjects whose group claim
    (`access_control.claim_groups`) contains the group, if group claim
    values are used as subjects, i.e., if `access_control.group_mapping` is
    not set, or if `access_control.keep_group_subjects` is set; otherwise,
    a warning is logged, as the role would not apply. Which
    subjects may grant roles to which groups is up to the app (e.g., only
    to groups they are members of, cf.
    :py:func:`get_current_group_values`).

    Args:
        subject: Subject to grant role to, without prefix: a user identifier
            (e.g., as returned by :py:func:`get_current_subject`), a group
            claim value if `subject_type` is `group`, or a client ID of a
            service client if `subject_type` is `client` (e.g., as returned
            by :py:func:`get_current_client`). It is prefixed with
            `access_control.subject_prefix`, `group:` or `client:`,
            respectively, in the rule.
        role: Role to grant (e.g., `owner`).
        resource: Resource to grant role on, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.
        subject_type: Type of the subject: `user`, `group` or `client`.

    Returns:
        `True` if the role was granted, `False` if it already existed.

    Raises:
        ValueError: The model does not define resource-scoped roles, the
            subject is empty, the subject type is not supported, the role
            starts with `access_control.subject_prefix`, `group:` or
            `client:` (so that subjects cannot get the roles of other
            subjects through it), or
            the prefixed subject, role or resource is not a valid value for
            policy rules (cf.
            :py:func:`foca.security.access_control.foca_casbin_adapter.casbin_rule.validate_rule_value`).
    """
    with _resource_role_enforcer(ptype) as enforcer:
        rule = _validate_grantable_rule(
            subject, role, resource, ptype, subject_type, enforcer
        )
        return enforcer.add_named_grouping_policy(ptype, *rule)


def _replace_resource_role(
    subject: str,
    role: str,
    resource: str,
    roles: list[str],
    ptype: str = RESOURCE_ROLE_PTYPE,
    subject_type: SubjectType = "user",
) -> bool:
    """Grant a subject a role on a resource, replacing some of its roles.

    Unlike revoking the other roles and then granting the role, the subject
    keeps a role throughout, and concurrent calls for the same subject and
    resource leave it with one of `roles` (cf.
    :py:meth:`foca.security.access_control.foca_casbin_adapter.adapter.Adapter.replace_role_rule`).
    Roles set via the app's configuration (`access_control.policies`) are
    kept, with a warning.

    Args:
        subject: Subject without prefix (cf. :py:func:`add_resource_role`).
        role: Role to grant.
        resource: Resource, i.e., its full request path.
        roles: Roles of the subject on the resource to replace.
        ptype: Policy type of resource-scoped role rules.
        subject_type: Type of the subject: `user`, `group` or `client`.

    Returns:
        `True` if the role was granted, `False` if it already existed.

    Raises:
        ValueError: As for :py:func:`add_resource_role`.
    """
    with _resource_role_enforcer(ptype) as enforcer:
        rule = _validate_grantable_rule(
            subject, role, resource, ptype, subject_type, enforcer
        )
    adapter = _get_adapter()
    others = set(roles) - {role}
    for configured in adapter.get_filtered_rules(ptype, 2, resource, configured=True):
        if configured[0] == rule[0] and configured[1] in others:
            _warn_configured_rule(ptype, configured)
    return adapter.replace_role_rule(ptype, rule, roles)


def _validate_grantable_rule(
    subject: str,
    role: str,
    resource: str,
    ptype: str,
    subject_type: SubjectType,
    enforcer: Enforcer,
) -> list[str]:
    """Get and validate a resource-scoped role rule to grant.

    Logs a warning if the role would not apply (cf.
    :py:func:`add_resource_role`).

    Args:
        subject: Subject without prefix (cf. :py:func:`add_resource_role`).
        role: Role to grant.
        resource: Resource, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.
        subject_type: Type of the subject.
        enforcer: Casbin enforcer (cf. :py:func:`_get_resource_role_enforcer`).

    Returns:
        Values of the rule, with the subject prefixed according to its type.

    Raises:
        ValueError: As for :py:func:`add_resource_role`.
    """
    # Only checked when granting roles, so that rules with empty subjects
    # (e.g., stored by previous versions) can still be revoked
    if not subject:
        raise ValueError("Subjects must not be empty")
    rule = _get_resource_role_rule(
        subject, role, resource, ptype, subject_type, enforcer
    )
    _validate_grantable_role(role)
    conf = _get_access_control_config()
    if (
        subject_type == "group"
        and conf.group_mapping is not None
        and not conf.keep_group_subjects
    ):
        logger.warning(
            "Role '%s' granted to group "
            "'%s' on "
            "'%s' does not apply: group claim "
            "values are not used as subjects with "
            "'access_control.group_mapping', unless "
            "'access_control.keep_group_subjects' is set",
            escape_log_value(role),
            escape_log_value(subject),
            escape_log_value(resource),
        )
    return rule


def remove_resource_role(
    subject: str,
    role: str,
    resource: str,
    ptype: str = RESOURCE_ROLE_PTYPE,
    subject_type: SubjectType = "user",
) -> bool:
    """Revoke a role of a subject on a resource.

    Args:
        subject: Subject to revoke role from, without prefix: a user
            identifier, a group claim value if `subject_type` is `group`, or a
            client ID if `subject_type` is `client`.
        role: Role to revoke (e.g., `owner`).
        resource: Resource to revoke role on, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.
        subject_type: Type of the subject: `user`, `group` or `client`.

    Returns:
        `True` if the role was revoked, `False` if it did not exist or is
        set via the app's configuration (`access_control.policies`), in
        which case a warning is logged and the configured rule is kept, as
        it would be added again on the next start. All other stored copies
        of the rule are removed (e.g., if it was stored twice by concurrent
        requests, or also added via the permissions API).

    Raises:
        ValueError: The model does not define resource-scoped roles, the
            subject type is not supported, or the prefixed subject, role or
            resource is not a valid value for policy rules. Unlike
            :py:func:`add_resource_role`, empty subjects and roles starting
            with `access_control.subject_prefix`, `group:` or `client:` are
            accepted, so that such rules (e.g., stored before they were
            rejected) can be revoked.
    """
    # Subject and role checks of `add_resource_role()` do not apply here
    with _resource_role_enforcer(ptype) as enforcer:
        rule = _get_resource_role_rule(
            subject, role, resource, ptype, subject_type, enforcer
        )
    # Via the adapter, which records the change for the enforcers of all app
    # instances, also if the rule is not loaded yet (e.g., if stored
    # concurrently). Removes unmarked copies only; the adapter keeps
    # configured rules, also if they were marked after the policies were
    # loaded; the section (first argument) is not used by the adapter.
    adapter = _get_adapter()
    removed = adapter.remove_policy(ptype[0], ptype, rule)
    if adapter.is_configured_rule(ptype, rule):
        _warn_configured_rule(ptype, rule)
        return False
    return removed


def list_resource_roles(
    resource: str,
    ptype: str = RESOURCE_ROLE_PTYPE,
    subject_type: SubjectType | None = None,
) -> list[tuple[str, str]]:
    """List roles granted on a resource.

    Args:
        resource: Resource to list roles of, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.
        subject_type: Type of subjects to list roles of: `user`, `group` or
            `client`. If `None`, roles of all subjects are listed, with user
            identifiers without `access_control.subject_prefix` and all other
            subjects as is (e.g., `group:vo-members`), so that a user whose
            identifier starts with `group:` or `client:` cannot be told apart
            from a group or client; pass the type to tell them apart.

    Returns:
        Subject and role of each role granted on the resource, without
        duplicates. Subjects are returned without the prefix of
        `subject_type`, if given.

    Raises:
        ValueError: The model does not define resource-scoped roles, or the
            subject type is not supported.
    """
    with _resource_role_enforcer(ptype) as enforcer:
        rules = _get_role_rules(enforcer, ptype, 2, resource)
    if subject_type is None:
        prefix = _get_subject_prefix("user")
    else:
        prefix = _get_subject_prefix(subject_type)
        rules = [rule for rule in rules if rule[0].startswith(prefix)]
    # Rules may be stored more than once (e.g., by concurrent requests)
    return list(
        dict.fromkeys((rule[0].removeprefix(prefix), rule[1]) for rule in rules)
    )


def remove_resource(
    resource: str,
    ptype: str = RESOURCE_ROLE_PTYPE,
) -> bool:
    """Revoke all roles granted on a resource, e.g., after deleting it.

    Only roles granted on exactly `resource` are revoked, not roles granted
    on its sub-paths (e.g., `/services/1/files` for `/services/1`) or on
    the path with a trailing slash; revoke these separately. Rules set via
    the app's configuration (`access_control.policies`) are kept, with a
    warning, as they would be added again on the next start. Invalid
    stored rules on the resource (cf.
    :py:func:`foca.security.access_control.foca_casbin_adapter.casbin_rule.validate_rule`)
    are removed, too.

    Args:
        resource: Resource to revoke roles on, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.

    Returns:
        `True` if any roles were revoked, i.e., if this call removed valid
        rules that are not also set via the configuration. Removing a
        stored copy of a configured rule (e.g., one added before it was
        configured) or an invalid rule does not revoke a role, nor does
        removing rules that a concurrent call removed first.

    Raises:
        ValueError: The model does not define resource-scoped roles.
    """
    # Rules are removed via the adapter, without loading them; the adapter
    # records the changes for the enforcers of all app instances
    model = Model()
    model.load_model(current_app.config["CASBIN_MODEL"])
    _check_resource_role_model(ptype, model)
    adapter = _get_adapter()
    configured = adapter.get_filtered_rules(ptype, 2, resource, configured=True)
    for rule in configured:
        _warn_configured_rule(ptype, rule)
    revoked = False
    for rule in adapter.get_filtered_rules(ptype, 2, resource, configured=False):
        # Counted only if this call removed it (not a concurrent one), and
        # if it granted a role: unmarked copies of configured rules still
        # apply, and invalid rules are not loaded. Configured copies are
        # kept by the adapter; the section (first argument) is not used.
        if (
            adapter.remove_policy(ptype[0], ptype, rule)
            and rule not in configured
            and _is_valid_rule(ptype, rule, model)
        ):
            revoked = True
    # Remove rules that could not be removed one by one (e.g., with values
    # that are not strings) or that were stored concurrently; the section
    # (first argument) is not used by the adapter
    adapter.remove_filtered_policy(ptype[0], ptype, 2, resource, keep_configured=True)
    return revoked


def _is_valid_rule(ptype: str, rule: list[str], model: Model) -> bool:
    """Check whether a rule is valid, i.e., loaded when enforcing.

    Args:
        ptype: Policy type of the rule.
        rule: Values of the rule.
        model: Casbin model.

    Returns:
        Whether the rule is valid for `model` (cf. :py:func:`validate_rule`).
    """
    try:
        validate_rule(ptype, rule, model)
    except ValueError:
        return False
    return True


def _get_adapter() -> Adapter:
    """Get Casbin adapter of the current app."""
    return current_app.config["casbin_adapter"]


def _warn_configured_rule(ptype: str, rule: list[str]) -> None:
    """Warn that a rule set via the app's configuration is not removed.

    Args:
        ptype: Policy type of the rule.
        rule: Values of the rule.
    """
    # Escaped, so that values cannot forge log lines
    values = ", ".join(f"'{escape_log_value(value)}'" for value in rule)
    logger.warning(
        "Policy rule (%s, %s) is set via the configuration "
        "('access_control.policies') and is not removed, as it would be "
        "added again on the next start. Remove it from the configuration "
        "instead.",
        ptype,
        values,
    )
