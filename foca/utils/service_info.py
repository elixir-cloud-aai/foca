"""Build and validate GA4GH service info.

Helpers shared by the service info configuration (cf.
:py:class:`foca.models.config.ServiceInfoConfig`) and its controller (cf.
:py:mod:`foca.api.service_info`). The service info is validated against the
`Service` schema of the GA4GH service-info specification, version 1.0.0,
bundled with FOCA.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from datetime import date
from functools import cache
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from typing import Any

import yaml
from jsonschema import Draft4Validator, FormatChecker, ValidationError
from jsonschema.exceptions import best_match

# GA4GH service-info specification, version 1.0.0, with the `Service` schema
SPEC_FILE = "service-info.yaml"
SCHEMA = "Service"
# Formats used by the `Service` schema (`uri`, `date-time`). `jsonschema`
# only checks them if optional packages are installed (`rfc3987`,
# `rfc3339-validator`), and silently skips them otherwise. To check them
# without depending on these packages, they are checked by the hand-written
# checkers below: strictly for date-times (RFC 3339), and leniently, by
# design, for URIs (any absolute URI, e.g., also `mailto:` or `urn:`).
FORMAT_CHECKER = FormatChecker(formats=())
# Lenient check for absolute URIs: a scheme, followed by a colon and any
# characters other than whitespace (not a full RFC 3986 check)
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
    """Check whether a string looks like an absolute URI (non-strings pass).

    The check is lenient: a scheme, followed by a colon and any characters
    other than whitespace.
    """
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


def _runtime_label(path: str) -> str:
    """Name a runtime fields function in error messages."""
    return f"Runtime fields function '{path}' (`service_info.runtime_fields`)"


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


def _without_null_version(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Copy fields (shallowly) without `version` if it is ``None``."""
    return {
        key: value
        for key, value in fields.items()
        if key != "version" or value is not None
    }


def merge_service_info(
    service: Mapping[str, Any],
    package_version: str | None = None,
    runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge service info fields from their sources.

    Fields are taken, in order of precedence, from `service`, `runtime` and
    `package_version`. Fields and list items set to ``None`` are then
    removed, also if set at a lower precedence, except for `version`: as
    `version` is required, ``None`` means that it is not set, so that it is
    taken from the next source (`runtime`, then `package_version`).

    Args:
        service: Configured service info fields.
        package_version: Version of the app's package, served as `version`.
        runtime: Fields computed at runtime.

    Returns:
        Service info, as a copy.
    """
    package = {} if package_version is None else {"version": package_version}
    return without_nulls(
        {
            **package,
            **_without_null_version(runtime or {}),
            **_without_null_version(service),
        }
    )


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
        InvalidServiceInfo: The service info is invalid, naming the first
            invalid field, or cannot be serialized as JSON.
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
    try:
        json.dumps(info, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidServiceInfo(
            f"Invalid service info in `{prefix or '(root)'}`{note}: not "
            f"JSON-serializable ({exc})"
        ) from exc


def lookup_package_version(package: str) -> str:
    """Get the version of an installed package from its metadata.

    Args:
        package: Name of the distribution package, as installed (e.g., with
            `pip install`).

    Returns:
        Version of the package.

    Raises:
        InvalidServiceInfo: The package is not installed.
    """
    try:
        return version(package)
    except PackageNotFoundError as exc:
        raise InvalidServiceInfo(
            f"Package '{package}' (`service_info.package`) is not installed; "
            "install it, or set `service_info.service.version`"
        ) from exc


def import_runtime_function(path: str) -> Callable[[], Mapping[str, Any]]:
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
    label = _runtime_label(path)
    module_path, _, name = path.rpartition(".")
    try:
        func = getattr(import_module(module_path), name)
    except (ImportError, ValueError, AttributeError) as exc:
        raise InvalidServiceInfo(f"{label} could not be imported") from exc
    if not callable(func):
        raise InvalidServiceInfo(f"{label} is not callable")
    return func


def call_runtime_function(
    func: Callable[[], Mapping[str, Any]], path: str
) -> Mapping[str, Any]:
    """Call a function returning service info fields at runtime.

    Args:
        func: Function returning service info fields.
        path: Path the function was imported from, for error messages.

    Returns:
        Fields returned by the function.

    Raises:
        InvalidServiceInfo: The function raised an exception (chained) or
            did not return a dictionary.
    """
    label = _runtime_label(path)
    try:
        fields = func()
    except Exception as exc:
        raise InvalidServiceInfo(f"{label} failed") from exc
    if not isinstance(fields, Mapping):
        raise InvalidServiceInfo(f"{label} did not return a dictionary")
    return fields
