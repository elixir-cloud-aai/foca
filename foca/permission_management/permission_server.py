""""Controllers for permission management endpoints."""

import logging
from typing import (Optional, Dict, List, Tuple)
from urllib.parse import unquote

from flask import (request, current_app)
from foca.utils.logging import log_traffic


logger = logging.getLogger(__name__)


@log_traffic
def postPermission() -> str:
    """Method to register new permissions.

    Returns:
        Identifier of the new permission added.
    """
    return ""


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
    return ""


@log_traffic
def getAllPermissions() -> List[Dict]:
    """Method to fetch all permissions.

    Returns:
        List of permission dicts.
    """
    return []


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
    return {}