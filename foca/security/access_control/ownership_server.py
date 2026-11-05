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
is the last one. Resources without owners are not found, unless the caller is
an admin and an existence check finds them, so that admins can assign the
first owner of resources stored before an app adopted ownership. Who may call
the endpoints is decided by the app's policies, which must be enforced on
them, as the controllers do not check the caller themselves: apps routing operations
that do not enforce policies to the controllers do not start, and requests on
which policies were not enforced (e.g., via wrappers of the controllers, or
routes added later) are refused. Requests and responses are logged unless
disabled via config option `security.access_control.log_traffic`.

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

from flask import Flask, current_app, g, request
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from foca.api.routes import get_routes, match
from foca.config.access import get_app_config

# Internal helpers of the same package: whether traffic is logged (as for
# the permissions API), subject descriptions for logs and owners whose role
# applies
from foca.security.access_control.access_control_server import (
    _permissions_traffic_logged,
)
from foca.security.access_control.enforcement import (
    _ENFORCED_MARK,
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
from foca.utils.pagination import get_link_header, get_page, get_page_links

logger = logging.getLogger(__name__)

# Variables of Flask URL rules, e.g., `<id>` or `<path:group>`
_RULE_VARIABLE = re.compile(r"<(?:[^<>:]+:)?([^<>:]+)>")


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
        found = match(request.path, request.method)
        # The mark, as a route outside of the table may have the same path, so
        # that the route found isn't the one that served the request
        if (
            found is None
            or not _enforces_access_control(found.route.function)
            or not getattr(g, _ENFORCED_MARK, False)
        ):
            message = (
                "Access control policies are not enforced on route "
                f"'{request.method} {found.route.rule if found else None}', so "
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
    found = match(request.path, request.method)
    if found is None:
        raise RuntimeError("The current request was not routed via a URL rule")
    rule = found.route.rule
    variables = [
        (variable.group(1), variable.start(), variable.end())
        for variable in _RULE_VARIABLE.finditer(rule)
    ]
    return _Route(rule, variables, found.params)


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


def _is_manageable(resource: str, exists: Callable[[], bool] | None) -> bool:
    """Check whether the caller may manage the roles of a resource.

    Args:
        resource: Path of the resource.
        exists: Function checking whether the resource exists, if any; only
            called for admins, on resources without owners.

    Returns:
        Whether the resource has owners (whose role applies) or, if not, the
        caller is an admin and `exists` finds the resource.
    """
    return bool(_get_owners(resource, RESOURCE_ROLE_PTYPE)) or (
        exists is not None and is_admin() and exists()
    )


def _get_owned_resource(
    with_subject: bool, exists: Callable[[], bool] | None = None
) -> str:
    """Get the resource of the current request, which must be manageable.

    Args:
        with_subject: Whether the last path parameter is the subject.
        exists: Function checking whether the resource exists, for admins
            managing resources without owners. Not called for others.

    Returns:
        Path of the resource (cf. :py:func:`_get_resource`).

    Raises:
        werkzeug.exceptions.NotFound: The resource has no owners (whose
            role applies), and either the caller is not an admin, or there is
            no function checking whether the resource exists, or it found
            that the resource doesn't exist.
    """
    resource = _get_resource(with_subject)
    if not _is_manageable(resource, exists):
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
        werkzeug.exceptions.NotFound: The resource has no owners (unless the
            caller is an admin and `exists` returned `True`), or `exists`
            returned `False` after the role was granted.
        foca.security.access_control.LastOwner: The subject is the last
            owner of the resource, and the role is not `owner`.
    """
    role = _get_role(body)
    resource = _get_owned_resource(with_subject=True, exists=exists)
    try:
        grant_role(resource, subject, role, subject_type=subject_type, exists=exists)
    # Messages may name configuration options, so they are only logged
    except ValueError as exc:
        # The resource may have been deleted since it was checked
        if not _is_manageable(resource, exists):
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
def getResourceRoles(
    exists: Callable[[], bool] | None = None,
    q: str | None = None,
    limit: int | str | None = None,
    offset: int | str | None = None,
    **kwargs: Any,
) -> list[dict[str, str]] | tuple[list[dict[str, str]], int, dict[str, str]]:
    """List the roles granted on the resource of the route.

    Declare the query parameters `q`, `limit` and `offset` in the specification to
    offer them.

    Args:
        exists: Function checking whether the resource exists, so that admins
            can list the roles of a resource without owners (cf.
            :py:func:`putUserRole`).
        q: Only list the roles of subjects (users, groups or service clients)
            with this text in their identifier, in any case. Like `limit` and
            `offset`, ignored if it's a path parameter.
        limit: Largest number of roles to return. With this or `offset`, a page
            of the roles is returned, sorted so that pages are stable (cf.
            :py:func:`foca.utils.pagination.get_page`, which also takes the values
            as strings, for apps that don't declare a type).
        offset: Number of roles to skip.
        **kwargs: Path parameters and further arguments that Connexion
            passes (e.g., `user`, `token_info`); unused, as the resource is
            taken from the route.

    Returns:
        Subject type, subject and role of each role granted on the resource
        (cf. :py:func:`foca.security.access_control.list_roles`). For a page, also
        status code `200` and a header `Link` with the links to other pages, if the
        app has a public URL (`server.public_url`).

    Raises:
        werkzeug.exceptions.BadRequest: `q`, `limit` or `offset` is invalid.
        werkzeug.exceptions.NotFound: The resource has no owners, unless the
            caller is an admin and `exists` returned `True`.
    """
    # Parameters of the path name the resource, whatever they are called
    path_parameters = request.view_args or {}
    q = None if "q" in path_parameters else q
    limit = None if "limit" in path_parameters else limit
    offset = None if "offset" in path_parameters else offset
    if q is not None and not isinstance(q, str):
        raise BadRequest("'q' must be a string.")
    page = None if limit is None and offset is None else get_page(offset, limit)
    resource = _get_owned_resource(with_subject=False, exists=exists)
    roles = [
        granted._asdict() for granted in list_roles(resource, ptype=RESOURCE_ROLE_PTYPE)
    ]
    if q:
        text = q.casefold()
        roles = [role for role in roles if text in role["subject"].casefold()]
    if page is None:
        return roles
    roles.sort(key=lambda role: (role["subject_type"], role["subject"], role["role"]))
    headers: dict[str, str] = {}
    if get_app_config(current_app).server.public_url is not None:
        headers["Link"] = get_link_header(get_page_links(page, total=len(roles)))
    return roles[page.offset : page.offset + page.limit], 200, headers


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
            role granted while the resource is deleted is revoked again, and
            admins can assign the first owner of a resource without owners;
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
    for route in get_routes(app):
        controller = controllers.get(unwrap(route.function))
        if controller is None or _enforces_access_control(route.function):
            continue
        unenforced.append(f"{route.method} {route.rule} ({controller.__name__})")
    if unenforced:
        raise ValueError(
            "Access control policies are not enforced on operations routed to "
            f"controllers of '{__name__}', so any caller could grant "
            f"themselves roles: {', '.join(unenforced)}. Enforce policies on "
            "them: enable 'security.access_control.enforce', require "
            "authentication (security requirements without empty "
            "alternatives, no 'disable_auth') and do not set "
            "'x-foca-access-control: false' or 'x-foca-self-service: true'."
        )
