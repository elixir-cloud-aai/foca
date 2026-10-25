"""Glue between the plugin engine and Connexion 2 / Flask.

The only module of :py:mod:`foca.plugins` that uses the web framework: it
builds the framework-neutral request model (cf. :py:mod:`foca.plugins.model`)
from the current request and turns gateway errors into problem responses.
The plugin host itself is reached without the framework (cf.
:py:mod:`foca.plugins.runtime`). FOCA 1.0 (Connexion 3, without
Flask) replaces this module and keeps its function names and signatures.
"""

import re
import uuid
from typing import Any

from flask import has_request_context, request
from flask.wrappers import Response
from werkzeug.exceptions import HTTPException

from foca.errors.exceptions import problem_response
from foca.plugins.engine import GatewayError
from foca.plugins.model import CREDENTIAL_HEADERS, Headers, InboundRequest, Principal
from foca.security.access_control.enforcement import (
    get_current_claims,
    get_current_groups,
    get_current_subject,
)

# Request IDs taken from clients (`X-Request-ID`)
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def request_id() -> str:
    """Get the identifier of the current request, for logs.

    Returns:
        The client's `X-Request-ID` header if it is a safe identifier (at
        most 128 letters, digits and `._:-`), otherwise a new random one.
    """
    value = request.headers.get("X-Request-ID", "") if has_request_context() else ""
    return value if _REQUEST_ID.fullmatch(value) else uuid.uuid4().hex


def current_principal() -> Principal:
    """Get the authenticated caller of the current request.

    Returns:
        Subject, issuer and claims of the validated token (as used by
        access control), roles from group claims (the mapped roles of
        access control, cf.
        :py:func:`foca.security.access_control.enforcement.get_current_groups`)
        and the bearer token, only if a token was validated for the request
        (i.e., there are claims); empty outside requests or without a
        validated token.
    """
    if not has_request_context():
        return Principal()
    claims = get_current_claims()
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    issuer = claims.get("iss")
    return Principal(
        subject=get_current_subject(),
        issuer=issuer if isinstance(issuer, str) else None,
        groups=tuple(get_current_groups()),
        claims=dict(claims),
        token=(token.strip() or None)
        if claims and scheme.lower() == "bearer"
        else None,
    )


def inbound_from_request(payload: Any, route: str | None = None) -> InboundRequest:
    """Build the inbound request of the current request.

    Args:
        payload: Parsed body, as injected by Connexion (e.g., the `body`
            argument of the controller).
        route: Path template of the operation; defaults to Flask's URL rule.

    Returns:
        Inbound request, with a copy of the query parameters and of the
        headers (repeated fields combined) without credentials (cf.
        :py:data:`foca.plugins.model.CREDENTIAL_HEADERS`).
    """
    # repeated field lines are combined (RFC 9110, section 5.3)
    fields: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for name, value in request.headers.items():
        key = name.lower()
        if key not in CREDENTIAL_HEADERS:
            names.setdefault(key, name)
            fields.setdefault(key, []).append(value)
    headers = Headers({names[key]: ", ".join(values) for key, values in fields.items()})
    rule = request.url_rule.rule if request.url_rule is not None else request.path
    return InboundRequest(
        method=request.method.upper(),
        path=request.path,
        route=route if route is not None else rule,
        path_params={
            key: str(value) for key, value in (request.view_args or {}).items()
        },
        query={key: request.args.getlist(key) for key in request.args},
        headers=headers,
        media_type=request.mimetype or "",
        payload=payload,
    )


def problem(exc: GatewayError) -> Response:
    """Build the problem response of a gateway error.

    Args:
        exc: Gateway error.

    Returns:
        Error response with the error's status, title (e.g., that of a
        plugin's rejection) and detail, and its problem type as extension
        member `problem_type` (FOCA maps `type` per exception class).
    """
    http_exc = HTTPException(description=exc.detail)
    http_exc.code = exc.code
    return problem_response(http_exc, title=exc.title, problem_type=exc.problem_type)
