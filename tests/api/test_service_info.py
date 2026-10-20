"""Tests for `service_info.py` module."""

import logging
from copy import deepcopy
from importlib.metadata import PackageNotFoundError

import pytest
from flask import Flask
from yaml import safe_dump

from foca import Foca
from foca.api import service_info as module
from foca.api.service_info import (
    InvalidServiceInfo,
    check_service_info,
    get_service_info,
    getServiceInfo,
    import_runtime_fields,
    package_fields,
    validate_service_info,
    without_nulls,
)
from foca.config.access import set_app_config
from foca.models.config import Config

FIELDS = "tests.test_files.service_info_fields"
SERVICE = {
    "id": "org.example.my-service",
    "name": "My service",
    "type": {
        "group": "org.example",
        "artifact": "my-api",
        "version": "1.0.0",
    },
    "description": "Serves examples.",
    "organization": {"name": "Example", "url": "https://example.org"},
    "contactUrl": "mailto:support@example.org",
    "documentationUrl": "https://docs.example.org",
    "environment": "dev",
    "version": "1.2.3",
}
REQUIRED = ("id", "name", "type", "organization", "version")
SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "My service", "version": "1.0.0"},
    "paths": {
        "/service-info": {
            "get": {
                "operationId": "getServiceInfo",
                "x-openapi-router-controller": "foca.api.service_info",
                "responses": {"200": {"description": "Service info"}},
            },
        },
    },
}


def _service(**fields) -> dict:
    """Copy the service info, with fields replaced (`None` removes them)."""
    service = deepcopy(SERVICE)
    for field, value in fields.items():
        if value is None:
            service.pop(field, None)
        else:
            service[field] = value
    return service


def _app(service_info: dict) -> Flask:
    """Create a Flask app with service info configured, without checks."""
    app = Flask(__name__)
    set_app_config(app, Config(service_info=service_info))
    return app


def _foca(tmp_path, service_info: dict | None) -> Foca:
    """Create a FOCA instance with an API serving the service info."""
    spec = tmp_path / "openapi.yaml"
    spec.write_text(safe_dump(SPEC))
    conf: dict = {
        "server": {"port": 8000},
        "api": {
            "specs": [
                {
                    "path": "openapi.yaml",
                    # The operation's own controller takes precedence
                    "add_operation_fields": {
                        "x-openapi-router-controller": "tests.api.controllers"
                    },
                },
            ],
        },
    }
    if service_info is not None:
        conf["service_info"] = service_info
    config_file = tmp_path / "config.yaml"
    config_file.write_text(safe_dump(conf))
    return Foca(config_file=config_file)


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


def test_package_fields(monkeypatch):
    """The version is taken from the package metadata."""
    monkeypatch.setattr(module, "version", lambda name: f"{name}-1.2.3")
    assert package_fields("my-app") == {"version": "my-app-1.2.3"}


