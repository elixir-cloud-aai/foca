"""Resource ownership: owners and other roles of subjects on resources.

Helpers on top of the resource role helpers (cf.
:py:func:`foca.security.access_control.add_resource_role`) for apps whose
resources are owned by the subject creating them, e.g., services in a registry
or tasks in a task execution service:

* The creator of a resource becomes its owner (:py:func:`add_owner`).
* Roles (`access_control.resource_roles`, by default `owner` and
  `maintainer`) are granted to and revoked from users, groups and service
  clients (:py:func:`grant_role`, :py:func:`revoke_roles`), replacing other
  roles of the subject on the resource. The last owner of a resource cannot
  be removed (:py:class:`LastOwner`).
* Listings are filtered to the resources the current subject has a role on,
  unless it is an admin (:py:func:`resource_filter`,
  :py:func:`list_subject_roles`, :py:func:`is_admin`).

Resources are identified by their full request paths (e.g., `/tasks/123`), as
roles are enforced on request paths. Role changes are logged for auditing
(who changed which role of whom on which resource). Which subjects may call
the operations using the helpers is decided by the app's policies.
"""

import logging
from collections.abc import Callable
from typing import NamedTuple

from casbin import Enforcer
from werkzeug.exceptions import Conflict, NotFound

from foca.config.access import get_config
from foca.security.access_control.constants import (
    ADMIN_ROLE,
    GROUP_PREFIX,
    OWNER_ROLE,
)
from foca.security.access_control.enforcement import (
    RESOURCE_ROLE_PTYPE,
    SubjectType,
    _get_current_policy_subjects,
    _get_resource_role_enforcer,
    _get_subject_prefix,
    _prefix_subject,
    _replace_resource_role,
    add_resource_role,
    get_current_client,
    get_current_groups,
    get_current_subject,
    get_enforcer,
    get_on_behalf_of_user,
    remove_resource_role,
)
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    validate_rule_value,
)
from foca.utils.logging import escape_log_value

logger = logging.getLogger(__name__)

# Types of subjects roles are granted to, in the order they are listed
SUBJECT_TYPES: tuple[SubjectType, ...] = ("user", "group", "client")


class ResourceRole(NamedTuple):
    """Role of a subject on a resource.

    Attributes:
        subject_type: Type of the subject: `user`, `group` or `client`.
        subject: User identifier, group claim value or client ID, without
            prefix.
        role: Role of the subject on the resource.
    """

    subject_type: SubjectType
    subject: str
    role: str


class SubjectResourceRole(NamedTuple):
    """Role of the current subject on a resource under a path prefix.

    Attributes:
        id: Identifier of the resource, i.e., its path below the prefix.
        role: Role on the resource.
        subject_type: Type of the subject the role is granted to: `user` or
            `client` for the current subject itself, `group` for one of its
            groups.
        subject: User identifier, group claim value or client ID, without
            prefix.
    """

    id: str
    role: str
    subject_type: SubjectType
    subject: str


class LastOwner(Conflict):
    """The last owner of a resource cannot be removed or downgraded."""

    description = "The last owner of a resource cannot be removed"


def _get_resource_roles() -> list[str]:
    """Get the roles managed by the helpers (`access_control.resource_roles`)."""
    return get_config().security.access_control.resource_roles


def _describe(subject_type: SubjectType, subject: str) -> str:
    """Describe a subject for log messages.

    Args:
        subject_type: Type of the subject.
        subject: Subject without prefix.

    Returns:
        Type and quoted subject, with non-printable characters escaped
        (e.g., `group 'devs'`).
    """
    return f"{subject_type} '{escape_log_value(subject)}'"


def _describe_caller() -> str:
    """Describe the subject of the current request for log messages.

    Returns:
        Service client (e.g., `client 'abc'`) or user (e.g., `user 'alice'`)
        of the current request, or `unidentified caller`.
    """
    client = get_current_client()
    if client is not None:
        return _describe("client", client)
    subject = get_current_subject()
    if subject is not None:
        return _describe("user", subject)
    return "unidentified caller"


