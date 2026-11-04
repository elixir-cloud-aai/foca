"""Audit log: who changed what, on a logger of its own.

Events are written to the logger `foca.audit`, on level `INFO`, so that
operators can route them (cf. the documentation of `log` in the configuration).
The permissions API and the helpers for resource roles use it; apps can log
their own events.
"""

import json
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from foca.context import request_id
from foca.utils.logging import _to_json_value, _to_text

logger = logging.getLogger(__name__)
# Not below the audit logger, whose handlers may be the ones failing
_failure_logger = logging.getLogger("foca.audit_failures")


def _try(get: Callable[[], str | None]) -> str | None:
    """Get a part of the actor; logging an event must not fail.

    Args:
        get: Function getting the part.

    Returns:
        What the function returns, or `None` if it fails, e.g., for an app without
        FOCA's configuration, or for a header with an invalid user; the failure is
        logged on level `DEBUG`.
    """
    try:
        return get()
    except Exception:
        logger.debug("Part of the actor not available", exc_info=True)
        return None


def _get_on_behalf_of() -> str | None:
    """Get the user that the service client of the current request acts for.

    Returns:
        User named by the service client, if it's allowed to act for users,
        otherwise `None`. Callers that aren't allowed aren't told apart: rejecting
        them is up to the operation, and this must not log it for them.
    """
    # Import cycle, as the access control modules use this module
    from foca.security.access_control.enforcement import (  # noqa: PLC0415
        _get_current_service_client,
        get_on_behalf_of_user,
    )

    client = _get_current_service_client()
    if client is None or not client.on_behalf_of:
        return None
    return get_on_behalf_of_user()


def _get_actor() -> dict[str, str | None]:
    """Get who is making the current request.

    Returns:
        The `subject` and `client` of the validated token and the user that a
        service client acts for (`on_behalf_of`); `None` where not available,
        e.g., outside of requests.
    """
    # Import cycles, as the access control modules use this module
    from foca.security.access_control.enforcement import (  # noqa: PLC0415
        get_current_claims,
        get_current_subject,
    )
    from foca.security.auth import get_token_client  # noqa: PLC0415

    return {
        "subject": _try(get_current_subject),
        "client": _try(lambda: get_token_client(get_current_claims())),
        "on_behalf_of": _try(_get_on_behalf_of),
    }


def _format_value(value: object) -> str:
    """Format a value for the text of an event.

    Args:
        value: Value.

    Returns:
        Value as JSON, in one line of printable ASCII, so that strings have their
        quotes and can't forge other fields or log lines.
    """
    return json.dumps(value, default=_to_text)


def _format_name(name: str) -> str:
    """Format the name of a field for the text of an event.

    Args:
        name: Name of the field.

    Returns:
        The name, quoted and escaped unless it's an identifier.
    """
    return name if name.isidentifier() else _format_value(name)


def log(action: str, target: object, /, **fields: Any) -> None:
    """Log an audit event to the logger `foca.audit`, on level `INFO`.

    The record has all fields in `extra={"audit": {...}}`, so that the formatter
    `json` (cf. :py:class:`foca.utils.logging.JsonFormatter`) writes them as an
    object. Its message has them, too, as `name=value` with values as JSON, so that
    values can't forge fields or log lines. Values that JSON doesn't have are
    replaced with their text.

    This never fails, as what it logs has happened: not if the actor can't be
    determined, and not if a handler fails, which is logged to the logger
    `foca.audit_failures`.

    Args:
        action: What was done, e.g., `permission.create`.
        target: What it was done to, e.g., the path of a resource.
        **fields: Further fields, e.g., the role granted. Fields named `action`,
            `target`, `actor` or `request_id` are left out, so that they can't
            replace the actual ones. Don't pass secrets. The `on_behalf_of` of the
            actor is the user that a service client names in the header
            `auth.on_behalf_of_header`, also if the operation doesn't act for it; a
            user named in another way isn't known to it, so pass it as a field.
    """
    event: dict[str, Any] = {
        "action": action,
        "target": _to_json_value(target),
        "actor": _get_actor(),
        "request_id": request_id() or None,
    }
    event.update(
        {
            key: _to_json_value(value)
            for key, value in fields.items()
            if key not in event
        }
    )
    message = " ".join(
        f"{_format_name(key)}={_format_value(value)}" for key, value in event.items()
    )
    try:
        logger.info("%s", message, extra={"audit": event})
    except Exception:
        with suppress(Exception):
            _failure_logger.exception("Audit event not logged: %s", message)