def test_package_fields_not_installed(monkeypatch):
    """Packages that are not installed are named."""

    def not_installed(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(module, "version", not_installed)
    with pytest.raises(InvalidServiceInfo, match=r"'my-app' .* not installed"):
        package_fields("my-app")


def test_import_runtime_fields():
    """Functions are imported by their dotted path."""
    func = import_runtime_fields(f"{FIELDS}.version_field")
    assert func() == {"version": "2.0.0-runtime"}


@pytest.mark.parametrize(
    "path",
    [f"{FIELDS}.missing", "tests.test_files.missing.func", "func", ""],
)
def test_import_runtime_fields_missing(path):
    """Paths that cannot be imported are named."""
    with pytest.raises(InvalidServiceInfo, match="could not be imported"):
        import_runtime_fields(path)


def test_import_runtime_fields_not_callable():
    """Imported objects must be callable."""
    with pytest.raises(InvalidServiceInfo, match="is not callable"):
        import_runtime_fields(f"{FIELDS}.NOT_CALLABLE")


def test_validate_service_info():
    """Valid service info passes, also with the required fields only and
    with extra fields.
    """
    validate_service_info(SERVICE)
    validate_service_info({field: SERVICE[field] for field in REQUIRED})
    validate_service_info(_service(**{"x-custom": {"any": "value"}}))
    validate_service_info(
        _service(
            createdAt="2020-11-08T10:20:30Z", updatedAt="2026-10-20T00:00:00Z"
        )
    )


@pytest.mark.parametrize("field", REQUIRED)
def test_validate_service_info_missing(field):
    """Missing required fields are named."""
    with pytest.raises(
        InvalidServiceInfo,
        match=rf"in `service_info.service`: '{field}' is a required",
    ):
        validate_service_info(
            _service(**{field: None}), prefix="service_info.service"
        )


@pytest.mark.parametrize("field", REQUIRED)
def test_validate_service_info_ignore_missing(field):
    """Missing required fields can be ignored."""
    validate_service_info(_service(**{field: None}), ignore_missing=True)


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
    service = _service(**fields)
    with pytest.raises(InvalidServiceInfo, match=rf"in `x\.{path}`"):
        validate_service_info(service, prefix="x")
    with pytest.raises(InvalidServiceInfo, match=rf"in `{path}`"):
        validate_service_info(service, ignore_missing=True)


def test_validate_service_info_not_an_object():
    """Service info must be an object."""
    with pytest.raises(InvalidServiceInfo, match=r"in `\(root\)`"):
        validate_service_info([])  # type: ignore[arg-type]


def test_get_service_info():
    """Service info is served from the configuration, as a copy."""
    app = _app({"service": SERVICE})
    with app.app_context():
        info = get_service_info()
        assert info == SERVICE
        info["organization"]["name"] = "changed"
        assert get_service_info() == SERVICE


def test_get_service_info_nulls():
    """Fields and list items set to `None` (e.g., by config overlays) are
    not served.
    """
    service = _service(
        contactUrl=None,
        organization={**SERVICE["organization"], "x-logo": None},
        **{"x-links": [None, {"url": "https://example.org", "title": None}]},
    )
    service["contactUrl"] = None
    app = _app({"service": service})
    with app.app_context():
        info = get_service_info()
    assert info == {
        **_service(contactUrl=None),
        "x-links": [{"url": "https://example.org"}],
    }


def test_get_service_info_runtime_fields():
    """Runtime fields are computed per request, in the app context, and
    added unless configured; configured `None` values remove them.
    """
    service = _service(description=None)
    service["x-runtime"] = None
    app = _app(
        {"service": service, "runtime_fields": f"{FIELDS}.runtime_fields"}
    )
    with app.app_context():
        assert get_service_info() == _service(description="From runtime")
        app.config.foca.server.port = 9000
        app.config.foca.service_info.service.pop("environment")
        assert get_service_info() == _service(
            description="From runtime", environment="port-9000"
        )


def test_get_service_info_runtime_version():
    """Runtime fields take precedence over the package version."""
    app = _app(
        {
            "service": _service(version=None),
            "package": "foca",
            "runtime_fields": f"{FIELDS}.version_field",
        }
    )
    with app.app_context():
        assert get_service_info() == _service(version="2.0.0-runtime")


def test_get_service_info_package_version(monkeypatch):
    """The package version is served unless configured."""
    monkeypatch.setattr(module, "version", lambda name: "3.0.0")
    app = _app({"service": _service(version=None), "package": "my-app"})
    with app.app_context():
        assert get_service_info() == _service(version="3.0.0")
    app = _app({"service": SERVICE, "package": "my-app"})
    with app.app_context():
        assert get_service_info() == SERVICE


def test_get_service_info_runtime_invalid():
    """Invalid runtime fields are named."""
    app = _app(
        {"service": SERVICE, "runtime_fields": f"{FIELDS}.invalid_fields"}
    )
    with (
        app.app_context(),
        pytest.raises(
            InvalidServiceInfo,
            match=(
                r"in `createdAt` \(from `service_info.service` and "
                rf"`{FIELDS}.invalid_fields`\)"
            ),
        ),
    ):
        get_service_info()


def test_get_service_info_runtime_not_a_dict():
    """Runtime fields must be a dictionary."""
    app = _app({"service": SERVICE, "runtime_fields": f"{FIELDS}.not_a_dict"})
    with (
        app.app_context(),
        pytest.raises(InvalidServiceInfo, match="did not return a dictionary"),
    ):
        get_service_info()


def test_get_service_info_not_configured():
    """Service info must be configured."""
    app = Flask(__name__)
    set_app_config(app, Config())
    with (
        app.app_context(),
        pytest.raises(RuntimeError, match="`service_info`"),
    ):
        get_service_info()


def test_getServiceInfo():
    """The controller returns the service info."""
    app = _app({"service": SERVICE})
    with app.app_context():
        assert getServiceInfo() == SERVICE


def test_check_service_info(caplog):
    """Valid service info is logged."""
    caplog.set_level(logging.INFO)
    check_service_info(_app({"service": SERVICE}))
    message = "Serving service info 'org.example.my-service', version '1.2.3'"
    assert message in caplog.text


def test_service_info_endpoint(tmp_path):
    """Apps serve the service info from the configuration only, with fields
    computed at runtime.
    """
    foca = _foca(
        tmp_path,
        {
            "service": _service(environment=None),
            "runtime_fields": f"{FIELDS}.runtime_fields",
        },
    )
    client = foca.create_app().app.test_client()
    response = client.get("/service-info")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.json == {
        **_service(environment="port-8000"),
        "x-runtime": {},
    }
    assert client.post("/service-info").status_code == 405


def test_service_info_endpoint_invalid_at_runtime(tmp_path, monkeypatch):
    """Service info that becomes invalid at runtime is not served."""
    foca = _foca(
        tmp_path,
        {"service": SERVICE, "runtime_fields": f"{FIELDS}.version_field"},
    )
    client = foca.create_app().app.test_client()
    monkeypatch.setattr(
        foca.conf.service_info,
        "_runtime_function",
        lambda: {"createdAt": "yesterday"},
    )
    response = client.get("/service-info")
    assert response.status_code == 500
    assert response.mimetype == "application/problem+json"


def test_service_info_endpoint_not_configured(tmp_path):
    """Without service info configured, the endpoint fails."""
    client = _foca(tmp_path, None).create_app().app.test_client()
    assert client.get("/service-info").status_code == 500


def test_service_info_invalid_at_startup(tmp_path):
    """Apps with invalid service info refuse to start."""
    foca = _foca(
        tmp_path,
        {
            "service": _service(version=None),
            "runtime_fields": f"{FIELDS}.runtime_fields",
        },
    )
    with pytest.raises(InvalidServiceInfo, match="'version' is a required"):
        foca.create_app()