def _get_current_subjects() -> list[tuple[SubjectType, str]]:
    """Get the subjects of the current request that roles may be granted to.

    Returns:
        Type and identifier (without prefix) of the service client or user of
        the current request, followed by its groups, if used as subjects
        (cf. :py:func:`foca.security.access_control.get_current_groups`).
        Empty if the request has neither a service client nor a user.
    """
    client = get_current_client()
    subject = get_current_subject()
    subjects: list[tuple[SubjectType, str]]
    if client is not None:
        subjects = [("client", client)]
    elif subject is not None:
        subjects = [("user", subject)]
    else:
        return []
    subjects.extend(
        ("group", group.removeprefix(GROUP_PREFIX))
        for group in get_current_groups()
        if group.startswith(GROUP_PREFIX)
    )
    return subjects


def _get_listing_subjects(
    on_behalf_of: str | None = None,
) -> list[tuple[SubjectType, str]]:
    """Get the subjects whose resources the current request lists.

    Args:
        on_behalf_of: User named by the caller to act for (cf.
            :py:func:`foca.security.access_control.get_on_behalf_of_user`).

    Returns:
        The user that an allowed service client acts for, if any, without
        groups (service tokens carry none of the user's), otherwise the
        subjects of the current request (cf. :py:func:`_get_current_subjects`).

    Raises:
        connexion.exceptions.BadRequestProblem: The named user is invalid.
        connexion.exceptions.Forbidden: The caller names a user, but is not a
            service client allowed to act for users.
    """
    user = get_on_behalf_of_user(on_behalf_of)
    if user is not None:
        return [("user", user)]
    return _get_current_subjects()


def _has_global_roles(enforcer: Enforcer) -> bool:
    """Check whether the model of an enforcer defines global roles (`g`)."""
    return "g" in enforcer.get_model().model.get("g", {})


def is_admin() -> bool:
    """Check whether the subject of the current request is an admin.

    Admins may do everything in the reference model
    (`resource_role_model.conf`), so they see all resources (cf.
    :py:func:`resource_filter`).

    Returns:
        Whether the subject of the current request has the role `admin`:
        mapped from its group claims (`access_control.group_mapping`) or
        from its service client (`access_control.client_mapping`), or
        granted via global role rules (e.g., `g, user:alice, admin`),
        also transitively. `False` if the request has neither a user nor a
        service client.
    """
    if not _get_current_subjects():
        return False
    subjects = _get_current_policy_subjects()
    if ADMIN_ROLE in subjects:
        return True
    enforcer = get_enforcer()
    if not _has_global_roles(enforcer):
        return False
    role_manager = enforcer.get_role_manager()
    return any(role_manager.has_link(subject, ADMIN_ROLE) for subject in subjects)


def _get_creator(on_behalf_of: str | None = None) -> tuple[SubjectType, str]:
    """Get the subject that a resource created by the current request belongs to.

    Args:
        on_behalf_of: User named by the caller to act for (cf.
            :py:func:`foca.security.access_control.get_on_behalf_of_user`).

    Returns:
        Type and identifier of the subject: the user that an allowed service
        client acts for, named via `on_behalf_of` or the header configured
        via `auth.on_behalf_of_header` (cf.
        :py:func:`foca.security.access_control.get_on_behalf_of_user`),
        otherwise the service client (cf.
        :py:func:`foca.security.access_control.get_current_client`),
        otherwise the user of the current request.

    Raises:
        connexion.exceptions.BadRequestProblem: The named user is invalid.
        connexion.exceptions.Forbidden: The caller names a user, but is not a
            service client allowed to act for users.
        connexion.exceptions.Unauthorized: The current request has neither a
            user nor a service client.
    """
    user = get_on_behalf_of_user(on_behalf_of)
    if user is not None:
        return "user", user
    client = get_current_client()
    if client is not None:
        return "client", client
    subject = get_current_subject()
    if subject is None:
        from foca.security.auth import _unauthorized  # noqa: PLC0415 (import cycle)

        raise _unauthorized("Subject could not be identified", invalid_token=False)
    return "user", subject


