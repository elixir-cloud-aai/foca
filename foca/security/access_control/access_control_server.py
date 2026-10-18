"""Controllers for permission management endpoints."""

import logging

from casbin.model import Model
from flask import current_app, request
from pymongo.collection import Collection
from werkzeug.exceptions import InternalServerError, NotFound

from foca.errors.exceptions import BadRequest
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    MAX_RULE_VALUES,
    CasbinRule,
    validate_rule,
)
from foca.utils.logging import log_traffic

logger = logging.getLogger(__name__)


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
        `id`, `ptype` and `v0` to `v5` of the stored rule only.
    """
    return {
        "id": document.get("id"),
        "policy_type": document.get("ptype"),
        "rule": {
            f"v{index}": document[f"v{index}"]
            for index in range(MAX_RULE_VALUES)
            if document.get(f"v{index}") is not None
        },
    }


@log_traffic
def postPermission() -> str:
    """Method to register new permissions.

    Returns:
        Identifier of the new permission added.
    """
    ptype, values = _get_rule()
    try:
        access_control_adapter = current_app.config["casbin_adapter"]
        permission_id = access_control_adapter.save_policy_line(
            ptype=ptype,
            rule=values,
        )
        logger.info("New policy added.")
        return permission_id
    except Exception as e:
        logger.error(f"{type(e).__name__}: {e}")
        raise InternalServerError from e


@log_traffic
def putPermission(
    id: str,
) -> str:
    """Update permission with a user-supplied ID.

    Args:
        id: Identifier of permission to be updated.

    Returns:
        Identifier of updated permission.
    """
    ptype, values = _get_rule()
    app_config = current_app.config
    try:
        security_conf = app_config.foca.security  # type: ignore[attr-defined]
        access_control_config = security_conf.access_control  # type: ignore[attr-defined]
        db_coll_permission: Collection = (
            app_config.foca.db.dbs[  # type: ignore[attr-defined]
                access_control_config.db_name
            ]
            .collections[access_control_config.collection_name]
            .client
        )

        permission_data = CasbinRule(ptype, *values).dict()
        permission_data["id"] = id
        db_coll_permission.replace_one(
            filter={"id": id}, replacement=permission_data, upsert=True
        )
        logger.info("Policy updated.")
        return id
    except Exception as e:
        logger.error(f"{type(e).__name__}: {e}")
        raise InternalServerError from e


@log_traffic
def getAllPermissions(limit=None) -> list[dict]:
    """Method to fetch all permissions.

    Args:
        limit: Number of objects requested.

    Returns:
        List of permission dicts.
    """
    app_config = current_app.config
    access_control_config = app_config.foca.security.access_control  # type: ignore[attr-defined]
    db_coll_permission: Collection = (
        app_config.foca.db.dbs[  # type: ignore[attr-defined]
            access_control_config.db_name
        ]
        .collections[access_control_config.collection_name]
        .client
    )

    if not limit:
        limit = 0
    permissions = list(
        db_coll_permission.find(filter={}, projection={"_id": False})
        .sort([("$natural", -1)])
        .limit(limit)
    )
    return [_to_permission(permission) for permission in permissions]


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
    app_config = current_app.config
    access_control_config = app_config.foca.security.access_control  # type: ignore[attr-defined]
    db_coll_permission: Collection = (
        app_config.foca.db.dbs[  # type: ignore[attr-defined]
            access_control_config.db_name
        ]
        .collections[access_control_config.collection_name]
        .client
    )

    permission = db_coll_permission.find_one(filter={"id": id})
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
    app_config = current_app.config
    access_control_config = app_config.foca.security.access_control  # type: ignore[attr-defined]
    db_coll_permission: Collection = (
        app_config.foca.db.dbs[  # type: ignore[attr-defined]
            access_control_config.db_name
        ]
        .collections[access_control_config.collection_name]
        .client
    )

    del_obj_permission = db_coll_permission.delete_one({"id": id})

    if del_obj_permission.deleted_count:
        return id
    else:
        raise NotFound
