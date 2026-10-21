"""Tests for `service_info.py` module."""

import logging
import logging.config
from copy import deepcopy
from datetime import date

import pytest
from flask import Flask
from werkzeug.exceptions import InternalServerError
from yaml import safe_dump

from foca import Foca
from foca.api.service_info import (
    GetServiceInfo,
    InvalidServiceInfo,
    check_service_info,
    get_service_info,
    getServiceInfo,
)
from foca.config import config_parser
from foca.config.access import set_app_config
from foca.models.config import Config
from foca.utils import service_info as utils
from tests.test_files import service_info_fields
from tests.test_files.service_info_data import FIELDS, REMOVE, SERVICE, service

OPERATION = {
    "operationId": "getServiceInfo",
    "x-openapi-router-controller": "foca.api.service_info",
    "responses": {"200": {"description": "Service info"}},
}
SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "My service", "version": "1.0.0"},
    "paths": {"/service-info": {"get": OPERATION}},
}
TOGGLED = f"{FIELDS}.toggled_fields"
# Problem details mapping `ValueError` to `400 Bad Request`
PROBLEMS = {ValueError: {"title": "Bad Request", "status": 400}}


def _app(service_info: dict) -> Flask:
    """Create a Flask app with service info configured, without checks."""
    app = Flask(__name__)
    set_app_config(app, Config(service_info=service_info))
    return app


def _foca(
    tmp_path,
    service_info: dict | None,
    spec: dict = SPEC,
    add_controller: bool = True,
    **conf,
) -> Foca:
    """Create a FOCA instance with an API serving the service info.

    Unless `add_controller` is false, another controller is added to all
    operations; the operation's own controller takes precedence.
    """
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(safe_dump(spec))
    spec_conf: dict = {"path": "openapi.yaml"}
    if add_controller:
        spec_conf["add_operation_fields"] = {
            "x-openapi-router-controller": "tests.api.controllers"
        }
    conf = {
        "server": {"port": 8000},
        "api": {"specs": [spec_conf]},
        **conf,
    }
    if service_info is not None:
        conf["service_info"] = service_info
    config_file = tmp_path / "config.yaml"
    config_file.write_text(safe_dump(conf))
    return Foca(config_file=config_file)


@pytest.fixture
def log(caplog, monkeypatch):
    """Capture logs, also after `Foca()` configured logging.

    FOCA's logging configuration replaces the handlers of the root logger,
    dropping pytest's capturing handler, so it is attached again.
    """

    def dict_config(config: dict) -> None:
        logging.config.dictConfig(config)
        logging.getLogger().addHandler(caplog.handler)

    monkeypatch.setattr(config_parser, "dictConfig", dict_config)
    caplog.set_level(logging.INFO)
    yield caplog
    logging.getLogger().removeHandler(caplog.handler)


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
    configured = service(
        contactUrl=None,
        organization={**SERVICE["organization"], "x-logo": None},
        **{"x-links": [None, {"url": "https://example.org", "title": None}]},
    )
    app = _app({"service": configured})
    with app.app_context():
        info = get_service_info()
    assert info == {
        **service(contactUrl=REMOVE),
        "x-links": [{"url": "https://example.org"}],
    }


def test_get_service_info_runtime_fields():
    """Runtime fields are computed per request, in the app context, and
    added unless configured; configured `None` values remove them.
    """
    configured = service(description=REMOVE, **{"x-runtime": None})
    app = _app(
        {"service": configured, "runtime_fields": f"{FIELDS}.runtime_fields"}
    )
    with app.app_context():
        assert get_service_info() == service(description="From runtime")
        app.config.foca.server.port = 9000
        app.config.foca.service_info.service.pop("environment")
        assert get_service_info() == service(
            description="From runtime", environment="port-9000"
        )


def test_get_service_info_runtime_version():
    """Runtime fields take precedence over the package version."""
    app = _app(
        {
            "service": service(version=REMOVE),
            "package": "foca",
            "runtime_fields": f"{FIELDS}.version_field",
        }
    )
    with app.app_context():
        assert get_service_info() == service(version="2.0.0-runtime")


def test_get_service_info_runtime_version_unset(monkeypatch):
    """A version of `None` returned at runtime counts as not set."""
    monkeypatch.setattr(utils, "version", lambda name: "3.0.0")
    monkeypatch.setitem(
        service_info_fields.TOGGLE, "fields", {"version": None}
    )
    app = _app(
        {
            "service": service(version=REMOVE),
            "package": "my-app",
            "runtime_fields": TOGGLED,
        }
    )
    with app.app_context():
        assert get_service_info() == service(version="3.0.0")


def test_get_service_info_package_version(monkeypatch):
    """The package version is served unless configured; `version: null`
    counts as not configured.
    """
    monkeypatch.setattr(utils, "version", lambda name: "3.0.0")
    for configured in (service(version=REMOVE), service(version=None)):
        app = _app({"service": configured, "package": "my-app"})
        with app.app_context():
            assert get_service_info() == service(version="3.0.0")
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


