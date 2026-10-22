"""Controllers for managing roles on owned resources.

Optional, ready-made controllers on top of the resource ownership helpers
(cf. :py:mod:`foca.security.access_control.ownership`), which apps route to
from their own API specifications (via `operationId`), under paths of their
choice, e.g.:

* `GET /tasks/{id}/permissions`: :py:func:`getResourceRoles`
* `PUT /tasks/{id}/permissions/users/{userId}`: :py:func:`putUserRole`
* `DELETE /tasks/{id}/permissions/users/{userId}`: :py:func:`deleteUserRole`
* `PUT /tasks/{id}/permissions/groups/{group}`: :py:func:`putGroupRole`
* `DELETE /tasks/{id}/permissions/groups/{group}`: :py:func:`deleteGroupRole`

The resource is taken from the route of the request: its path up to the last
path parameter before the subject's (e.g., `/tasks/1` for
`/tasks/1/permissions/users/alice`), or up to the last path parameter for
:py:func:`getResourceRoles`. Path parameters may have any names; the subject
is the last one. Resources without owners are not found. Who may call the
endpoints is decided by the app's policies, which must be enforced on them, as
the controllers do not check the caller themselves: apps routing operations
that do not enforce policies to the controllers do not start, and requests on
which policies were not enforced (e.g., via wrappers of the controllers) are
refused. Requests and responses are logged unless disabled via config option
`security.access_control.log_traffic`.

The controllers take an optional existence check as `exists` (cf.
:py:func:`putUserRole`) and request bodies as `body`, so neither may be the
name of a parameter of operations routed to them.
"""

import logging
import re
from collections.abc import Callable
from functools import wraps
from inspect import unwrap
from typing import Any, NamedTuple

from flask import Flask, current_app, request
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

# Internal helpers of the same package: whether traffic is logged (as for
# the permissions API), subject descriptions for logs and owners whose role
# applies
from foca.security.access_control.access_control_server import (
    _permissions_traffic_logged,
)
from foca.security.access_control.enforcement import (
    RESOURCE_ROLE_PTYPE,
    SubjectType,
    _enforces_access_control,
    get_current_group_values,
)
from foca.security.access_control.ownership import (
    _describe,
    _describe_caller,
    _get_owners,
    grant_role,
    is_admin,
    list_roles,
    revoke_roles,
)
from foca.utils.logging import escape_log_value, log_traffic_if

logger = logging.getLogger(__name__)

# Variables of Flask URL rules, e.g., `<id>` or `<path:group>`
_RULE_VARIABLE = re.compile(r"<(?:[^<>:]+:)?([^<>:]+)>")
# Methods that Flask adds to routes itself
_AUTOMATIC_METHODS = frozenset({"HEAD", "OPTIONS"})


