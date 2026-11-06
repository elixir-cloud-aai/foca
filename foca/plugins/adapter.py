"""Glue between the plugin engine and Connexion 2 / Flask.

The only module of :py:mod:`foca.plugins` that uses the web framework: it
builds the framework-neutral request model (cf. :py:mod:`foca.plugins.model`)
from the current request and turns gateway errors into problem responses.
The plugin host itself is reached without the framework (cf.
:py:mod:`foca.plugins.runtime`). FOCA 1.0 (Connexion 3, without
Flask) replaces this module and keeps its function names and signatures.
"""

import uuid
from typing import Any

from flask.wrappers import Response
from werkzeug.exceptions import HTTPException
from werkzeug.http import parse_options_header

from foca.api.routes import match
from foca.context import _get_request_key, current_request
from foca.context import request_id as get_request_id
from foca.errors.exceptions import problem_response
from foca.plugins.engine import GatewayError
from foca.plugins.model import CREDENTIAL_HEADERS, Headers, InboundRequest, Principal
from foca.security.access_control.enforcement import (
    get_current_claims,
    get_current_client,
    get_current_roles,
    get_current_subject,
    get_on_behalf_of_user,
)


def request_id() -> str:
    """Get the identifier of the current request, for logs.

    Returns:
        Identifier of the current request (cf. :py:func:`foca.context.request_id`),
        otherwise a new random one.
    """
    return get_request_id() or uuid.uuid4().hex


def current_principal() -> Principal:
    """Get the authenticated caller of the current request.

    Returns:
        Identity (the service client, or else the user), the user a service
        client acts for, roles, issuer and claims of the validated token, as
        access control uses them (cf. :py:class:`Principal`), and the
        bearer token only if a token was validated for the request (i.e.,
        there are claims); empty outside requests or without a validated
        token.

    Raises:
        foca.errors.RequestValidationProblem: The user named by a service
            client in `auth.on_behalf_of_header` is invalid (`400`).
        foca.errors.Forbidden: A service client names a user, but
            is not allowed to act for users (`403`).
    """
    if _get_request_key() is None:
        return Principal()
    claims = get_current_claims()
    authorization = current_request().headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    issuer = claims.get("iss")
    client = get_current_client()
    return Principal(
        subject=client if client is not None else get_current_subject(),
        # users by default
        subject_type="client" if client is not None else None,
        # only service clients act for users; others' headers are ignored here
        acting_for=None if client is None else get_on_behalf_of_user(),
        issuer=issuer if isinstance(issuer, str) else None,
        groups=tuple(get_current_roles()),
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
        route: Path template of the operation; defaults to the Flask-style
            URL rule of its route (`Route.rule`, e.g., `/tasks/<id>`, not
            the `{id}` of :py:attr:`foca.context.RequestSnapshot.route`),
            else the request path.

    Path parameters and the default route are those of the operations of the
    app's API specifications (cf. :py:func:`foca.api.routes.match`); for other
    routes (e.g., added via Flask), path parameters are empty.

    Returns:
        Inbound request, with a copy of the query parameters and of the
        headers (repeated fields combined, names in title case, as WSGI
        servers give them) without credentials (cf.
        :py:data:`foca.plugins.model.CREDENTIAL_HEADERS`).
    """
    request = current_request()
    # repeated field lines are combined (RFC 9110, section 5.3)
    headers = Headers(
        {
            name.title(): ", ".join(request.headers.getlist(name))
            for name in request.headers
            if name not in CREDENTIAL_HEADERS
        }
    )
    found = match(request.path, request.method)
    rule = found.route.rule if found is not None else request.path
    return InboundRequest(
        method=request.method.upper(),
        path=request.path,
        route=route if route is not None else rule,
        path_params={key: str(value) for key, value in request.path_params.items()},
        query={name: request.query.getlist(name) for name in request.query},
        headers=headers,
        media_type=parse_options_header(request.headers.get("Content-Type", ""))[
            0
        ].lower(),
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