def test_get_service_info_runtime_failed(monkeypatch):
    """Exceptions raised by the runtime fields function are chained."""
    error = KeyError("missing")
    monkeypatch.setitem(service_info_fields.TOGGLE, "error", error)
    app = _app({"service": SERVICE, "runtime_fields": TOGGLED})
    with (
        app.app_context(),
        pytest.raises(
            InvalidServiceInfo,
            match=rf"Runtime fields function '{TOGGLED}' .* failed",
        ) as exc_info,
    ):
        get_service_info()
    assert exc_info.value.__cause__ is error


def test_get_service_info_runtime_not_json(monkeypatch):
    """Runtime fields must be serializable as JSON."""
    monkeypatch.setitem(
        service_info_fields.TOGGLE, "fields", {"x-date": date(2026, 10, 20)}
    )
    app = _app({"service": SERVICE, "runtime_fields": TOGGLED})
    with (
        app.app_context(),
        pytest.raises(
            InvalidServiceInfo,
            match=(
                rf"in `\(root\)` \(from `service_info.service` and "
                rf"`{TOGGLED}`\): not JSON-serializable"
            ),
        ),
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
    """The controller returns the service info, under both names."""
    app = _app({"service": SERVICE})
    with app.test_request_context():
        assert getServiceInfo() == SERVICE
    assert GetServiceInfo is getServiceInfo


def test_getServiceInfo_invalid(monkeypatch, log):
    """The controller logs errors and raises a generic server error."""
    monkeypatch.setitem(service_info_fields.TOGGLE, "error", KeyError("x"))
    app = _app({"service": SERVICE, "runtime_fields": TOGGLED})
    with (
        app.test_request_context(),
        pytest.raises(InternalServerError) as exc_info,
    ):
        getServiceInfo()
    assert isinstance(exc_info.value.__cause__, InvalidServiceInfo)
    assert "Service info not served: Runtime fields function" in log.text


def test_getServiceInfo_not_configured(log):
    """The controller fails with a generic server error without service
    info.
    """
    app = Flask(__name__)
    set_app_config(app, Config())
    with app.test_request_context(), pytest.raises(InternalServerError):
        getServiceInfo()
    assert "No service info configured" in log.text


def test_check_service_info(log):
    """Valid service info is logged."""
    check_service_info(_app({"service": SERVICE}))
    message = "Serving service info 'org.example.my-service', version '1.2.3'"
    assert message in log.text


def test_check_service_info_not_configured(log):
    """Apps without service info pass if they do not serve it."""
    app = Flask(__name__)
    set_app_config(app, Config())
    app.add_url_rule("/other", "other", lambda: "")
    check_service_info(app)
    assert "No service info configured." in log.text


def test_service_info_endpoint(tmp_path):
    """Apps serve the service info from the configuration only, with fields
    computed at runtime.
    """
    foca = _foca(
        tmp_path,
        {
            "service": service(environment=REMOVE),
            "runtime_fields": f"{FIELDS}.runtime_fields",
        },
    )
    client = foca.create_app().app.test_client()
    response = client.get("/service-info")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.json == {
        **service(environment="port-8000"),
        "x-runtime": {},
    }
    assert client.post("/service-info").status_code == 405


@pytest.mark.parametrize("log_traffic", [None, True, False])
def test_service_info_endpoint_logged(tmp_path, log, log_traffic):
    """Requests for the service info and their responses are logged, unless
    disabled via `service_info.log_traffic`.
    """
    service_info: dict = {"service": SERVICE}
    if log_traffic is not None:
        service_info["log_traffic"] = log_traffic
    foca = _foca(tmp_path, service_info)
    client = foca.create_app().app.test_client()
    assert client.get("/service-info").status_code == 200
    request = '"GET /service-info HTTP/1.1" from 127.0.0.1'
    logged = [
        f"Incoming request: {request}",
        f"Response to request {request}: dict, status 200",
    ]
    for message in logged:
        assert (message in log.text) is (log_traffic is not False)


def test_service_info_endpoint_full_operation_id(tmp_path):
    """Operations can be routed with a full `operationId`."""
    spec = deepcopy(SPEC)
    spec["paths"]["/service-info"]["get"] = {
        "operationId": "foca.api.service_info.getServiceInfo",
        "responses": OPERATION["responses"],
    }
    foca = _foca(tmp_path, {"service": SERVICE}, spec, add_controller=False)
    response = foca.create_app().app.test_client().get("/service-info")
    assert response.status_code == 200
    assert response.json == SERVICE


def test_service_info_endpoint_wes_tes_spelling(tmp_path):
    """Operations can be routed with `operationId: GetServiceInfo`, as in
    the GA4GH WES and TES specifications.
    """
    spec = deepcopy(SPEC)
    spec["paths"]["/service-info"]["get"] = {
        **OPERATION,
        "operationId": "GetServiceInfo",
    }
    response = (
        _foca(tmp_path, {"service": SERVICE}, spec)
        .create_app()
        .app.test_client()
        .get("/service-info")
    )
    assert response.status_code == 200
    assert response.json == SERVICE
    # Apps refuse to start without service info, also with this spelling
    with pytest.raises(InvalidServiceInfo, match="no service info"):
        _foca(tmp_path, None, spec).create_app()


def test_service_info_endpoint_public(tmp_path):
    """`security: []` serves the service info without authentication."""
    spec = deepcopy(SPEC)
    spec["components"] = {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "x-bearerInfoFunc": "foca.security.auth.validate_token",
            },
        },
    }
    spec["security"] = [{"bearerAuth": []}]
    spec["paths"]["/service-info"]["get"] = {**OPERATION, "security": []}
    # Other operations require authentication
    spec["paths"]["/pets"] = {
        "get": {"operationId": "listPets", "responses": OPERATION["responses"]}
    }
    foca = _foca(
        tmp_path,
        {"service": SERVICE},
        spec=spec,
        security={
            "auth": {
                "issuers": ["https://login.example.org"],
                "audience": ["my-app"],
            },
        },
    )
    client = foca.create_app().app.test_client()
    response = client.get("/service-info")
    assert response.status_code == 200
    assert response.json == SERVICE
    assert client.get("/pets").status_code == 401