def _require_enforcement(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator refusing requests on which policies were not enforced.

    The controllers do not check who is calling: without enforcement, any
    caller could grant themselves roles. Policies are enforced if the view
    function of the route enforces them (cf.
    :py:func:`foca.security.access_control.enforce_access_control`), e.g.,
    for wrappers of the controllers, which :py:func:`_check_routes_enforced`
    cannot find.

    Args:
        fn: Controller.

    Returns:
        The decorated controller.

    Raises:
        RuntimeError: Policies are not enforced on the route of the request,
            or the request was not routed via a URL rule.
    """

    @wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        rule = request.url_rule
        view = None if rule is None else current_app.view_functions.get(rule.endpoint)
        if view is None or not _enforces_access_control(view):
            message = (
                f"Access control policies are not enforced on route "
                f"'{request.method} {getattr(rule, 'rule', None)}', so "
                f"controller '{fn.__name__}' refuses requests"
            )
            logger.error(escape_log_value(message))
            raise RuntimeError(message)
        return fn(*args, **kwargs)

    return _wrapper


class _Route(NamedTuple):
    """Route of a request: its URL rule and the values of its variables.

    Attributes:
        rule: URL rule, e.g., `/tasks/<id>/permissions/users/<userId>`.
        variables: Name and position (start, end) of each variable of the
            rule, in order.
        values: Values of the variables, as routed (i.e., decoded).
    """

    rule: str
    variables: list[tuple[str, int, int]]
    values: dict[str, Any]


def _get_route() -> _Route:
    """Get the route of the current request.

    Returns:
        URL rule, variables and their values.

    Raises:
        RuntimeError: The current request was not routed via a URL rule.
    """
    rule = request.url_rule
    if rule is None or request.view_args is None:
        raise RuntimeError("The current request was not routed via a URL rule")
    variables = [
        (match.group(1), match.start(), match.end())
        for match in _RULE_VARIABLE.finditer(rule.rule)
    ]
    return _Route(rule.rule, variables, request.view_args)


def _get_resource(with_subject: bool) -> str:
    """Get the resource of the current request from its route.

    Args:
        with_subject: Whether the last path parameter is the subject.

    Returns:
        Path of the resource: the request path up to the last path parameter
        (before the subject's, if `with_subject` is set), with the values of
        the path parameters, as routed (i.e., decoded).

    Raises:
        RuntimeError: The route has too few path parameters.
    """
    route = _get_route()
    count = len(route.variables) - (1 if with_subject else 0)
    if count < 1:
        raise RuntimeError(
            f"Route '{route.rule}' has no path parameter identifying the resource"
        )
    resource = ""
    position = 0
    for name, start, end in route.variables[:count]:
        resource += f"{route.rule[position:start]}{route.values[name]}"
        position = end
    return resource


def _get_subject() -> str:
    """Get the subject of the current request, i.e., its last path parameter."""
    route = _get_route()
    return str(route.values[route.variables[-1][0]])


def _get_owned_resource(with_subject: bool) -> str:
    """Get the resource of the current request, which must have owners.

    Args:
        with_subject: Whether the last path parameter is the subject.

    Returns:
        Path of the resource (cf. :py:func:`_get_resource`).

    Raises:
        werkzeug.exceptions.NotFound: The resource has no owners (whose
            role applies), i.e., it does not exist (anymore).
    """
    resource = _get_resource(with_subject)
    if not _get_owners(resource, RESOURCE_ROLE_PTYPE):
        raise NotFound
    return resource


def _get_role(body: Any) -> str:
    """Get the role from a request body (`{"role": ...}`).

    Args:
        body: Request body.

    Returns:
        Role.

    Raises:
        werkzeug.exceptions.BadRequest: The body is not an object with a
            role (a string).
    """
    role = body.get("role") if isinstance(body, dict) else None
    if not isinstance(role, str):
        raise BadRequest("The request body must contain a role")
    return role


def _put_role(
    subject: str,
    subject_type: SubjectType,
    body: Any,
    exists: Callable[[], bool] | None,
) -> dict[str, str]:
    """Grant a subject the role in the request body, replacing others.

    Args:
        subject: Subject without prefix.
        subject_type: Type of the subject.
        body: Request body.
        exists: Function checking whether the resource exists (cf.
            :py:func:`foca.security.access_control.grant_role`).

    Returns:
        Subject type, subject and granted role.

    Raises:
        werkzeug.exceptions.BadRequest: The role is not one of
            `access_control.resource_roles`, or the role cannot be granted to
            the subject (cf.
            :py:func:`foca.security.access_control.grant_role`).
        werkzeug.exceptions.NotFound: The resource has no owners, or `exists`
            returned `False` after the role was granted.
        foca.security.access_control.LastOwner: The subject is the last
            owner of the resource, and the role is not `owner`.
    """
    role = _get_role(body)
    resource = _get_owned_resource(with_subject=True)
    try:
        grant_role(resource, subject, role, subject_type=subject_type, exists=exists)
    # Messages may name configuration options, so they are only logged
    except ValueError as exc:
        # The resource may have been deleted since it was checked
        if not _get_owners(resource, RESOURCE_ROLE_PTYPE):
            raise NotFound from exc
        logger.info(
            "Role '%s' on '%s' not granted to %s: %s",
            escape_log_value(role),
            escape_log_value(resource),
            _describe(subject_type, subject),
            escape_log_value(exc),
        )
        raise BadRequest("The role cannot be granted to the subject") from exc
    return {"subject_type": subject_type, "subject": subject, "role": role}


def _delete_roles(
    subject: str,
    subject_type: SubjectType,
    exists: Callable[[], bool] | None,
) -> str:
    """Revoke all roles of a subject.

    Args:
        subject: Subject without prefix.
        subject_type: Type of the subject.
        exists: Function checking whether the resource exists (cf.
            :py:func:`foca.security.access_control.revoke_roles`).

    Returns:
        The subject.

    Raises:
        werkzeug.exceptions.NotFound: The resource has no owners, or the
            subject has no roles on it.
        foca.security.access_control.LastOwner: The subject is the last
            owner of the resource.
    """
    resource = _get_owned_resource(with_subject=True)
    if not revoke_roles(resource, subject, subject_type=subject_type, exists=exists):
        raise NotFound
    return subject


@log_traffic_if(_permissions_traffic_logged)
@_require_enforcement
def getResourceRoles(**kwargs: Any) -> list[dict[str, str]]:
    """List the roles granted on the resource of the route.

    Args:
        **kwargs: Path parameters and further arguments that Connexion
            passes (e.g., `user`, `token_info`); unused, as the resource is
            taken from the route.

    Returns:
        Subject type, subject and role of each role granted on the resource
        (cf. :py:func:`foca.security.access_control.list_roles`).

    Raises:
        werkzeug.exceptions.NotFound: The resource has no owners.
    """
    resource = _get_owned_resource(with_subject=False)
    return [
        granted._asdict() for granted in list_roles(resource, ptype=RESOURCE_ROLE_PTYPE)
    ]


@log_traffic_if(_permissions_traffic_logged)
@_require_enforcement
def putUserRole(
    body: Any = None,
    exists: Callable[[], bool] | None = None,
    **kwargs: Any,
) -> dict[str, str]:
    """Grant the user of the route the role in the request body.

    Other roles of `access_control.resource_roles` of the user on the
    resource are replaced (cf.
    :py:func:`foca.security.access_control.grant_role`).

    Args:
        body: Request body, with the role to grant (`{"role": "owner"}`).
        exists: Function checking whether the resource exists, so that a
            role granted while the resource is deleted is revoked again;
            passed by apps that route to a wrapper of the controller. Not
            checked if `None`.
        **kwargs: Path parameters and further arguments that Connexion
            passes; unused, as resource and user are taken from the route.

    Returns:
        Subject type (`user`), user identifier and granted role.
    """
    return _put_role(_get_subject(), "user", body, exists)


@log_traffic_if(_permissions_traffic_logged)
@_require_enforcement
def deleteUserRole(exists: Callable[[], bool] | None = None, **kwargs: Any) -> str:
    """Revoke the roles of the user of the route on the resource.

    Args:
        exists: Function checking whether the resource exists, if an owner
            role removed concurrently is restored (cf. :py:func:`putUserRole`).
        **kwargs: Path parameters and further arguments that Connexion
            passes; unused, as resource and user are taken from the route.

    Returns:
        User identifier.
    """
    return _delete_roles(_get_subject(), "user", exists)


@log_traffic_if(_permissions_traffic_logged)
@_require_enforcement
def putGroupRole(
    body: Any = None,
    exists: Callable[[], bool] | None = None,
    **kwargs: Any,
) -> dict[str, str]:
    """Grant the group of the route the role in the request body.

    Callers may only grant roles to groups they are members of (i.e., whose
    value is in their group claim, cf.
    :py:func:`foca.security.access_control.get_current_group_values`), as
    they could otherwise hand resources to any group; admins (cf.
    :py:func:`foca.security.access_control.is_admin`) may grant roles to
    any group.

    Args:
        body: Request body, with the role to grant (`{"role": "owner"}`).
        exists: Function checking whether the resource exists (cf.
            :py:func:`putUserRole`).
        **kwargs: Path parameters and further arguments that Connexion
            passes; unused, as resource and group are taken from the route.

    Returns:
        Subject type (`group`), group and granted role.

    Raises:
        werkzeug.exceptions.Forbidden: The caller is neither a member of the
            group nor an admin.
    """
    group = _get_subject()
    if group not in get_current_group_values() and not is_admin():
        logger.info(
            "Role not granted to %s by %s: not a member of the group",
            _describe("group", group),
            _describe_caller(),
        )
        raise Forbidden
    return _put_role(group, "group", body, exists)


@log_traffic_if(_permissions_traffic_logged)
@_require_enforcement
def deleteGroupRole(exists: Callable[[], bool] | None = None, **kwargs: Any) -> str:
    """Revoke the roles of the group of the route on the resource.

    Callers allowed by the policies may revoke roles of any group, also of
    groups they are not members of.

    Args:
        exists: Function checking whether the resource exists (cf.
            :py:func:`deleteUserRole`).
        **kwargs: Path parameters and further arguments that Connexion
            passes; unused, as resource and group are taken from the route.

    Returns:
        The group.
    """
    return _delete_roles(_get_subject(), "group", exists)


_CONTROLLERS = (
    getResourceRoles,
    putUserRole,
    deleteUserRole,
    putGroupRole,
    deleteGroupRole,
)


def _check_routes_enforced(app: Flask) -> None:
    """Check that policies are enforced on the routes to the controllers.

    Routes are found by their view functions, unwrapping decorators (e.g.,
    of Connexion), so routes to aliases of the controllers are found too, but
    not routes to wrappers of them (which refuse unenforced requests, cf.
    :py:func:`_require_enforcement`).

    Args:
        app: Flask app, with API specifications registered.

    Raises:
        ValueError: Routes to the controllers do not enforce access control
            policies (e.g., with `access_control.enforce` disabled, or for
            operations without security requirements), so that any caller
            could grant themselves roles.
    """
    controllers = {unwrap(controller): controller for controller in _CONTROLLERS}
    unenforced = []
    for rule in app.url_map.iter_rules():
        view = app.view_functions[rule.endpoint]
        controller = controllers.get(unwrap(view))
        if controller is None or _enforces_access_control(view):
            continue
        for method in sorted((rule.methods or set()) - _AUTOMATIC_METHODS):
            unenforced.append(f"{method} {rule.rule} ({controller.__name__})")
    if unenforced:
        raise ValueError(
            "Access control policies are not enforced on operations routed to "
            f"controllers of '{__name__}', so any caller could grant "
            f"themselves roles: {', '.join(unenforced)}. Enforce policies on "
            "them: enable 'security.access_control.enforce', require "
            "authentication (security requirements without empty "
            "alternatives, no 'disable_auth') and do not set "
            "'x-foca-access-control: false'."
        )
