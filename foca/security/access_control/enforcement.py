"""Enforcement of access control policies and helpers for managing them."""

import logging
import threading
from collections.abc import Callable
from functools import wraps
from typing import Literal

from casbin import Enforcer
from casbin.model import Model
from connexion.exceptions import Forbidden
from connexion.resolver import Resolution, Resolver
from flask import current_app, has_request_context, request
from flask.globals import request_ctx

from foca.config.access import get_config
from foca.security.access_control.constants import GROUP_PREFIX
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    validate_rule,
    validate_rule_value,
)
from foca.utils.logging import escape_log_value

logger = logging.getLogger(__name__)

# Policy type of resource-scoped role rules: subject, role, resource
RESOURCE_ROLE_PTYPE = "g2"

# Key of the app config marking that loaded resource-scoped roles were
# checked for roles that are subjects
_SUBJECT_ROLES_CHECKED_KEY = "FOCA_ACCESS_CONTROL_SUBJECT_ROLES_CHECKED"
_subject_roles_check_lock = threading.Lock()

SubjectType = Literal["user", "group"]
"""Types of subjects of resource role helpers: users or groups."""


def _get_access_control_config():
    """Get access control configuration of current app."""
    return get_config().security.access_control


def _get_claims() -> dict:
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
            with `access_control.subject_prefix`) or `group` for group claim
            values (prefixed with `group:`).

    Returns:
        Prefix of subjects of the type.

    Raises:
        ValueError: The subject type is not supported.
    """
    if subject_type == "user":
        return _get_access_control_config().subject_prefix
    if subject_type == "group":
        return GROUP_PREFIX
    raise ValueError(
        f"Unsupported subject type '{subject_type}': expected 'user' or 'group'"
    )


def _prefix_subject(subject: str, subject_type: SubjectType = "user") -> str:
    """Add prefix of a subject type to a subject, as used in policy rules."""
    return f"{_get_subject_prefix(subject_type)}{subject}"


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


def _is_within_path(request_path: str, resource_path: str) -> bool:
    """Check whether a request path is a resource path or a sub-path.

    Args:
        request_path: Request path.
        resource_path: Resource path a role was granted on. Trailing
            slashes are ignored. The root path `/` only matches itself.

    Returns:
        `True` if `request_path` equals `resource_path` or starts with
        `resource_path` followed by `/`.
    """
    resource_path = resource_path.rstrip("/")
    if not resource_path:
        return request_path == "/"
    return request_path == resource_path or request_path.startswith(f"{resource_path}/")


def _get_resource_role_ptypes(model: Model) -> list[str]:
    """Get policy types of resource-scoped role definitions of a model.

    Args:
        model: Casbin model.

    Returns:
        Policy types of role definitions with three fields (subject, role,
        resource), e.g., `g2` for `g2 = _, _, _`.
    """
    return [
        ptype
        for ptype, assertion in model.model.get("g", {}).items()
        if assertion.value.count("_") == 3
    ]


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
    prefixes = (_get_subject_prefix("user"), _get_subject_prefix("group"))
    for ptype in _get_resource_role_ptypes(enforcer.get_model()):
        for subject, role, resource in enforcer.get_named_grouping_policy(ptype):
            if role.startswith(prefixes):
                # Values are quoted, so that they cannot forge log lines
                logger.warning(
                    f"Policy rule ({ptype}, {subject!r}, {role!r}, "
                    f"{resource!r}) grants a role that is a subject, giving "
                    f"{subject!r} the rights of {role!r} on {resource!r}. "
                    "Revoke it, if not intended, via "
                    "`remove_resource_role()` or the permissions API."
                )


def get_enforcer() -> Enforcer:
    """Get Casbin enforcer with current policies of the app.

    If `access_control.resource_roles_cover_subpaths` is set, roles of
    resource-scoped role definitions (with three fields, e.g.,
    `g2 = _, _, _`) also apply to sub-paths of the resource path they were
    granted on. When policies are first loaded, a warning is logged for
    each stored resource-scoped role that is a subject (cf.
    :py:func:`add_resource_role`).

    Returns:
        Casbin enforcer for the app's model with policies loaded from the
        app's access control database.
    """
    enforcer = Enforcer(
        current_app.config["CASBIN_MODEL"],
        current_app.config["casbin_adapter"],
    )
    if _get_access_control_config().resource_roles_cover_subpaths:
        for ptype in _get_resource_role_ptypes(enforcer.get_model()):
            enforcer.add_named_domain_matching_func(ptype, _is_within_path)
    _warn_subject_like_roles(enforcer)
    return enforcer


def enforce_access_control(fn: Callable) -> Callable:
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
    def _wrapper(*args, **kwargs):
        if not is_permitted():
            raise Forbidden
        return fn(*args, **kwargs)

    return _wrapper


def is_permitted() -> bool:
    """Check whether the current request is allowed by the policies.

    The subject (cf. :py:func:`get_current_subject`), prefixed with
    `access_control.subject_prefix`, and any roles derived from its group
    claims (cf. :py:func:`get_current_groups`) are checked against the
    request path and HTTP method.

    Returns:
        `True` if the subject or any of its roles is allowed to access the
        resource, `False` if not, or if the subject is not a valid value for
        policy rules (cf. :py:func:`validate_rule_value`).

    Raises:
        connexion.exceptions.Unauthorized: The subject could not be
            identified.
    """
    subject = get_current_subject()
    if subject is None:
        from foca.security.auth import _unauthorized  # noqa: PLC0415 (import cycle)

        # No `invalid_token` error code for validated tokens lacking the
        # identity claim, as clients cannot get tokens with the claim by
        # logging in again
        raise _unauthorized("Subject could not be identified", invalid_token=False)
    try:
        validate_rule_value(_prefix_subject(subject))
    except ValueError as exc:
        logger.info(f"Access denied: invalid subject: {exc}")
        return False
    enforcer = get_enforcer()
    # Escaped, so that request paths and claims cannot forge log lines
    _request = escape_log_value(f"{request.method} {request.path}")
    for _subject in [_prefix_subject(subject), *get_current_groups()]:
        if enforcer.enforce(_subject, request.path, request.method):
            logger.debug(
                f"Access granted: {_request} to "
                f"'{escape_log_value(subject)}' as "
                f"'{escape_log_value(_subject)}'"
            )
            return True
    logger.info(f"Access denied: {_request} to '{escape_log_value(subject)}'")
    return False


def _requires_authentication(security: list[dict] | None) -> bool:
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
        resolver: Resolver | Callable | None = None,
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

    def _is_enforced(self, operation) -> bool:
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

    def resolve(self, operation) -> Resolution:
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
            f"Enforcing access control: {operation.method.upper()} {operation.path}"
        )
        return Resolution(
            function=enforce_access_control(resolution.function),
            operation_id=resolution.operation_id,
        )


def _get_resource_role_enforcer(ptype: str) -> Enforcer:
    """Get Casbin enforcer and check model for resource-scoped roles.

    Args:
        ptype: Policy type of resource-scoped role rules.

    Returns:
        Casbin enforcer.

    Raises:
        ValueError: The model does not define `ptype` as a role definition
            with three fields (subject, role, resource).
    """
    enforcer = get_enforcer()
    _check_resource_role_model(ptype, enforcer.get_model())
    return enforcer


def _check_resource_role_model(ptype: str, model: Model) -> None:
    """Check that a model defines resource-scoped roles.

    Args:
        ptype: Policy type of resource-scoped role rules.
        model: Casbin model.

    Raises:
        ValueError: The model does not define `ptype` as a role definition
            with three fields (subject, role, resource).
    """
    if ptype not in _get_resource_role_ptypes(model):
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
        ValueError: The role starts with `access_control.subject_prefix` or
            `group:`.
    """
    for prefix in [_get_subject_prefix("user"), _get_subject_prefix("group")]:
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
            (e.g., as returned by :py:func:`get_current_subject`), or a group
            claim value if `subject_type` is `group`. It is prefixed with
            `access_control.subject_prefix` or `group:`, respectively, in the
            rule.
        role: Role to grant (e.g., `owner`).
        resource: Resource to grant role on, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.
        subject_type: Type of the subject: `user` or `group`.

    Returns:
        `True` if the role was granted, `False` if it already existed.

    Raises:
        ValueError: The model does not define resource-scoped roles, the
            subject is empty, the subject type is not supported, the role
            starts with `access_control.subject_prefix` or `group:` (so that
            subjects cannot get the roles of other subjects through it), or
            the prefixed subject, role or resource is not a valid value for
            policy rules (cf.
            :py:func:`foca.security.access_control.foca_casbin_adapter.casbin_rule.validate_rule_value`).
    """
    # Only checked when granting roles, so that rules with empty subjects
    # (e.g., stored by previous versions) can still be revoked
    if not subject:
        raise ValueError("Subjects must not be empty")
    enforcer = _get_resource_role_enforcer(ptype)
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
            f"Role '{escape_log_value(role)}' granted to group "
            f"'{escape_log_value(subject)}' on "
            f"'{escape_log_value(resource)}' does not apply: group claim "
            "values are not used as subjects with "
            "'access_control.group_mapping', unless "
            "'access_control.keep_group_subjects' is set"
        )
    return enforcer.add_named_grouping_policy(ptype, *rule)


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
            identifier, or a group claim value if `subject_type` is `group`.
        role: Role to revoke (e.g., `owner`).
        resource: Resource to revoke role on, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.
        subject_type: Type of the subject: `user` or `group`.

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
            with `access_control.subject_prefix` or `group:` are accepted,
            so that such rules (e.g., stored before they were rejected) can
            be revoked.
    """
    # Subject and role checks of `add_resource_role()` do not apply here
    enforcer = _get_resource_role_enforcer(ptype)
    rule = _get_resource_role_rule(
        subject, role, resource, ptype, subject_type, enforcer
    )
    # Removes unmarked copies only; the adapter keeps configured rules,
    # also if they were marked after the policies were loaded
    removed = enforcer.remove_named_grouping_policy(ptype, *rule)
    if _get_adapter().is_configured_rule(ptype, rule):
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
        subject_type: Type of subjects to list roles of: `user` or `group`.
            If `None`, roles of all subjects are listed, with user
            identifiers without `access_control.subject_prefix` and all other
            subjects as is (e.g., `group:vo-members`), so that a user whose
            identifier starts with `group:` cannot be told apart from a
            group; pass the type to tell them apart.

    Returns:
        Subject and role of each role granted on the resource, without
        duplicates. Subjects are returned without the prefix of
        `subject_type`, if given.

    Raises:
        ValueError: The model does not define resource-scoped roles, or the
            subject type is not supported.
    """
    enforcer = _get_resource_role_enforcer(ptype)
    rules = enforcer.get_filtered_named_grouping_policy(ptype, 2, resource)
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
    # Policies are loaded anew for each enforcer, so rules can be removed
    # via the adapter, without loading them
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
        f"Policy rule ({ptype}, {values}) is set via the configuration "
        "('access_control.policies') and is not removed, as it would be "
        "added again on the next start. Remove it from the configuration "
        "instead."
    )
