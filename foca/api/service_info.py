"""Serve GA4GH service info from the configuration.

Service info (`GET /service-info`) is built from config section
`service_info` (cf. :py:class:`foca.models.config.ServiceInfoConfig`) and
validated against the `Service` schema of the GA4GH service-info
specification, version 1.0.0, bundled with FOCA. Apps serve it by routing the
operation to :py:func:`getServiceInfo` in their API specifications.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator, Mapping
from datetime import date
from functools import cache
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from typing import TYPE_CHECKING, Any

import yaml
from flask import Flask
from jsonschema import Draft4Validator, FormatChecker, ValidationError
from jsonschema.exceptions import best_match

from foca.config.access import get_config

if TYPE_CHECKING:
    from foca.models.config import ServiceInfoConfig

logger = logging.getLogger(__name__)

# GA4GH service-info specification, version 1.0.0, with the `Service` schema
SPEC_FILE = "service-info.yaml"
SCHEMA = "Service"
# Formats used by the `Service` schema; `jsonschema` only checks them if
# optional packages are installed (`rfc3987`, `rfc3339-validator`), so they
# are checked here
FORMAT_CHECKER = FormatChecker(formats=())
# RFC 3986: a scheme, followed by anything without whitespace
_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:\S*")
# RFC 3339, section 5.6 (ASCII digits only; the RFC allows a space instead of
# `T` in a note only)
_DATE_TIME = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})[Tt]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(\.\d+)?"
    r"([Zz]|[+-](?P<offset_hour>\d{2}):(?P<offset_minute>\d{2}))",
    re.ASCII,
)


class InvalidServiceInfo(ValueError):
    """The service info or its configuration is invalid."""


@FORMAT_CHECKER.checks("uri")
def _is_uri(value: object) -> bool:
    """Check whether a string is an absolute URI (non-strings pass)."""
    return not isinstance(value, str) or _URI.fullmatch(value) is not None


@FORMAT_CHECKER.checks("date-time")
def _is_date_time(value: object) -> bool:
    """Check whether a string is an RFC 3339 date-time (non-strings pass)."""
    if not isinstance(value, str):
        return True
    match = _DATE_TIME.fullmatch(value)
    if match is None:
        return False
    parts = {name: int(part or 0) for name, part in match.groupdict().items()}
    try:
        date(parts["year"], parts["month"], parts["day"])
    except ValueError:
        return False
    # Seconds may be 60 (leap seconds)
    return (
        parts["hour"] < 24
        and parts["minute"] < 60
        and parts["second"] <= 60
        and parts["offset_hour"] < 24
        and parts["offset_minute"] < 60
    )


@cache
def _validator() -> Draft4Validator:
    """Get a validator for the `Service` schema of the bundled specification.

    Returns:
        Validator resolving references within the specification.
    """
    spec = yaml.safe_load(
        files(__package__).joinpath(SPEC_FILE).read_text(encoding="utf-8")
    )
    schema = {
        "$ref": f"#/components/schemas/{SCHEMA}",
        "components": spec["components"],
    }
    return Draft4Validator(schema, format_checker=FORMAT_CHECKER)


def without_nulls(data: Any) -> Any:
    """Copy data without ``None`` values, recursively.

    Args:
        data: Data, e.g., service info.

    Returns:
        Copy of `data`, without dictionary items and list items that are
        ``None``. Config overlays cannot remove fields otherwise.
    """
    if isinstance(data, Mapping):
        return {
            key: without_nulls(value)
            for key, value in data.items()
            if value is not None
        }
    if isinstance(data, list):
        return [without_nulls(item) for item in data if item is not None]
    return data


def validate_service_info(
    info: Mapping[str, Any],
    prefix: str | None = None,
    ignore_missing: bool = False,
    note: str = "",
) -> None:
    """Validate service info against the GA4GH `Service` schema.

    Args:
        info: Service info.
        prefix: Path of the service info in the configuration, prepended to
            the path of invalid fields in error messages.
        ignore_missing: Whether to ignore missing required fields of the
            service info (but not of nested objects), e.g., if they may be
            computed at runtime.
        note: Note appended to the path of invalid fields in error messages,
            e.g., where fields come from.

    Raises:
        InvalidServiceInfo: The service info is invalid; the error names the
            first invalid field.
    """
    errors: Iterator[ValidationError] = _validator().iter_errors(info)
    if ignore_missing:
        errors = (
            error
            for error in errors
            if error.validator != "required" or error.absolute_path
        )
    error = best_match(errors)
    if error is not None:
        parts = [str(part) for part in error.absolute_path]
        field = ".".join([prefix, *parts] if prefix else parts) or "(root)"
        raise InvalidServiceInfo(
            f"Invalid service info in `{field}`{note}: {error.message}"
        )


def package_fields(package: str) -> dict[str, Any]:
    """Get service info fields from the metadata of an installed package.

    Args:
        package: Name of the distribution package, as installed (e.g., with
            `pip install`).

    Returns:
        The package version as `version`.

    Raises:
        InvalidServiceInfo: The package is not installed.
    """
    try:
        return {"version": version(package)}
    except PackageNotFoundError as exc:
        raise InvalidServiceInfo(
            f"Package '{package}' (`service_info.package`) is not installed; "
            "install it, or set `service_info.service.version`"
        ) from exc


def import_runtime_fields(path: str) -> Callable[[], Mapping[str, Any]]:
    """Import a function returning service info fields at runtime.

    Args:
        path: Dot-separated path to the module, followed by the name of the
            function, e.g., ``my_app.service_info.runtime_fields``.

    Returns:
        Imported function.

    Raises:
        InvalidServiceInfo: The function cannot be imported or is not
            callable.
    """
    function = (
        f"Runtime fields function '{path}' (`service_info.runtime_fields`)"
    )
    module_path, _, name = path.rpartition(".")
    try:
        func = getattr(import_module(module_path), name)
    except (ImportError, ValueError, AttributeError) as exc:
        raise InvalidServiceInfo(f"{function} could not be imported") from exc
    if not callable(func):
        raise InvalidServiceInfo(f"{function} is not callable")
    return func


def build_service_info(conf: ServiceInfoConfig) -> dict[str, Any]:
    """Build and validate service info from its configuration.

    Fields are taken, in order of precedence, from the configuration, the
    function returning fields at runtime (called on each call), and the
    package metadata. Fields and list items set to ``None`` are removed, also
    if set at a lower precedence.

    Args:
        conf: Service info configuration.

    Returns:
        Service info, as a copy.

    Raises:
        InvalidServiceInfo: The runtime fields are not a dictionary, or the
            service info is invalid.
    """
    if conf.runtime_function is None:
        info = without_nulls({**conf.package_fields, **conf.service})
        validate_service_info(info, prefix="service_info.service")
        return info
    runtime = conf.runtime_function()
    if not isinstance(runtime, Mapping):
        raise InvalidServiceInfo(
            f"Runtime fields function '{conf.runtime_fields}' "
            "(`service_info.runtime_fields`) did not return a dictionary"
        )
    info = without_nulls({**conf.package_fields, **runtime, **conf.service})
    validate_service_info(
        info,
        note=f" (from `service_info.service` and `{conf.runtime_fields}`)",
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
    getServiceInfo`.

    Returns:
        Service info of the current app.
    """
    return get_service_info()


def check_service_info(app: Flask) -> None:
    """Check the service info of an app.

    The function returning fields at runtime, if configured, is called in an
    app context.

    Args:
        app: Flask app, with service info configured.

    Raises:
        RuntimeError: No service info is configured.
        InvalidServiceInfo: The service info is invalid.
    """
    with app.app_context():
        info = get_service_info()
    logger.info(
        f"Serving service info '{info['id']}', version '{info['version']}'."
    )
