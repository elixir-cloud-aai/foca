"""Tests for `foca.utils.service_info` module."""

from datetime import date
from importlib.metadata import PackageNotFoundError

import pytest

from foca.utils import service_info as module
from foca.utils.service_info import (
    InvalidServiceInfo,
    call_runtime_function,
    import_runtime_function,
    lookup_package_version,
    merge_service_info,
    validate_service_info,
    without_nulls,
)
from tests.test_files.service_info_data import (
    FIELDS,
    REMOVE,
    REQUIRED,
    SERVICE,
    service,
)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.org",
        "mailto:support@example.org",
        "urn:isbn:0451450523",
        "x:",
        1,
    ],
)
def test_is_uri(value):
    """Absolute URIs with any scheme pass; non-strings are left to the
    schema.
    """
    assert module._is_uri(value)


@pytest.mark.parametrize(
    "value",
    ["support", "/org", "docs example org", "https://example.org/a b", ""],
)
def test_is_uri_invalid(value):
    """Relative URIs and strings with whitespace fail."""
    assert not module._is_uri(value)


@pytest.mark.parametrize(
    "value",
    [
        "2020-11-08T10:20:30Z",
        "2020-11-08T10:20:30.123+01:00",
        "2020-11-08t10:20:30z",
        "2020-02-29T10:20:30.1234567-05:30",
        "2016-12-31T23:59:60Z",
        1,
    ],
)
def test_is_date_time(value):
    """RFC 3339 date-times pass, including leap seconds; non-strings are left
    to the schema.
    """
    assert module._is_date_time(value)


@pytest.mark.parametrize(
    "value",
    [
        "yesterday",
        "2020-11-08",
        "2020-11-08T00:00:00",
        "2020-13-45T25:99:99Z",
        "2021-02-29T00:00:00Z",
        "2020-11-08T24:00:00Z",
        "2020-11-08T00:60:00Z",
        "2020-11-08T00:00:61Z",
        "2020-11-08T00:00:00+00:60",
        "2020-11-08T00:00:00+24:00",
        "2020-11-08 00:00:00Z",
        # Fullwidth digits
        "".join(chr(0xFF10 + int(digit)) for digit in "2020")
        + "-11-08T00:00:00Z",
    ],
)
def test_is_date_time_invalid(value):
    """Other strings fail, also with non-ASCII digits."""
    assert not module._is_date_time(value)


def test_without_nulls():
    """Fields and list items set to `None` are removed recursively, from a
    copy.
    """
    data = {"a": None, "b": [None, {"c": None, "d": 1}], "e": {"f": None}}
    copy = without_nulls(data)
    assert copy == {"b": [{"d": 1}], "e": {}}
    copy["b"][0]["d"] = 2
    assert data["b"][1]["d"] == 1


def test_merge_service_info():
    """Fields are taken from the service, the runtime fields and the package
    version, in this order; `None` removes fields of lower precedence.
    """
    configured = {"a": 1, "b": None}
    runtime = {"a": 2, "b": 2, "c": 2, "d": None}
    assert merge_service_info(configured, runtime=runtime) == {"a": 1, "c": 2}
    assert merge_service_info({"a": 1}, "1.0", {"version": "2.0"}) == {
        "a": 1,
        "version": "2.0",
    }
    assert merge_service_info({"a": 1}, "1.0") == {"a": 1, "version": "1.0"}
    # `version: None` of the service means that it is not set
    unset = {"version": None}
    assert merge_service_info(unset, "1.0") == {"version": "1.0"}
    assert merge_service_info(unset, "1.0", {"version": "2.0"}) == {
        "version": "2.0"
    }
    # Also if returned at runtime
    assert merge_service_info({}, "1.0", unset) == {"version": "1.0"}
    assert merge_service_info(unset, "1.0", unset) == {"version": "1.0"}
    assert merge_service_info({"a": [None, 1]}) == {"a": [1]}


def test_merge_service_info_copy():
    """The merged service info is a copy."""
    configured = service()
    merged = merge_service_info(configured)
    merged["organization"]["name"] = "changed"
    assert configured == SERVICE


def test_lookup_package_version(monkeypatch):
    """The version is taken from the package metadata."""
    monkeypatch.setattr(module, "version", lambda name: f"{name}-1.2.3")
    assert lookup_package_version("my-app") == "my-app-1.2.3"


