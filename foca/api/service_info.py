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
from urllib.parse import urlsplit

from flask import Flask
from werkzeug.exceptions import InternalServerError

from foca.config.access import get_config
from foca.utils.logging import log_traffic_if
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

# Reserved example domains (RFC 2606), as used by templates: second-level
# domains, also as parents of hosts, and the top-level domain `example`
EXAMPLE_DOMAINS = ("example.com", "example.net", "example.org", "example")
# The same domains in reverse domain name notation, as used for service IDs
# (e.g., `org.example.my-service`)
_EXAMPLE_ID_DOMAINS = ("com.example", "net.example", "org.example")
# Fields of the service info with URLs, and how they are named in warnings
_URL_FIELDS = (
    (("organization", "url"), "organization.url"),
    (("contactUrl",), "contactUrl"),
    (("documentationUrl",), "documentationUrl"),
)


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
    validate_service_info(info, note=f" (from `service_info.service` and `{path}`)")
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


def _service_info_traffic_logged() -> bool:
    """Check whether the traffic of :py:func:`getServiceInfo` is logged.

    Returns:
        Whether config option `service_info.log_traffic` of the current app
        is set. Also ``True`` (the default of the option) if no service info
        or no FOCA configuration is available, so that requests failing for
        that reason are logged, too.
    """
    try:
        conf = get_config().service_info
    except RuntimeError:
        return True
    return conf is None or conf.log_traffic


@log_traffic_if(_service_info_traffic_logged)
def getServiceInfo() -> dict[str, Any]:
    """Controller for `GET /service-info`.

    Route the operation of your API specifications here, e.g., with
    `x-openapi-router-controller: foca.api.service_info` and `operationId:
    getServiceInfo` (or `GetServiceInfo`, as in the GA4GH WES and TES
    specifications).

    Requests and responses are logged unless disabled via
    `service_info.log_traffic`.

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
        logger.exception("Service info not served: %s", exc)
        raise InternalServerError() from exc


# Spelling of the operation in the GA4GH WES and TES specifications
GetServiceInfo = getServiceInfo


def _routes_to_controller(app: Flask) -> list[str]:
    """List the routes of an app served by :py:func:`getServiceInfo`.

    Routes are found by their view functions, unwrapping decorators (e.g.,
    of Connexion and :py:func:`foca.utils.logging.log_traffic_if`), so they
    are found whichever resolver routed them.

    Args:
        app: Flask app.

    Returns:
        Routes, e.g., ``['/ga4gh/wes/v1/service-info']``.
    """
    controller = unwrap(getServiceInfo)
    return sorted(
        rule.rule
        for rule in app.url_map.iter_rules()
        if unwrap(app.view_functions[rule.endpoint]) is controller
    )


def _in_example_domain(domain: str) -> bool:
    """Check whether a domain is a reserved example domain or a subdomain.

    Args:
        domain: Domain name, e.g., ``docs.example.org``.

    Returns:
        Whether `domain` is one of :py:data:`EXAMPLE_DOMAINS` or a subdomain
        of one, ignoring case and a trailing dot.
    """
    domain = domain.lower().rstrip(".")
    return any(
        domain == example or domain.endswith(f".{example}")
        for example in EXAMPLE_DOMAINS
    )


def _url_domain(url: str) -> str:
    """Get the domain of a URL.

    Args:
        url: URL, e.g., ``https://docs.example.org/`` or
            ``mailto:support@example.org``.

    Returns:
        Host of the URL, or, for `mailto:` URLs, the domain of the (first)
        address; empty if there is none or the URL cannot be parsed.
    """
    try:
        parts = urlsplit(url)
    except ValueError:  # e.g., invalid IPv6 addresses
        return ""
    if parts.scheme.lower() == "mailto":
        address = parts.path.split(",")[0]
        return address.rpartition("@")[2] if "@" in address else ""
    return parts.hostname or ""


def _placeholder_fields(info: dict[str, Any]) -> list[str]:
    """List the fields of service info that are in reserved example domains.

    Args:
        info: Service info.

    Returns:
        Fields whose values are in a reserved example domain (RFC 2606),
        as in templates: `id`, in reverse domain name notation (e.g.,
        ``org.example.my-service`` or ``example.my-service``), and the URLs
        `organization.url`, `contactUrl` and `documentationUrl`, by their
        hosts (cf. :py:func:`_in_example_domain`).
    """
    fields = []
    service_id = info.get("id")
    if isinstance(service_id, str):
        service_id = service_id.lower()
        if service_id.startswith("example.") or any(
            service_id == domain or service_id.startswith(f"{domain}.")
            for domain in _EXAMPLE_ID_DOMAINS
        ):
            fields.append("id")
    for path, name in _URL_FIELDS:
        value: Any = info
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, str) and _in_example_domain(_url_domain(value)):
            fields.append(name)
    return fields


def check_service_info(app: Flask) -> None:
    """Check the service info of an app.

    The function returning fields at runtime, if configured, is called in an
    app context. A warning is logged if the service info still has values in
    reserved example domains, e.g., from a template (cf.
    :py:func:`_placeholder_fields`).

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
    fields = _placeholder_fields(info)
    if fields:
        logger.warning(
            "The service info has values in reserved example domains "
            "(RFC 2606), e.g., from a template: %s; set them to describe the "
            "deployment ('service_info.service').",
            ", ".join(f"'{field}'" for field in fields),
        )
    logger.info("Serving service info '%s', version '%s'.", info["id"], info["version"])
