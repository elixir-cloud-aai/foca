"""Controllers for permission management endpoints."""

import logging
from math import ceil

from casbin.model import Model
from flask import current_app, request
from pymongo.collection import Collection
from werkzeug.exceptions import BadRequest, NotFound

from foca.security.access_control.constants import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
)
from foca.security.access_control.enforcement import (
    _prefix_subject,
    get_current_groups,
    get_current_subject,
    is_permitted,
)
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    MAX_RULE_VALUES,
    validate_rule,
)
from foca.utils.logging import log_traffic

logger = logging.getLogger(__name__)

# Filter for stored policy rules that can be returned as permissions
_PERMISSION_FILTER = {"ptype": {"$type": "string"}}


def _get_permissions_collection() -> Collection:
    """Get collection storing policy rules of the current app.

    Returns:
        Policy rule collection, as registered in the app's database
        configuration.
    """
    conf = current_app.config.foca  # type: ignore[attr-defined]
    access_control = conf.security.access_control
    return (
        conf.db.dbs[access_control.db_name]
        .collections[access_control.collection_name]
        .client
    )


def _get_visibility_filter() -> dict:
    """Get filter for the permissions visible to the current subject.

    Returns:
        Empty filter if the subject may access all permissions, i.e., if
        `access_control.self_service` is disabled (then, the permissions API
        is only accessible to subjects allowed by the policies) or if the
        policies allow the subject to access the requested resource.
        Otherwise, a filter for the rules that apply to the subject: rules
        whose subject (`v0`) is the prefixed subject, one of its roles from
        group claims, or `*`.

    Raises:
        connexion.exceptions.Unauthorized: The subject could not be
            identified.
    """
    conf = current_app.config.foca.security.access_control  # type: ignore[attr-defined]
    if not conf.self_service or is_permitted():
        return {}
    subject = get_current_subject()
    subjects = [_prefix_subject(str(subject)), *get_current_groups(), "*"]
    return {"v0": {"$in": subjects}}


def _get_rule() -> tuple[str, list[str]]:
    """Get and validate policy rule from request payload.

    Returns:
        Policy type and values of the rule.

    Raises:
        foca.errors.exceptions.BadRequest: The payload is not a valid rule
            for the app's access control model (cf.
            :py:func:`validate_rule`), e.g., because the policy type is not
            defined in the model, the rule has keys other than `v0` to `v5`
            or values are missing.
    """
    request_json = request.get_json(silent=True)
    if not isinstance(request_json, dict) or not isinstance(
        request_json.get("rule"), dict
    ):
        logger.error("Invalid request payload.")
        raise BadRequest
    ptype = request_json.get("policy_type")
    rule = request_json["rule"]
    keys = [f"v{index}" for index in range(MAX_RULE_VALUES)][: len(rule)]
    try:
        if set(rule) != set(keys):
            raise ValueError(
                "Rule keys must be consecutive, starting from 'v0', up to "
                f"'v{MAX_RULE_VALUES - 1}'"
            )
        model = Model()
        model.load_model(current_app.config["CASBIN_MODEL"])
        values = validate_rule(ptype, [rule[key] for key in keys], model)
    except ValueError as exc:
        logger.error(f"Invalid rule: {exc}")
        raise BadRequest from exc
    return str(ptype), values


def _to_permission(document: dict) -> dict:
    """Convert stored policy rule to permission.

    Args:
        document: Stored policy rule.

    Returns:
        Permission with identifier, policy type and rule, built from fields
        `id`, `ptype` and `v0` to `v5` of the stored rule only; the
        identifier is omitted if the rule has none (e.g., if it was added to
        the database directly), and values that are not strings are omitted.
    """
    permission = {
        "policy_type": document["ptype"],
        "rule": {
            f"v{index}": document[f"v{index}"]
            for index in range(MAX_RULE_VALUES)
            if isinstance(document.get(f"v{index}"), str)
        },
    }
    if isinstance(document.get("id"), str):
        permission["id"] = document["id"]
    return permission


@log_traffic
def postPermission() -> str:
    """Method to register new permissions.

    Returns:
        Identifier of the new permission added.
    """
    ptype, values = _get_rule()
    permission_id = current_app.config["casbin_adapter"].save_policy_line(
        ptype=ptype,
        rule=values,
    )
    logger.info(f"Policy rule '{permission_id}' added.")
    return permission_id


@log_traffic
def putPermission(
    id: str,
) -> str:
    """Update permission with a user-supplied ID.

    Args:
        id: Identifier of permission to be updated.

    Returns:
        Identifier of updated permission.

    Raises:
        foca.errors.exceptions.BadRequest: The identifier does not have the
            length and characters configured via `access_control.id_length`
            and `access_control.id_charset`, or the rule is invalid.
    """
    conf = current_app.config.foca.security.access_control  # type: ignore[attr-defined]
    if len(id) != conf.id_length or not set(id) <= set(conf.id_charset):
        logger.error(f"Invalid policy rule identifier: '{id}'")
        raise BadRequest
    ptype, values = _get_rule()
    current_app.config["casbin_adapter"].save_policy_line(
        ptype=ptype,
        rule=values,
        id=id,
    )
    logger.info(f"Policy rule '{id}' updated.")
    return id


@log_traffic
def getAllPermissions(
    page_size: int = DEFAULT_PAGE_SIZE,
    page: int = 0,
) -> dict:
    """Method to fetch permissions page by page.

    Permissions are returned in the order they were stored, as recommended
    by the GA4GH API pagination guide (page-based pagination).

    Args:
        page_size: Maximum number of permissions per page (1 to
            `MAX_PAGE_SIZE`).
        page: Page to return, starting at 0.

    Returns:
        Page of permissions (`results`) and pagination metadata
        (`pagination`).

    Raises:
        foca.errors.exceptions.BadRequest: The page size is out of range, or
            the page does not exist (except page 0 if there are no
            permissions).
    """
    if not 1 <= page_size <= MAX_PAGE_SIZE or page < 0:
        raise BadRequest
    collection = _get_permissions_collection()
    query = {**_PERMISSION_FILTER, **_get_visibility_filter()}
    total = collection.count_documents(query)
    total_pages = ceil(total / page_size)
    if page >= max(total_pages, 1):
        logger.error(f"Page {page} out of range ({total_pages} pages)")
        raise BadRequest
    permissions = (
        collection.find(filter=query, projection={"_id": False})
        .sort([("_id", 1)])
        .skip(page * page_size)
        .limit(page_size)
    )
    return {
        "results": [_to_permission(permission) for permission in permissions],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    }


@log_traffic
def getPermission(
    id: str,
) -> dict:
    """Method to fetch a particular permission.

    Args:
        id: Permission identifier.

    Returns:
        Permission data for the given id.
    """
    permission = _get_permissions_collection().find_one(
        filter={"id": id, **_PERMISSION_FILTER, **_get_visibility_filter()}
    )
    if permission is None:
        raise NotFound
    return _to_permission(permission)


@log_traffic
def deletePermission(
    id: str,
) -> str:
    """Method to delete a particular permission.

    Args:
        id: Permission identifier.

    Returns:
        Delete permission identifier.
    """
    if not _get_permissions_collection().delete_one({"id": id}).deleted_count:
        raise NotFound
    logger.info(f"Policy rule '{id}' deleted.")
    return id
