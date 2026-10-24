"""Glue between the plugin engine and Connexion 2 / Flask.

The only module of :py:mod:`foca.plugins` that uses the web framework: it
builds the framework-neutral request model (cf. :py:mod:`foca.plugins.model`)
from the current request, keeps the app's :py:class:`PluginHost` and turns
gateway errors into problem responses. FOCA 1.0 (Connexion 3, without
Flask) replaces this module and keeps its function names and signatures.
"""

from typing import Any

from flask import current_app, has_request_context, request
from flask.wrappers import Response
from werkzeug.exceptions import HTTPException

from foca.errors.exceptions import problem_response
from foca.plugins.engine import GatewayError, PluginHost
from foca.plugins.model import CREDENTIAL_HEADERS, Headers, InboundRequest, Principal
from foca.security.access_control.enforcement import (
    _get_claims,
    get_current_groups,
    get_current_subject,
)

# Key of the plugin host in the Flask app's config
_HOST_KEY = "foca_plugin_host"


def register_plugin_host(app: Any, host: PluginHost) -> PluginHost:
    """Keep the plugin host of an app, e.g., for :py:func:`get_plugin_host`.

    Args:
        app: Connexion app (e.g., from `Foca.create_app()`) or Flask app.
        host: Plugin host.

    Returns:
        The host.
    """
    flask_app = getattr(app, "app", app)
    flask_app.config[_HOST_KEY] = host
    return host


def get_plugin_host() -> PluginHost:
    """Get the plugin host of the current app.

    Returns:
        The host registered with :py:func:`register_plugin_host`.

    Raises:
        RuntimeError: No host is registered, or there is no app context.
    """
    host = current_app.config.get(_HOST_KEY)
    if not isinstance(host, PluginHost):
        raise RuntimeError("No plugin host registered: call register_plugin_host()")
    return host


def current_principal() -> Principal:
    """Get the authenticated caller of the current request.

    Returns:
        Subject, issuer and claims of the validated token, roles from group
        claims and the bearer token; empty outside requests or without a
        validated token.
    """
    if not has_request_context():
        return Principal()
    claims = _get_claims()
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    issuer = claims.get("iss")
    return Principal(
        subject=get_current_subject(),
        issuer=issuer if isinstance(issuer, str) else None,
        groups=tuple(get_current_groups()),
        claims=dict(claims),
        token=token.strip() or None if scheme.lower() == "bearer" else None,
    )


def inbound_from_request(payload: Any, route: str | None = None) -> InboundRequest:
    """Build the inbound request of the current request.

    Args:
        payload: Parsed body, as injected by Connexion (e.g., the `body`
            argument of the controller).
        route: Path template of the operation; defaults to Flask's URL rule.

    Returns:
        Inbound request, with a copy of the query parameters and of the
        headers without credentials (cf.
        :py:data:`foca.plugins.model.CREDENTIAL_HEADERS`).
    """
    headers = Headers(
        {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in CREDENTIAL_HEADERS
        }
    )
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
        Error response with the error's status, title and detail, and its
        problem type as extension member `problem_type` (FOCA maps `type`
        per exception class).
    """
    http_exc = HTTPException(description=exc.detail)
    http_exc.code = exc.code
    return problem_response(http_exc, problem_type=exc.problem_type)
