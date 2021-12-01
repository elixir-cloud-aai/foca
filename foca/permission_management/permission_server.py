""""Controllers for permission management endpoints."""

import logging

from typing import Dict, List
from bson.objectid import ObjectId

from flask import request, current_app

from foca.utils.logging import log_traffic
from foca.permission_management.constants import PERMISSION_DB_COLLECTION_NAME, PERMISSION_DB_NAME
from werkzeug.exceptions import InternalServerError, NotFound

logger = logging.getLogger(__name__)


@log_traffic
def postPermission() -> str:
    """Method to register new permissions.

    Returns:
        Identifier of the new permission added.
    """
    try:
        request_json = request.json
        db_coll_permission = (
            current_app.config['FOCA'].db.dbs[PERMISSION_DB_NAME]
            .collections[PERMISSION_DB_COLLECTION_NAME].client
        )
        
        rule = request_json.get("rule", {})
        permission_data = {
            "ptype": request_json.get("ptype", None) if request_json.get("ptype", None) else None,
            "v0": rule.get("v0", None) if rule.get("v0", None) else None,
            "v1": rule.get("v1", None) if rule.get("v1", None) else None,
            "v2": rule.get("v2", None) if rule.get("v2", None) else None,
            "v3": rule.get("v3", None) if rule.get("v3", None) else None,
            "v4": rule.get("v4", None) if rule.get("v4", None) else None,
            "v5": rule.get("v5", None) if rule.get("v5", None) else None
        }
        permission = db_coll_permission.insert_one(document=permission_data)
        logger.info("Policy added.")
        return str(permission.inserted_id)
    except Exception as e:
        logger.error(f"{type(e).__name__}: {e}")
        raise InternalServerError


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
    try:
        request_json = request.json
        db_coll_permission = (
            current_app.config['FOCA'].db.dbs[PERMISSION_DB_NAME]
            .collections[PERMISSION_DB_COLLECTION_NAME].client
        )
        
        rule = request_json.get("rule", {})
        permission_data = {
            "ptype": request_json.get("ptype", None) if request_json.get("ptype", None) else None,
            "v0": rule.get("v0", None) if rule.get("v0", None) else None,
            "v1": rule.get("v1", None) if rule.get("v1", None) else None,
            "v2": rule.get("v2", None) if rule.get("v2", None) else None,
            "v3": rule.get("v3", None) if rule.get("v3", None) else None,
            "v4": rule.get("v4", None) if rule.get("v4", None) else None,
            "v5": rule.get("v5", None) if rule.get("v5", None) else None
        }
        db_coll_permission.replace_one(
            filter={"_id": ObjectId(id)},
            replacement=permission_data
        )
        logger.info("Policy updated.")
        return id
    except Exception as e:
        logger.error(f"{type(e).__name__}: {e}")
        raise InternalServerError


@log_traffic
def getAllPermissions(limit=None) -> List[Dict]:
    """Method to fetch all permissions.

    Args:
        limit: Number of objects requested.

    Returns:
        List of permission dicts.
    """
    db_coll_permission = (
        current_app.config['FOCA'].db.dbs[PERMISSION_DB_NAME]
        .collections[PERMISSION_DB_COLLECTION_NAME].client
    )
    if not limit:
        limit = 0
    permissions = db_coll_permission.find(
        filter={},
        projection={'_id': False}
    ).sort([('$natural', -1)]).limit(limit)
    return list(permissions)


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
    db_coll_permission = (
        current_app.config['FOCA'].db.dbs[PERMISSION_DB_NAME]
        .collections[PERMISSION_DB_COLLECTION_NAME].client
    )
    permission = db_coll_permission.find_one(
        filter={"_id": ObjectId(id)},
        projection={'_id': False}
    )
    logger.info(f"permission {permission}")
    try:
        return permission
    except (KeyError, TypeError):
        raise NotFound


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
    db_coll_permission = (
        current_app.config['FOCA'].db.dbs[PERMISSION_DB_NAME]
        .collections[PERMISSION_DB_COLLECTION_NAME].client
    )
    del_obj_permission = db_coll_permission.delete_one({'_id': ObjectId(id)})

    if del_obj_permission.deleted_count:
        return id
    else:
        raise NotFound