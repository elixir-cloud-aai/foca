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
endpoints is decided by the app's policies. Requests and responses are logged
unless disabled via config option `security.access_control.log_traffic`.
"""

import logging
import re
from typing import Any

from flask import request
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from foca.security.access_control.access_control_server import (
    _permissions_traffic_logged,
)
from foca.security.access_control.enforcement import (
    RESOURCE_ROLE_PTYPE,
    SubjectType,
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


def _get_route() -> tuple[str, list[str]]:
    """Get the URL rule of the current request and its path parameters.

    Returns:
        URL rule (e.g., `/tasks/<id>/permissions/users/<userId>`) and names
        of its variables, in order.

    Raises:
        RuntimeError: The current request was not routed via a URL rule.
    """
    rule = request.url_rule
    if rule is None or request.view_args is None:
        raise RuntimeError("The current request was not routed via a URL rule")
    return rule.rule, _RULE_VARIABLE.findall(rule.rule)


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
    rule, names = _get_route()
    count = len(names) - 1 if with_subject else len(names)
    if count < 1:
        raise RuntimeError(
            f"Route '{rule}' has no path parameter identifying the resource"
        )
    matches = list(_RULE_VARIABLE.finditer(rule))
    view_args = request.view_args or {}
    resource = ""
    position = 0
    for match in matches[:count]:
        resource += rule[position : match.start()] + str(view_args[match.group(1)])
        position = match.end()
    return resource


def _get_subject() -> str:
    """Get the subject of the current request, i.e., its last path parameter."""
    _, names = _get_route()
    return str((request.view_args or {})[names[-1]])


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

    Raises:
        werkzeug.exceptions.BadRequest: The body has no role.
    """
    role = body.get("role") if isinstance(body, dict) else None
    if not isinstance(role, str):
        raise BadRequest("The request body must contain a role")
    return role


def _put_role(subject: str, subject_type: SubjectType, body: Any) -> dict[str, str]:
    """Grant a subject the role in the request body, replacing others.

    Args:
        subject: Subject without prefix.
        subject_type: Type of the subject.
        body: Request body.

    Returns:
        Subject type, subject and granted role.

    Raises:
        werkzeug.exceptions.BadRequest: The role is not one of
            `access_control.resource_roles`, or the role cannot be granted to
            the subject (cf.
            :py:func:`foca.security.access_control.grant_role`).
        werkzeug.exceptions.NotFound: The resource has no owners, or had
            none left after the role was granted (e.g., as it was deleted
            meanwhile).
        foca.security.access_control.LastOwner: The subject is the last
            owner of the resource, and the role is not `owner`.
    """
    role = _get_role(body)
    resource = _get_owned_resource(with_subject=True)
    was_owner = (subject_type, subject) in _get_owners(resource, RESOURCE_ROLE_PTYPE)

    def _exists() -> bool:
        # Resources are deleted with their roles, so a resource whose only
        # owner is the one just granted was deleted meanwhile, unless the
        # subject was an owner before
        owners = _get_owners(resource, RESOURCE_ROLE_PTYPE)
        return was_owner or bool(owners - {(subject_type, subject)})

    try:
        grant_role(resource, subject, role, subject_type=subject_type, exists=_exists)
    # Messages may name configuration options, so they are only logged
    except ValueError as exc:
        logger.info(
            "Role '%s' on '%s' not granted to %s: %s",
            escape_log_value(role),
            escape_log_value(resource),
            _describe(subject_type, subject),
            escape_log_value(exc),
        )
        raise BadRequest("The role cannot be granted to the subject") from exc
    return {"subject_type": subject_type, "subject": subject, "role": role}


def _delete_roles(subject: str, subject_type: SubjectType) -> str:
    """Revoke all roles of a subject.

    Args:
        subject: Subject without prefix.
        subject_type: Type of the subject.

    Returns:
        The subject.

    Raises:
        werkzeug.exceptions.NotFound: The resource has no owners, or the
            subject has no roles on it.
        foca.security.access_control.LastOwner: The subject is the last
            owner of the resource.
    """
    resource = _get_owned_resource(with_subject=True)
    if not revoke_roles(resource, subject, subject_type=subject_type):
        raise NotFound
    return subject


@log_traffic_if(_permissions_traffic_logged)
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
def putUserRole(body: Any = None, **kwargs: Any) -> dict[str, str]:
    """Grant the user of the route the role in the request body.

    Other roles of `access_control.resource_roles` of the user on the
    resource are replaced (cf.
    :py:func:`foca.security.access_control.grant_role`).

    Args:
        body: Request body, with the role to grant (`{"role": "owner"}`).
        **kwargs: Path parameters and further arguments that Connexion
            passes; unused, as resource and user are taken from the route.

    Returns:
        Subject type (`user`), user identifier and granted role.
    """
    return _put_role(_get_subject(), "user", body)


@log_traffic_if(_permissions_traffic_logged)
def deleteUserRole(**kwargs: Any) -> str:
    """Revoke the roles of the user of the route on the resource.

    Args:
        **kwargs: Path parameters and further arguments that Connexion
            passes; unused, as resource and user are taken from the route.

    Returns:
        User identifier.
    """
    return _delete_roles(_get_subject(), "user")


@log_traffic_if(_permissions_traffic_logged)
def putGroupRole(body: Any = None, **kwargs: Any) -> dict[str, str]:
    """Grant the group of the route the role in the request body.

    Callers may only grant roles to groups they are members of (i.e., whose
    value is in their group claim, cf.
    :py:func:`foca.security.access_control.get_current_group_values`), as
    they could otherwise hand resources to any group; admins (cf.
    :py:func:`foca.security.access_control.is_admin`) may grant roles to
    any group.

    Args:
        body: Request body, with the role to grant (`{"role": "owner"}`).
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
    return _put_role(group, "group", body)


@log_traffic_if(_permissions_traffic_logged)
def deleteGroupRole(**kwargs: Any) -> str:
    """Revoke the roles of the group of the route on the resource.

    Callers allowed by the policies may revoke roles of any group, also of
    groups they are not members of.

    Args:
        **kwargs: Path parameters and further arguments that Connexion
            passes; unused, as resource and group are taken from the route.

    Returns:
        The group.
    """
    return _delete_roles(_get_subject(), "group")
