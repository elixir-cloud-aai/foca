"""Controller of the optional endpoint telling callers who they are.

Enabled with `security.access_control.me_enabled`, e.g., for clients that show
or hide admin functions.
"""

from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Any

from foca.config.access import get_config
from foca.security.access_control.constants import ADMIN_ROLE
from foca.security.access_control.enforcement import (
    get_available_opt_in_roles,
    get_current_client,
    get_current_groups,
    get_current_subject,
)
from foca.security.access_control.ownership import is_admin


def import_me_hook(path: str) -> Callable[[], Mapping[str, Any]]:
    """Import the function adding fields of an app to the response of `GET /me`.

    Args:
        path: Dot-separated path to the module, followed by the name of the
            function, e.g., ``my_app.access.me_fields``.

    Returns:
        Imported function.

    Raises:
        ValueError: The function cannot be imported or is not callable.
    """
    module_path, _, name = path.rpartition(".")
    try:
        function = getattr(import_module(module_path), name)
    except (ImportError, ValueError, AttributeError) as exc:
        raise ValueError(
            f"'security.access_control.me_hook' could not be imported: '{path}'"
        ) from exc
    if not callable(function):
        raise ValueError(f"'security.access_control.me_hook' is not callable: '{path}'")
    return function


def getMe() -> dict[str, Any]:
    """Get who the caller is, and what they may do.

    Returns:
        Fields of the app's function (`access_control.me_hook`), if any, and
        these, which it can't replace: the identifier of the caller (`subject`,
        cf. :py:func:`get_current_subject`), the client ID of a service client
        (`client`, cf. :py:func:`get_current_client`), the roles from group
        claims (`groups`, cf. :py:func:`get_current_groups`), whether the caller
        is an admin on this request (`admin`, cf.
        :py:func:`foca.security.access_control.ownership.is_admin`; with opt-in,
        only if the request asks for it) and whether they may be one
        (`admin_available`; cf. :py:func:`get_available_opt_in_roles`).
    """
    hook = get_config().security.access_control.me_hook
    fields = {} if hook is None else dict(import_me_hook(hook)())
    admin = is_admin()
    return {
        **fields,
        "subject": get_current_subject(),
        "client": get_current_client(),
        "groups": get_current_groups(),
        "admin": admin,
        # Admins without opt-in are always admins
        "admin_available": admin or ADMIN_ROLE in get_available_opt_in_roles(),
    }