def add_owner(
    resource: str,
    subject: str | None = None,
    subject_type: SubjectType = "user",
    ptype: str = RESOURCE_ROLE_PTYPE,
    on_behalf_of: str | None = None,
) -> ResourceRole:
    """Make the creator of a resource its owner.

    Call this when creating a resource. If the resource may have been
    deleted before (e.g., if identifiers are chosen by clients), revoke the
    roles left on it first (cf.
    :py:func:`foca.security.access_control.remove_resource`).

    Args:
        resource: Resource, i.e., its full request path (e.g., `/tasks/1`).
        subject: Owner, without prefix. By default, the subject the request
            is attributed to: the user that an allowed service client acts
            for (cf.
            :py:func:`foca.security.access_control.get_on_behalf_of_user`),
            otherwise the service client (cf.
            :py:func:`foca.security.access_control.get_current_client`),
            otherwise the user of the current request. A given subject is
            not checked against the caller, so never take it from requests
            unchecked; pass users that service clients name in request
            fields via `on_behalf_of` instead.
        subject_type: Type of `subject`; ignored if `subject` is `None`.
        ptype: Policy type of resource-scoped role rules.
        on_behalf_of: User that the caller names to act for (e.g., from a
            request field), checked by
            :py:func:`foca.security.access_control.get_on_behalf_of_user`;
            if `None`, the value of the header configured via
            `auth.on_behalf_of_header`, if any. Must not be given together
            with `subject`.

    Returns:
        The granted owner role.

    Raises:
        connexion.exceptions.BadRequestProblem: The user named by a service
            client is invalid.
        connexion.exceptions.Forbidden: The caller names a user to act for,
            but is not a service client allowed to.
        connexion.exceptions.Unauthorized: `subject` is `None`, and the
            current request has neither a user nor a service client.
        ValueError: Both `subject` and `on_behalf_of` are given, the role
            would not apply to the subject (cf. :py:func:`grant_role`), or
            cannot be granted (cf.
            :py:func:`foca.security.access_control.add_resource_role`).
    """
    if subject is not None and on_behalf_of is not None:
        raise ValueError("Pass either 'subject' or 'on_behalf_of', not both")
    if subject is None:
        subject_type, subject = _get_creator(on_behalf_of)
    _check_applicable(subject, subject_type)
    add_resource_role(
        subject=subject,
        role=OWNER_ROLE,
        resource=resource,
        ptype=ptype,
        subject_type=subject_type,
    )
    logger.info(
        "Role '%s' on '%s' granted to %s by %s",
        OWNER_ROLE,
        escape_log_value(resource),
        _describe(subject_type, subject),
        _describe_caller(),
    )
    return ResourceRole(subject_type, subject, OWNER_ROLE)


def list_roles(
    resource: str,
    role: str | None = None,
    ptype: str = RESOURCE_ROLE_PTYPE,
) -> list[ResourceRole]:
    """List the roles granted on a resource.

    Args:
        resource: Resource, i.e., its full request path.
        role: Role to list; all roles if `None`.
        ptype: Policy type of resource-scoped role rules.

    Returns:
        Roles granted to users, followed by those granted to groups and to
        service clients, each in the order they were granted, without
        duplicates. Rules of other subjects (e.g., roles) are not listed.

    Raises:
        ValueError: The model does not define resource-scoped roles.
    """
    # Policies are loaded once; prefixes of subject types do not overlap
    rules = _get_resource_role_enforcer(ptype).get_filtered_named_grouping_policy(
        ptype, 2, resource
    )
    roles: list[ResourceRole] = []
    for subject_type in SUBJECT_TYPES:
        prefix = _get_subject_prefix(subject_type)
        roles.extend(
            ResourceRole(subject_type, subject.removeprefix(prefix), granted_role)
            for subject, granted_role, _ in rules
            if subject.startswith(prefix) and role in (None, granted_role)
        )
    return list(dict.fromkeys(roles))


