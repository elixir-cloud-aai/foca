""""Controllers for permission management endpoints."""

import logging

from typing import (Dict, List)

from flask import (request, current_app)
from pymongo.collection import Collection
from werkzeug.exceptions import (InternalServerError, NotFound)

from foca.utils.logging import log_traffic
from foca.errors.exceptions import BadRequest

logger = logging.getLogger(__name__)


@log_traffic
def postPermission() -> str:
    """Method to register new permissions.

    Returns:
        Identifier of the new permission added.
    """
    request_json = request.json
    if isinstance(request_json, dict):
        try:
            access_control_adapter = current_app.config["casbin_adapter"]
            rule = request_json.get("rule", {})
            permission_data = [
                rule.get("v0", None),
                rule.get("v1", None),
                rule.get("v2", None),
                rule.get("v3", None),
                rule.get("v4", None),
                rule.get("v5", None)
            ]
            permission_id = access_control_adapter.save_policy_line(
                ptype=request_json.get("policy_type", None),
                rule=permission_data
            )
            logger.info("New policy added.")
            return permission_id
        except Exception as e:
            logger.error(f"{type(e).__name__}: {e}")
            raise InternalServerError
    else:
        logger.error("Invalid request payload.")
        raise BadRequest


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
    request_json = request.json
    if isinstance(request_json, dict):
        app_config = current_app.config
        try:
            security_conf = \
                app_config.foca.security  # type: ignore[attr-defined]
            access_control_config = \
                security_conf.access_control  # type: ignore[attr-defined]
            db_coll_permission: Collection = (
                app_config.foca.db.dbs[  # type: ignore[attr-defined]
                    access_control_config.db_name]
                .collections[access_control_config.collection_name].client
            )

            permission_data = request_json.get("rule", {})
            permission_data["id"] = id
            permission_data["ptype"] = request_json.get("policy_type", None)
            db_coll_permission.replace_one(
                filter={"id": id},
                replacement=permission_data,
                upsert=True
            )
            logger.info("Policy updated.")
            return id
        except Exception as e:
            logger.error(f"{type(e).__name__}: {e}")
            raise InternalServerError
    else:
        logger.error("Invalid request payload.")
        raise BadRequest


@log_traffic
def getAllPermissions(limit=None) -> List[Dict]:
    """Method to fetch all permissions.

    Args:
        limit: Number of objects requested.

    Returns:
        List of permission dicts.
    """
    app_config = current_app.config
    access_control_config = \
        app_config.foca.security.access_control  # type: ignore[attr-defined]
    db_coll_permission: Collection = (
        app_config.foca.db.dbs[  # type: ignore[attr-defined]
            access_control_config.db_name
        ].collections[access_control_config.collection_name].client
    )

    if not limit:
        limit = 0
    permissions = db_coll_permission.find(
        filter={},
        projection={'_id': False}
    ).sort([('$natural', -1)]).limit(limit)
    permissions = list(permissions)
    user_permission_list = []
    for _permission in permissions:
        policy_type = _permission.get("ptype", None)
        id = _permission.get("id", None)
        del _permission["ptype"]
        del _permission["id"]
        rule = _permission
        user_permission_list.append({
            "policy_type": policy_type,
            "rule": rule,
            "id": id
        })
    return user_permission_list


@log_traffic
def getPermission(
    id: str,
) -> Dict:
    """Method to fetch a particular permission.

    Args:
        id: Permission identifier.

    Returns:
        Permission data for the given id.
    """
    app_config = current_app.config
    access_control_config = \
        app_config.foca.security.access_control  # type: ignore[attr-defined]
    db_coll_permission: Collection = (
        app_config.foca.db.dbs[  # type: ignore[attr-defined]
            access_control_config.db_name
        ].collections[access_control_config.collection_name].client
    )

    permission = db_coll_permission.find_one(filter={"id": id})
    if permission is None:
        raise NotFound
    del permission["_id"]
    policy_type = permission.get("ptype", None)
    id = permission.get("id", None)
    del permission["ptype"]
    del permission["id"]
    return {
        "id": id,
        "rule": permission,
        "policy_type": policy_type
    }


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
    access_control_config = \
        app_config.foca.security.access_control  # type: ignore[attr-defined]
    db_coll_permission: Collection = (
        app_config.foca.db.dbs[  # type: ignore[attr-defined]
            access_control_config.db_name
        ].collections[access_control_config.collection_name].client
    )

    del_obj_permission = db_coll_permission.delete_one({'id': id})

    if del_obj_permission.deleted_count:
        return id
    else:
        raise NotFound
