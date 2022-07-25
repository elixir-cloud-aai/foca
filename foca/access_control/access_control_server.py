""""Controllers for permission management endpoints."""

import logging

from typing import (Dict, List)

from bson.objectid import ObjectId
from flask import (request, current_app)

from foca.utils.logging import log_traffic
from foca.access_control.foca_casbin_adapter.adapter import Adapter
from werkzeug.exceptions import (InternalServerError, NotFound)

logger = logging.getLogger(__name__)


@log_traffic
def postPermission() -> str:
    """Method to register new permissions.

    Returns:
        Identifier of the new permission added.
    """
    try:
        request_json = request.json

        access_control_config = current_app.config['FOCA'].access_control
        mongo_config = current_app.config['FOCA'].db

        access_control_adapter = Adapter(
            uri=f"mongodb://{mongo_config.host}:{mongo_config.port}/",
            dbname=access_control_config.db_name,
            collection=access_control_config.collection_name
        )

        rule = request_json.get("rule", {})
        permission_data = [
            rule.get("v0", None),
            rule.get("v1", None),
            rule.get("v2", None),
            rule.get("v3", None),
            rule.get("v4", None),
            rule.get("v5", None)
        ]
        access_control_adapter.save_policy_line(
            ptype=request_json.get("policy_type", None),
            rule=permission_data
        )
        logger.info("New policy added.")
        return "added"
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
        access_control_config = current_app.config['FOCA'].access_control
        db_coll_permission = (
            current_app.config['FOCA'].db.dbs[access_control_config.db_name]
            .collections[access_control_config.collection_name].client
        )

        rule = request_json.get("rule", {})
        permission_data = {
            "ptype": request_json.get("policy_type", None),
            "v0": rule.get("v0", None),
            "v1": rule.get("v1", None),
            "v2": rule.get("v2", None),
            "v3": rule.get("v3", None),
            "v4": rule.get("v4", None),
            "v5": rule.get("v5", None)
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
    access_control_config = current_app.config['FOCA'].access_control
    db_coll_permission = (
        current_app.config['FOCA'].db.dbs[access_control_config.db_name]
        .collections[access_control_config.collection_name].client
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
    access_control_config = current_app.config['FOCA'].access_control
    db_coll_permission = (
        current_app.config['FOCA'].db.dbs[access_control_config.db_name]
        .collections[access_control_config.collection_name].client
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
    access_control_config = current_app.config['FOCA'].access_control
    db_coll_permission = (
        current_app.config['FOCA'].db.dbs[access_control_config.db_name]
        .collections[access_control_config.collection_name].client
    )

    del_obj_permission = db_coll_permission.delete_one({'_id': ObjectId(id)})

    if del_obj_permission.deleted_count:
        return id
    else:
        raise NotFound