def _get_owners(resource: str, ptype: str) -> set[tuple[SubjectType, str]]:
    """Get type and identifier of each owner of a resource whose role applies.

    Owners whose role does not apply (cf. :py:func:`_check_applicable`),
    e.g., groups granted the role before `access_control.keep_group_subjects`
    was unset, do not count, so that the last owner whose role applies is
    kept.
    """
    return {
        (owner.subject_type, owner.subject)
        for owner in list_roles(resource, role=OWNER_ROLE, ptype=ptype)
        if _applies(owner.subject, owner.subject_type)
    }


def _applies(subject: str, subject_type: SubjectType) -> bool:
    """Check whether roles granted to a subject apply to requests."""
    try:
        _check_applicable(subject, subject_type)
    except ValueError:
        return False
    return True


def _get_roles_of(
    resource: str, subject: str, subject_type: SubjectType, ptype: str
) -> list[str]:
    """Get the roles of a subject on a resource managed by the helpers."""
    roles = _get_resource_roles()
    return [
        granted.role
        for granted in list_roles(resource, ptype=ptype)
        if (granted.subject_type, granted.subject) == (subject_type, subject)
        and granted.role in roles
    ]


def _check_applicable(subject: str, subject_type: SubjectType) -> None:
    """Check that roles granted to a subject apply to requests.

    Args:
        subject: Subject without prefix.
        subject_type: Type of the subject.

    Raises:
        ValueError: The subject is a group, but group claim values are not
            used as subjects (`access_control.group_mapping` is set, and
            `access_control.keep_group_subjects` is not), or a client that
            is not a service client (`auth.service_clients`), so that the
            role would not apply, and the resource could be left without an
            owner that applies.
    """
    conf = get_config().security
    if (
        subject_type == "group"
        and conf.access_control.group_mapping is not None
        and not conf.access_control.keep_group_subjects
    ):
        raise ValueError(
            "Roles granted to groups do not apply: group claim values are not "
            "used as subjects with 'access_control.group_mapping', unless "
            "'access_control.keep_group_subjects' is set"
        )
    if subject_type == "client" and subject not in {
        client.client_id for client in conf.auth.service_clients
    }:
        raise ValueError(
            f"Roles granted to client '{subject}' do not apply: not a service "
            "client ('auth.service_clients')"
        )


def _check_other_owners(
    resource: str, subject: str, subject_type: SubjectType, ptype: str
) -> None:
    """Check that a resource has owners other than a subject.

    Args:
        resource: Resource, i.e., its full request path.
        subject: Subject without prefix.
        subject_type: Type of the subject.
        ptype: Policy type of resource-scoped role rules.

    Raises:
        LastOwner: The subject is the only owner of the resource.
    """
    if _get_owners(resource, ptype) == {(subject_type, subject)}:
        logger.info(
            "Last owner of '%s': %s",
            escape_log_value(resource),
            _describe(subject_type, subject),
        )
        raise LastOwner


def _grant(
    resource: str,
    subject: str,
    role: str,
    subject_type: SubjectType,
    ptype: str,
    exists: Callable[[], bool] | None,
    revoke_on_error: bool = True,
) -> None:
    """Grant a subject a role on a resource, unless it was deleted.

    The role replaces the subject's other roles of
    `access_control.resource_roles` on the resource (cf.
    :py:func:`grant_role`).

    Checking that a resource exists before granting a role is not atomic:
    the resource may be deleted in between (after its roles were revoked),
    which would leave the role on the deleted resource. So existence is
    checked again afterwards, and if the resource is gone, the role is
    revoked again.

    Args:
        resource: Resource, i.e., its full request path.
        subject: Subject without prefix.
        role: Role to grant.
        subject_type: Type of the subject.
        ptype: Policy type of resource-scoped role rules.
        exists: Function checking whether the resource exists; not checked
            if `None`.
        revoke_on_error: Whether to revoke the role again if `exists`
            raises. Restored owners are kept, so that a failing check does
            not leave the resource without owners.

    Raises:
        werkzeug.exceptions.NotFound: The resource was deleted meanwhile.
        Exception: Any exception raised by `exists`; the role is revoked
            again if `revoke_on_error` is set and the role was not granted
            before.
    """
    added = _replace_resource_role(
        subject=subject,
        role=role,
        resource=resource,
        roles=_get_resource_roles(),
        ptype=ptype,
        subject_type=subject_type,
    )
    if exists is None:
        return
    resource_exists = not (revoke_on_error and added)
    try:
        resource_exists = exists()
    # The role is revoked again if the resource is gone, or if the check
    # fails, unless `revoke_on_error` is unset or the role was granted
    # before
    finally:
        if not resource_exists:
            remove_resource_role(
                subject=subject,
                role=role,
                resource=resource,
                ptype=ptype,
                subject_type=subject_type,
            )
    if not resource_exists:
        raise NotFound