def test_lookup_package_version_not_installed(monkeypatch):
    """Packages that are not installed are named."""

    def not_installed(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(module, "version", not_installed)
    with pytest.raises(
        InvalidServiceInfo,
        match=(
            r"Package 'my-app' \(`service_info\.package`\) is not installed; "
            r"install it, or set `service_info\.service\.version`"
        ),
    ):
        lookup_package_version("my-app")


def test_import_runtime_function():
    """Functions are imported by their dotted path."""
    func = import_runtime_function(f"{FIELDS}.version_field")
    assert func() == {"version": "2.0.0-runtime"}


@pytest.mark.parametrize(
    "path",
    [f"{FIELDS}.missing", "tests.test_files.missing.func", "func", ""],
)
def test_import_runtime_function_missing(path):
    """Paths that cannot be imported are named."""
    with pytest.raises(
        InvalidServiceInfo,
        match=(
            rf"Runtime fields function '{path}' "
            r"\(`service_info\.runtime_fields`\) could not be imported"
        ),
    ):
        import_runtime_function(path)


def test_import_runtime_function_not_callable():
    """Imported objects must be callable."""
    with pytest.raises(InvalidServiceInfo, match="is not callable"):
        import_runtime_function(f"{FIELDS}.NOT_CALLABLE")


def test_call_runtime_function():
    """The fields returned by the function are returned."""
    assert call_runtime_function(lambda: {"a": 1}, "f") == {"a": 1}


def test_call_runtime_function_failed():
    """Exceptions raised by the function are chained."""
    error = KeyError("missing")

    def failing():
        raise error

    with pytest.raises(
        InvalidServiceInfo,
        match=(
            r"Runtime fields function 'my\.f' "
            r"\(`service_info\.runtime_fields`\) failed"
        ),
    ) as exc_info:
        call_runtime_function(failing, "my.f")
    assert exc_info.value.__cause__ is error


def test_call_runtime_function_not_a_dict():
    """The function must return a dictionary."""
    with pytest.raises(
        InvalidServiceInfo, match="did not return a dictionary"
    ):
        call_runtime_function(lambda: ["a"], "f")  # type: ignore[arg-type]


def test_validate_service_info():
    """Valid service info passes, also with the required fields only and
    with extra fields.
    """
    validate_service_info(SERVICE)
    validate_service_info({field: SERVICE[field] for field in REQUIRED})
    validate_service_info(service(**{"x-custom": {"any": "value"}}))
    validate_service_info(
        service(
            createdAt="2020-11-08T10:20:30Z", updatedAt="2026-10-20T00:00:00Z"
        )
    )


@pytest.mark.parametrize("field", REQUIRED)
def test_validate_service_info_missing(field):
    """Missing required fields are named."""
    with pytest.raises(
        InvalidServiceInfo,
        match=rf"in `service_info\.service`: '{field}' is a required property",
    ):
        validate_service_info(
            service(**{field: REMOVE}), prefix="service_info.service"
        )


@pytest.mark.parametrize("field", REQUIRED)
def test_validate_service_info_ignore_missing(field):
    """Missing required fields can be ignored."""
    validate_service_info(service(**{field: REMOVE}), ignore_missing=True)


@pytest.mark.parametrize(
    "fields,path",
    [
        ({"contactUrl": "support"}, "contactUrl"),
        ({"documentationUrl": "docs example org"}, "documentationUrl"),
        ({"organization": {"name": "org", "url": "/org"}}, "organization.url"),
        ({"organization": {"name": "org"}}, "organization"),
        ({"createdAt": "yesterday"}, "createdAt"),
        ({"updatedAt": "2020-11-08"}, "updatedAt"),
        ({"type": {"group": "org.ga4gh", "artifact": "x"}}, "type"),
        ({"name": 1}, "name"),
    ],
)
def test_validate_service_info_invalid(fields, path):
    """Invalid fields are named, also when ignoring missing fields."""
    invalid = service(**fields)
    with pytest.raises(InvalidServiceInfo, match=rf"in `x\.{path}`"):
        validate_service_info(invalid, prefix="x")
    with pytest.raises(InvalidServiceInfo, match=rf"in `{path}`"):
        validate_service_info(invalid, ignore_missing=True)


def test_validate_service_info_not_an_object():
    """Service info must be an object."""
    with pytest.raises(InvalidServiceInfo, match=r"in `\(root\)`"):
        validate_service_info([])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value,reason",
    [
        (date(2026, 10, 20), "Object of type date is not JSON serializable"),
        ({1, 2}, "Object of type set is not JSON serializable"),
        (float("nan"), "Out of range float values are not JSON compliant"),
    ],
)
def test_validate_service_info_not_json(value, reason):
    """Service info must be serializable as JSON."""
    invalid = service(**{"x-custom": value})
    with pytest.raises(
        InvalidServiceInfo,
        match=(
            r"Invalid service info in `service_info\.service` \(note\): not "
            rf"JSON-serializable \({reason}"
        ),
    ):
        validate_service_info(
            invalid, prefix="service_info.service", note=" (note)"
        )
    with pytest.raises(InvalidServiceInfo, match=r"in `\(root\)`: not JSON"):
        validate_service_info(invalid)
