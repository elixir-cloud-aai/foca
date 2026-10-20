"""Serve GA4GH service info from the configuration.

Service info (`GET /service-info`) is built from config section
`service_info` (cf. :py:class:`foca.models.config.ServiceInfoConfig`) and
validated against the `Service` schema of the GA4GH service-info
specification, version 1.0.0, bundled with FOCA (cf.
:py:mod:`foca.utils.service_info`). Apps serve it by routing the operation to
:py:func:`getServiceInfo` in their API specifications.
"""

from __future__ import annotations

import logging
from inspect import unwrap
from typing import TYPE_CHECKING, Any

from flask import Flask
from werkzeug.exceptions import InternalServerError

from foca.config.access import get_config
from foca.utils.service_info import (
    InvalidServiceInfo,
    call_runtime_function,
    merge_service_info,
    validate_service_info,
)

if TYPE_CHECKING:
    from foca.models.config import ServiceInfoConfig

__all__ = [
    "GetServiceInfo",
    "InvalidServiceInfo",
    "build_service_info",
    "check_service_info",
    "getServiceInfo",
    "get_service_info",
]

logger = logging.getLogger(__name__)


def build_service_info(conf: ServiceInfoConfig) -> dict[str, Any]:
    """Build and validate service info from its configuration.

    Fields are taken, in order of precedence, from the configuration, the
    function returning fields at runtime (called on each call), and the
    package metadata (cf.
    :py:func:`foca.utils.service_info.merge_service_info`).

    Args:
        conf: Service info configuration.

    Returns:
        Service info, as a copy.

    Raises:
        InvalidServiceInfo: The function returning fields at runtime failed
            or did not return a dictionary, or the service info is invalid.
    """
    if conf.runtime_function is None:
        info = merge_service_info(conf.service, conf.package_version)
        validate_service_info(info, prefix="service_info.service")
        return info
    path = str(conf.runtime_fields)
    runtime = call_runtime_function(conf.runtime_function, path)
    info = merge_service_info(conf.service, conf.package_version, runtime)
    validate_service_info(
        info, note=f" (from `service_info.service` and `{path}`)"
    )
    return info


def get_service_info() -> dict[str, Any]:
    """Get the service info of the current app.

    Returns:
        Service info, built from the configuration of the current app (cf.
        :py:func:`build_service_info`).

    Raises:
        RuntimeError: No service info is configured.
        InvalidServiceInfo: The service info is invalid.
    """
    conf = get_config().service_info
    if conf is None:
        raise RuntimeError(
            "No service info configured; set config section `service_info`"
        )
    return build_service_info(conf)


def getServiceInfo() -> dict[str, Any]:
    """Controller for `GET /service-info`.

    Route the operation of your API specifications here, e.g., with
    `x-openapi-router-controller: foca.api.service_info` and `operationId:
    getServiceInfo` (or `GetServiceInfo`, as in the GA4GH WES and TES
    specifications).

    Returns:
        Service info of the current app.

    Raises:
        werkzeug.exceptions.InternalServerError: The service info cannot be
            built (the error is logged), whatever exceptions apps map to
            other problem details (e.g., `ValueError`).
    """
    try:
        return get_service_info()
    except (InvalidServiceInfo, RuntimeError) as exc:
        logger.exception(f"Service info not served: {exc}")
        raise InternalServerError() from exc


# Spelling of the operation in the GA4GH WES and TES specifications
GetServiceInfo = getServiceInfo


def _routes_to_controller(app: Flask) -> list[str]:
    """List the routes of an app served by :py:func:`getServiceInfo`.

    Routes are found by their view functions, unwrapping decorators (e.g.,
    of Connexion), so they are found whichever resolver routed them.

    Args:
        app: Flask app.

    Returns:
        Routes, e.g., ``['/ga4gh/wes/v1/service-info']``.
    """
    return sorted(
        rule.rule
        for rule in app.url_map.iter_rules()
        if unwrap(app.view_functions[rule.endpoint]) is getServiceInfo
    )


def check_service_info(app: Flask) -> None:
    """Check the service info of an app.

    The function returning fields at runtime, if configured, is called in an
    app context.

    Args:
        app: Flask app, with API specifications registered.

    Raises:
        InvalidServiceInfo: The service info is invalid, or the app routes
            operations to :py:func:`getServiceInfo`, but no service info is
            configured.
    """
    with app.app_context():
        if get_config().service_info is None:
            routes = _routes_to_controller(app)
            if routes:
                raise InvalidServiceInfo(
                    f"Routes {routes} are served by "
                    "`foca.api.service_info.getServiceInfo`, but no service "
                    "info is configured; set config section `service_info`"
                )
            logger.info("No service info configured.")
            return
        info = get_service_info()
    logger.info(
        f"Serving service info '{info['id']}', version '{info['version']}'."
    )