def _restore_owner_if_none(
    resource: str,
    subject: str,
    subject_type: SubjectType,
    ptype: str,
    exists: Callable[[], bool] | None,
) -> None:
    """Make a former owner owner again if a resource has no owners left.

    Checking for other owners before changing roles is not atomic:
    concurrent requests of owners removing each other could leave a
    resource without owners. Each request therefore checks again afterwards
    and, if no owner is left, restores the owner role it removed.

    Args:
        resource: Resource, i.e., its full request path.
        subject: Subject without prefix whose owner role was removed.
        subject_type: Type of the subject.
        ptype: Policy type of resource-scoped role rules.
        exists: Function checking whether the resource exists (cf.
            :py:func:`_grant`).

    Raises:
        LastOwner: The resource had no owners left.
        werkzeug.exceptions.NotFound: The resource was deleted meanwhile.
    """
    if _get_owners(resource, ptype):
        return
    _grant(
        resource,
        subject,
        OWNER_ROLE,
        subject_type,
        ptype,
        exists,
        revoke_on_error=False,
    )
    logger.info(
        "Restored owner %s of '%s', removed concurrently, for %s",
        _describe(subject_type, subject),
        escape_log_value(resource),
        _describe_caller(),
    )
    raise LastOwner


def grant_role(
    resource: str,
    subject: str,
    role: str,
    subject_type: SubjectType = "user",
    ptype: str = RESOURCE_ROLE_PTYPE,
    exists: Callable[[], bool] | None = None,
) -> list[str]:
    """Grant a subject a role on a resource, replacing its other roles.

    The other roles of `access_control.resource_roles` of the subject on the
    resource are revoked once the role is granted, so that the subject keeps
    a role throughout (e.g., a maintainer promoted to owner). Concurrent
    grants for the same subject and resource leave it with one of these
    roles, that of the grant stored last. If revoking the other roles fails,
    the granted role is revoked again, so that a failure leaves the subject
    with fewer rights, not more. Other roles (e.g., granted via
    :py:func:`foca.security.access_control.add_resource_role`) and roles set
    via the app's configuration (`access_control.policies`, with a warning)
    are kept. Which subjects may grant which roles to which subjects (e.g.,
    only to groups they are members of) is up to the app.

    Args:
        resource: Resource, i.e., its full request path.
        subject: Subject to grant the role to, without prefix: a user
            identifier, a group claim value or a client ID.
        role: Role to grant; one of `access_control.resource_roles`.
        subject_type: Type of the subject: `user`, `group` or `client`.
        ptype: Policy type of resource-scoped role rules.
        exists: Function checking whether the resource exists, called after
            the role was granted, so that roles granted while the resource
            was deleted are revoked again. Not checked if `None`.

    Returns:
        Previous roles of `access_control.resource_roles` of the subject on
        the resource.

    Raises:
        LastOwner: The subject is the last owner of the resource, and the
            role is not `owner`.
        werkzeug.exceptions.NotFound: `exists` returned `False` after the
            role was granted; the role was revoked again.
        ValueError: The role is not one of `access_control.resource_roles`,
            the role is not `owner` and the resource has no owner (whose
            role applies), the subject is empty, the role would not apply to the subject
            (a group while group claim values are not used as subjects, or a
            client that is not a service client), or the role cannot be
            granted (cf.
            :py:func:`foca.security.access_control.add_resource_role`).
    """
    roles = _get_resource_roles()
    if role not in roles:
        raise ValueError(
            f"Role '{role}' is not one of the roles in 'resource_roles': "
            f"{', '.join(roles)}"
        )
    if not subject:
        raise ValueError("Subjects must not be empty")
    _check_applicable(subject, subject_type)
    previous = _get_roles_of(resource, subject, subject_type, ptype)
    if role != OWNER_ROLE:
        if not _get_owners(resource, ptype):
            raise ValueError(
                f"Resource '{resource}' has no owner whose role applies: grant "
                f"'{OWNER_ROLE}' first"
            )
        _check_other_owners(resource, subject, subject_type, ptype)
    _grant(resource, subject, role, subject_type, ptype, exists)
    if OWNER_ROLE in previous and role != OWNER_ROLE:
        _restore_owner_if_none(resource, subject, subject_type, ptype, exists)
    logger.info(
        "Role '%s' on '%s' granted to %s by %s (previous roles: %s)",
        escape_log_value(role),
        escape_log_value(resource),
        _describe(subject_type, subject),
        _describe_caller(),
        ", ".join(previous) or "none",
    )
    return previous