def test_service_info_endpoint_invalid_at_runtime(tmp_path, monkeypatch, log):
    """Service info that becomes invalid at runtime is not served, and the
    error is logged.
    """
    foca = _foca(tmp_path, {"service": SERVICE, "runtime_fields": TOGGLED})
    client = foca.create_app().app.test_client()
    monkeypatch.setitem(
        service_info_fields.TOGGLE, "fields", {"createdAt": "yesterday"}
    )
    response = client.get("/service-info")
    assert response.status_code == 500
    assert response.mimetype == "application/problem+json"
    assert "yesterday" not in response.get_data(as_text=True)
    assert "Service info not served: Invalid service info in `createdAt`" in (
        log.text
    )


def test_service_info_endpoint_runtime_failed(tmp_path, monkeypatch, log):
    """Failures of the runtime fields function fail the request with a
    generic error, and are logged.
    """
    foca = _foca(tmp_path, {"service": SERVICE, "runtime_fields": TOGGLED})
    client = foca.create_app().app.test_client()
    monkeypatch.setitem(
        service_info_fields.TOGGLE, "error", KeyError("secret detail")
    )
    response = client.get("/service-info")
    assert response.status_code == 500
    assert "secret detail" not in response.get_data(as_text=True)
    assert f"Runtime fields function '{TOGGLED}'" in log.text


def test_service_info_endpoint_value_error_mapped(tmp_path, monkeypatch, log):
    """Requests fail with a generic 500, also if apps map `ValueError` (the
    base class of `InvalidServiceInfo`) to other problem details.
    """
    foca = _foca(
        tmp_path,
        {"service": SERVICE, "runtime_fields": TOGGLED},
        exceptions={"mapping": f"{__name__}.PROBLEMS"},
    )
    client = foca.create_app().app.test_client()
    monkeypatch.setitem(
        service_info_fields.TOGGLE, "fields", {"createdAt": "yesterday"}
    )
    response = client.get("/service-info")
    assert response.status_code == 500
    assert "Invalid service info in `createdAt`" in log.text


def test_service_info_endpoint_not_configured(tmp_path):
    """Apps serving the service info refuse to start without it."""
    foca = _foca(tmp_path, None)
    with pytest.raises(
        InvalidServiceInfo,
        match=(
            r"Routes \['/service-info'\] are served by "
            r"`foca\.api\.service_info\.getServiceInfo`, but no service info "
            r"is configured"
        ),
    ):
        foca.create_app()


def test_service_info_invalid_at_startup(tmp_path):
    """Apps with invalid service info refuse to start."""
    foca = _foca(
        tmp_path,
        {
            "service": service(version=REMOVE),
            "runtime_fields": f"{FIELDS}.runtime_fields",
        },
    )
    with pytest.raises(InvalidServiceInfo, match="'version' is a required"):
        foca.create_app()


def test_service_info_runtime_failed_at_startup(tmp_path, monkeypatch):
    """Apps whose runtime fields function fails refuse to start."""
    monkeypatch.setitem(service_info_fields.TOGGLE, "error", RuntimeError())
    foca = _foca(tmp_path, {"service": SERVICE, "runtime_fields": TOGGLED})
    with pytest.raises(InvalidServiceInfo, match="failed"):
        foca.create_app()
