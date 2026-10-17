"""Enforcement of access control policies and helpers for managing them."""

import logging
from collections.abc import Callable
from functools import wraps

from casbin import Enforcer
from connexion import context
from connexion.exceptions import Forbidden, Unauthorized
from connexion.resolver import Resolution, Resolver
from flask import current_app, request

from foca.security.access_control.constants import GROUP_PREFIX

logger = logging.getLogger(__name__)

# Policy type of resource-scoped role rules: subject, role, resource
RESOURCE_ROLE_PTYPE = "g2"


def _get_access_control_config():
    """Get access control configuration of current app."""
    return current_app.config.foca.security.access_control  # type: ignore[attr-defined]


def _get_claims() -> dict:
    """Get claims of the validated token of the current request.

    Returns:
        Claims of the token validated by Connexion's security handler for the
        current request, or an empty dictionary if not available.
    """
    try:
        token_info = context.get("token_info")
    except (AttributeError, RuntimeError):
        return {}
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
        claim = current_app.config.foca.security.auth.claim_identity  # type: ignore[attr-defined]
    subject = _get_claims().get(claim)
    return None if subject is None else str(subject)


def _prefix_subject(subject: str) -> str:
    """Add configured prefix to a subject, as used in policy rules."""
    return f"{_get_access_control_config().subject_prefix}{subject}"


def _unprefix_subject(subject: str) -> str:
    """Remove configured prefix from a subject used in policy rules."""
    prefix = _get_access_control_config().subject_prefix
    if prefix and subject.startswith(prefix):
        return subject[len(prefix) :]
    return subject


def get_current_groups() -> list[str]:
    """Get roles of the subject of the current request from group claims.

    Returns:
        Values of the claim configured via `access_control.claim_groups` of
        the validated token of the current request, mapped to roles via
        `access_control.group_mapping`, if set. Values without a mapping are
        dropped if a mapping is set. Without a mapping, values are prefixed
        with `group:` (e.g., `group:vo-members`), so that they cannot
        collide with role names.
    """
    conf = _get_access_control_config()
    if conf.claim_groups is None:
        return []
    values = _get_claims().get(conf.claim_groups, [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    groups = [str(value) for value in values]
    if conf.group_mapping is not None:
        groups = [
            conf.group_mapping[group]
            for group in groups
            if group in conf.group_mapping
        ]
    else:
        groups = [f"{GROUP_PREFIX}{group}" for group in groups]
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
            the resource.
    """

    @wraps(fn)
    def _wrapper(*args, **kwargs):
        subject = get_current_subject()
        if subject is None:
            raise Unauthorized("Subject could not be identified")
        enforcer = get_enforcer()
        for _subject in [_prefix_subject(subject), *get_current_groups()]:
            if enforcer.enforce(_subject, request.path, request.method):
                logger.debug(
                    f"Access granted: {request.method} {request.path} to "
                    f"'{subject}' as '{_subject}'"
                )
                return fn(*args, **kwargs)
        logger.info(
            f"Access denied: {request.method} {request.path} to '{subject}'"
        )
        raise Forbidden

    return _wrapper


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

    Attributes:
        resolver: Resolver to resolve functions with.
        enforce_all: Whether to enforce policies on all operations.
    """

    def __init__(
        self,
        resolver: Resolver | Callable | None = None,
        enforce_all: bool = False,
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

    def _is_enforced(self, operation) -> bool:
        """Check whether to enforce policies on an operation.

        Args:
            operation: Connexion operation.

        Returns:
            Whether to enforce policies on the operation.
        """
        if self.enforce_all:
            return True
        # Connexion only exposes vendor extensions via the operation object
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


def add_resource_role(
    subject: str,
    role: str,
    resource: str,
    ptype: str = RESOURCE_ROLE_PTYPE,
) -> bool:
    """Grant a subject a role on a resource.

    Requires a model with resource-scoped roles, i.e., a role definition
    `<ptype> = _, _, _` (e.g., `g2` in `resource_role_model.conf`).

    Args:
        subject: Subject (e.g., user identifier as returned by
            :py:func:`get_current_subject`) to grant role to. It is prefixed
            with `access_control.subject_prefix` in the rule.
        role: Role to grant (e.g., `owner`).
        resource: Resource to grant role on, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.

    Returns:
        `True` if the role was granted, `False` if it already existed.

    Raises:
        ValueError: The model does not define resource-scoped roles.
    """
    return _get_resource_role_enforcer(ptype).add_named_grouping_policy(
        ptype, _prefix_subject(subject), role, resource
    )


def remove_resource_role(
    subject: str,
    role: str,
    resource: str,
    ptype: str = RESOURCE_ROLE_PTYPE,
) -> bool:
    """Revoke a role of a subject on a resource.

    Args:
        subject: Subject (e.g., user identifier) to revoke role from.
        role: Role to revoke (e.g., `owner`).
        resource: Resource to revoke role on, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.

    Returns:
        `True` if the role was revoked, `False` if it did not exist.

    Raises:
        ValueError: The model does not define resource-scoped roles.
    """
    return _get_resource_role_enforcer(ptype).remove_named_grouping_policy(
        ptype, _prefix_subject(subject), role, resource
    )


def list_resource_roles(
    resource: str,
    ptype: str = RESOURCE_ROLE_PTYPE,
) -> list[tuple[str, str]]:
    """List roles granted on a resource.

    Args:
        resource: Resource to list roles of, i.e., its full request path.
        ptype: Policy type of resource-scoped role rules.

    Returns:
        Subject (without `access_control.subject_prefix`) and role of each
        role granted on the resource.

    Raises:
        ValueError: The model does not define resource-scoped roles.
    """
    enforcer = _get_resource_role_enforcer(ptype)
    return [
        (_unprefix_subject(rule[0]), rule[1])
        for rule in enforcer.get_filtered_named_grouping_policy(
            ptype, 2, resource
        )
    ]


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