def revoke_roles(
    resource: str,
    subject: str,
    subject_type: SubjectType = "user",
    ptype: str = RESOURCE_ROLE_PTYPE,
    exists: Callable[[], bool] | None = None,
) -> bool:
    """Revoke the roles of a subject on a resource.

    Only roles of `access_control.resource_roles` are revoked. Unlike
    :py:func:`grant_role`, subjects that cannot have roles (e.g., invalid
    values of policy rules) are accepted, as they have nothing to revoke.

    Args:
        resource: Resource, i.e., its full request path.
        subject: Subject to revoke the roles of, without prefix.
        subject_type: Type of the subject: `user`, `group` or `client`.
        ptype: Policy type of resource-scoped role rules.
        exists: Function checking whether the resource exists, called if an
            owner role removed concurrently is restored (cf.
            :py:func:`grant_role`). Not checked if `None`.

    Returns:
        `True` if any role was revoked, `False` if the subject had none
        (also if it is not a valid value of policy rules).

    Raises:
        LastOwner: The subject is the last owner of the resource.
        werkzeug.exceptions.NotFound: `exists` returned `False` when an
            owner role was restored.
        ValueError: The model does not define resource-scoped roles, the
            subject type is not supported, or the resource is not a valid
            value of policy rules.
    """
    # Values that cannot be stored in rules have no roles
    if not _is_valid_subject(subject, subject_type):
        return False
    _check_other_owners(resource, subject, subject_type, ptype)
    previous = _get_roles_of(resource, subject, subject_type, ptype)
    revoked = False
    for role in _get_resource_roles():
        revoked |= remove_resource_role(
            subject=subject,
            role=role,
            resource=resource,
            ptype=ptype,
            subject_type=subject_type,
        )
    if not revoked:
        return False
    if OWNER_ROLE in previous:
        _restore_owner_if_none(resource, subject, subject_type, ptype, exists)
    logger.info(
        "Roles on '%s' of %s revoked by %s (previous roles: %s)",
        escape_log_value(resource),
        _describe(subject_type, subject),
        _describe_caller(),
        ", ".join(previous),
    )
    return True


def _is_valid_subject(subject: str, subject_type: SubjectType) -> bool:
    """Check whether a prefixed subject is a valid value of policy rules."""
    prefixed = _prefix_subject(subject, subject_type)
    try:
        validate_rule_value(prefixed)
    except ValueError:
        return False
    return True


