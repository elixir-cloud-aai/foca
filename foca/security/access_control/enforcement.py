"""Enforcement of access control policies and helpers for managing them."""

import logging
from collections.abc import Callable
from functools import wraps
from typing import Literal

from casbin import Enforcer
from connexion.exceptions import Forbidden, Unauthorized
from connexion.resolver import Resolution, Resolver
from flask import current_app, has_request_context, request
from flask.globals import request_ctx

from foca.config.access import get_config
from foca.security.access_control.constants import GROUP_PREFIX
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    validate_rule,
    validate_rule_value,
)

logger = logging.getLogger(__name__)

# Policy type of resource-scoped role rules: subject, role, resource
RESOURCE_ROLE_PTYPE = "g2"

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
        f"Unsupported subject type '{subject_type}': expected 'user' or "
        "'group'"
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
            conf.group_mapping[value]
            for value in values
            if value in conf.group_mapping
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
    return request_path == resource_path or request_path.startswith(
        f"{resource_path}/"
    )


def get_enforcer() -> Enforcer:
    """Get Casbin enforcer with current policies of the app.

    If `access_control.resource_roles_cover_subpaths` is set, roles of
    resource-scoped role definitions (with three fields, e.g.,
    `g2 = _, _, _`) also apply to sub-paths of the resource path they were
    granted on.

    Returns:
        Casbin enforcer for the app's model with policies loaded from the
        app's access control database.
    """
    enforcer = Enforcer(
        current_app.config["CASBIN_MODEL"],
        current_app.config["casbin_adapter"],
    )
    if _get_access_control_config().resource_roles_cover_subpaths:
        roles = enforcer.get_model().model.get("g", {})
        for ptype, assertion in roles.items():
            if assertion.value.count("_") == 3:
                enforcer.add_named_domain_matching_func(ptype, _is_within_path)
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
        raise Unauthorized("Subject could not be identified")
    try:
        validate_rule_value(_prefix_subject(subject))
    except ValueError as exc:
        logger.info(f"Access denied: invalid subject: {exc}")
        return False
    enforcer = get_enforcer()
    for _subject in [_prefix_subject(subject), *get_current_groups()]:
        if enforcer.enforce(_subject, request.path, request.method):
            logger.debug(
                f"Access granted: {request.method} {request.path} to "
                f"'{subject}' as '{_subject}'"
            )
            return True
    logger.info(
        f"Access denied: {request.method} {request.path} to '{subject}'"
    )
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
            f"Enforcing access control: {operation.method.upper()} "
            f"{operation.path}"
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
    assertion = enforcer.get_model().model.get("g", {}).get(ptype)
    if assertion is None or assertion.value.count("_") != 3:
        raise ValueError(
            f"Access control model does not define resource-scoped roles: "
            f"expected role definition '{ptype} = _, _, _'"
        )
    return enforcer


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
        ValueError: The subject is empty, the subject type is not supported,
            or the rule is not valid (cf.
            :py:func:`foca.security.access_control.foca_casbin_adapter.casbin_rule.validate_rule`).
    """
    if not subject:
        raise ValueError("Subjects must not be empty")
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
            f"Role '{role}' granted to group '{subject}' on '{resource}' "
            "does not apply: group claim values are not used as subjects "
            "with 'access_control.group_mapping', unless "
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
        `True` if the role was revoked, `False` if it did not exist.

    Raises:
        ValueError: The model does not define resource-scoped roles, the
            subject is empty, the subject type is not supported, or the
            prefixed subject, role or resource is not a valid value for
            policy rules. Unlike :py:func:`add_resource_role`, roles
            starting with `access_control.subject_prefix` or `group:` are
            accepted, so that such rules (e.g., added before they were
            rejected) can be revoked.
    """
    enforcer = _get_resource_role_enforcer(ptype)
    rule = _get_resource_role_rule(
        subject, role, resource, ptype, subject_type, enforcer
    )
    return enforcer.remove_named_grouping_policy(ptype, *rule)


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
        Subject and role of each role granted on the resource. Subjects are
        returned without the prefix of `subject_type`, if given.

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
    return [(rule[0].removeprefix(prefix), rule[1]) for rule in rules]


def remove_resource(
    resource: str,
    ptype: str = RESOURCE_ROLE_PTYPE,
) -> bool:
    """Revoke all roles granted on a resource, e.g., after deleting it.

    Args:
        resource: Resource to revoke roles on, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.

    Returns:
        `True` if any roles were revoked.

    Raises:
        ValueError: The model does not define resource-scoped roles.
    """
    return _get_resource_role_enforcer(
        ptype
    ).remove_filtered_named_grouping_policy(ptype, 2, resource)