def list_subject_roles(
    prefix: str,
    ptype: str = RESOURCE_ROLE_PTYPE,
    on_behalf_of: str | None = None,
) -> list[SubjectResourceRole]:
    """List the roles of the current subject on resources under a path prefix.

    Use this, e.g., for an endpoint telling users which resources they have
    roles on. Only resources directly under the prefix are included (e.g.,
    `/tasks/1` for `/tasks`, but not `/tasks/1/logs`).

    Args:
        prefix: Path prefix of the resources, i.e., the full request path of
            their collection (e.g., `/tasks`).
        ptype: Policy type of resource-scoped role rules.
        on_behalf_of: User that the caller names to act for (cf.
            :py:func:`add_owner`); if `None`, the value of the header
            configured via `auth.on_behalf_of_header`, if any.

    Returns:
        Roles granted to the service client or user of the current request
        and to its groups (if used as subjects, cf.
        :py:func:`foca.security.access_control.get_current_groups`). If an
        allowed service client acts for a user (cf.
        :py:func:`foca.security.access_control.get_on_behalf_of_user`), the
        roles granted to that user instead, without groups, as service
        tokens carry none of the user's. Sorted by resource identifier, then
        by subject type (user or client first, then groups) and subject.
        Empty if the request has neither a user nor a service client.

    Raises:
        connexion.exceptions.BadRequestProblem: The user named by a service
            client is invalid.
        connexion.exceptions.Forbidden: The caller names a user to act for,
            but is not a service client allowed to.
        ValueError: The model does not define resource-scoped roles.
    """
    return _list_roles_of(_get_listing_subjects(on_behalf_of), prefix, ptype)


def _list_roles_of(
    subjects: list[tuple[SubjectType, str]], prefix: str, ptype: str
) -> list[SubjectResourceRole]:
    """List the roles of subjects on resources under a path prefix.

    Cf. :py:func:`list_subject_roles`.
    """
    enforcer = _get_resource_role_enforcer(ptype)
    base = f"{prefix.rstrip('/')}/"
    roles: list[SubjectResourceRole] = []
    for subject_type, subject in subjects:
        for _, role, resource in enforcer.get_filtered_named_grouping_policy(
            ptype, 0, _prefix_subject(subject, subject_type)
        ):
            resource_id = resource.removeprefix(base)
            if resource.startswith(base) and resource_id and "/" not in resource_id:
                roles.append(
                    SubjectResourceRole(resource_id, role, subject_type, subject)
                )
    return sorted(
        dict.fromkeys(roles),
        key=lambda role: (
            role.id,
            role.subject_type == "group",
            role.subject,
            role.role,
        ),
    )


def resource_filter(
    prefix: str,
    field: str = "id",
    ptype: str = RESOURCE_ROLE_PTYPE,
    on_behalf_of: str | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Get a MongoDB filter for the resources the current subject may see.

    Use this to filter listings of resources, e.g., so that users only list
    their own tasks: `collection.find(resource_filter("/tasks"))`. The filter
    only selects by `field`, so `field` must identify the resources under
    `prefix` uniquely among the queried documents. If a collection holds
    resources under several prefixes (e.g., files of several projects at
    `/projects/{id}/files`), combine the filter with one for the scope
    (e.g., `{"project": "x", **resource_filter("/projects/x/files")}`),
    also for admins, who get an empty filter.

    Args:
        prefix: Path prefix of the resources (cf.
            :py:func:`list_subject_roles`).
        field: Field of the documents holding the resource identifiers.
        ptype: Policy type of resource-scoped role rules.
        on_behalf_of: User that the caller names to act for (cf.
            :py:func:`list_subject_roles`).

    Returns:
        An empty filter (all resources) if the caller is an admin (cf.
        :py:func:`is_admin`); for service clients, this is the client's own
        status, also when it acts for a user, whose admin rights do not
        apply. Otherwise, a filter for the resources listed by
        :py:func:`list_subject_roles`.

    Raises:
        connexion.exceptions.BadRequestProblem: The user named by a service
            client is invalid.
        connexion.exceptions.Forbidden: The caller names a user to act for,
            but is not a service client allowed to.
        ValueError: The model does not define resource-scoped roles.
    """
    # Named users are checked also for admins, so that callers notice
    subjects = _get_listing_subjects(on_behalf_of)
    if is_admin():
        return {}
    ids = [role.id for role in _list_roles_of(subjects, prefix, ptype)]
    return {field: {"$in": list(dict.fromkeys(ids))}}
