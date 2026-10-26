"""Tests for `foca.py` module."""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from celery import Celery
from pydantic import ValidationError
from pymongo.collection import Collection
from pymongo.database import Database
from yaml import (
    safe_dump,
    safe_load,
)

from foca import Foca
from foca.models.config import AccessControlConfig, CORSConfig
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.register_access_control import check_opt_in_roles
from tests.mock_data import MongoClients

DIR = Path(__file__).parent / "test_files"
PATH_SPECS_2_YAML_ORIGINAL = str(DIR / "openapi_2_petstore.original.yaml")
EMPTY_CONF = DIR / "empty_conf.yaml"
INVALID_CONF = DIR / "invalid_conf.yaml"
VALID_DB_CONF = DIR / "conf_db.yaml"
INVALID_DB_CONF = DIR / "invalid_conf_db.yaml"
INVALID_LOG_CONF = DIR / "conf_invalid_log_level.yaml"
JOBS_CONF = DIR / "conf_jobs.yaml"
NO_JOBS_CONF = DIR / "conf_no_jobs.yaml"
INVALID_JOBS_CONF = DIR / "conf_invalid_jobs.yaml"
API_CONF = DIR / "conf_api.yaml"
VALID_CORS_CONF_DISABLED = DIR / "conf_valid_cors_disabled.yaml"
VALID_CORS_CONF_ENABLED = DIR / "conf_valid_cors_enabled.yaml"
INVALID_ACCESS_CONTROL_CONF = DIR / "conf_invalid_access_control.yaml"
VALID_ACCESS_CONTROL_CONF = DIR / "conf_valid_access_control.yaml"
ACCESS_CONTROL_ENABLED_CONF = DIR / "conf_access_control_enabled.yaml"
ENFORCE_WITHOUT_ENABLED_CONF = DIR / "conf_enforce_access_control_without_enabled.yaml"


def create_modified_api_conf(
    path: Path, temp_dir: str | os.PathLike[str], api_specs_in: str
) -> Path:
    """Create a copy of a configuration YAML file."""
    temp_path = Path(temp_dir) / "temp_test.yaml"
    shutil.copy2(path, temp_path)
    with open(temp_path, "r+") as conf_file:
        conf = safe_load(conf_file)
        conf["api"]["specs"][0]["path"] = api_specs_in
        conf_file.seek(0)
        safe_dump(conf, conf_file)
        conf_file.truncate()
    return temp_path


def test_foca_constructor_empty_conf() -> None:
    """Empty config."""
    with pytest.raises(ValidationError):
        Foca(config_file=EMPTY_CONF)


def test_foca_constructor_invalid_conf() -> None:
    """Invalid config file format."""
    with pytest.raises(ValueError):
        Foca(config_file=INVALID_CONF)


def test_foca_constructor_invalid_conf_log() -> None:
    """Invalid 'log' field."""
    with pytest.raises(ValidationError):
        Foca(config_file=INVALID_LOG_CONF)


def test_foca_constructor_invalid_conf_jobs() -> None:
    """Invalid 'jobs' field."""
    with pytest.raises(ValidationError):
        Foca(config_file=INVALID_JOBS_CONF)


def test_foca_constructor_invalid_conf_db() -> None:
    """Invalid 'db' field."""
    with pytest.raises(ValidationError):
        Foca(config_file=INVALID_DB_CONF)


def test_foca_create_app_output_defaults() -> None:
    """Ensure an app with FOCA's defaults is created.

    The app has FOCA's error handler, CORS and no access control; defaults
    only.
    """
    foca = Foca()
    app = foca.create_app()
    assert app.app.config.foca is foca.conf
    response = app.app.test_client().get(
        "/unknown", headers={"Origin": "https://example.org"}
    )
    assert response.status_code == 404
    assert response.mimetype == "application/problem+json"
    assert response.json == {"title": "Not Found", "status": 404}
    assert "Access-Control-Allow-Origin" in response.headers
    assert "casbin_adapter" not in app.app.config


def test_foca_create_app_error_media_type(tmp_path: Path) -> None:
    """Ensure error responses have the configured media type."""
    conf = tmp_path / "config.yaml"
    conf.write_text(safe_dump({"exceptions": {"media_type": "application/json"}}))
    app = Foca(config_file=conf).create_app()
    response = app.app.test_client().get("/unknown")
    assert response.status_code == 404
    assert response.content_type == "application/json"
    assert response.json == {"title": "Not Found", "status": 404}


@pytest.mark.usefixtures("mongo_clients")
def test_foca_create_app_error_media_type_access_control(tmp_path: Path) -> None:
    """Ensure errors of the permissions API have the configured media type.

    The media type is also declared in its specification.
    """
    conf_dict = safe_load(ACCESS_CONTROL_ENABLED_CONF.read_text())
    conf_dict["exceptions"] = {"media_type": "application/json"}
    conf_dict["security"]["access_control"]["api_serve_spec"] = True
    conf = tmp_path / "config.yaml"
    conf.write_text(safe_dump(conf_dict))
    client = Foca(config_file=conf).create_app().app.test_client()
    response = client.get("/admin/access-control/permissions")
    assert response.status_code == 401
    assert response.content_type == "application/json"
    spec = client.get("/admin/access-control/openapi.json").json
    responses = spec["paths"]["/permissions"]["get"]["responses"]
    assert set(responses["401"]["content"]) == {"application/json"}


def test_foca_constructor_invalid_error_media_type(tmp_path: Path) -> None:
    """Invalid 'exceptions.media_type' field."""
    conf = tmp_path / "config.yaml"
    conf.write_text(safe_dump({"exceptions": {"media_type": "text/plain"}}))
    with pytest.raises(ValidationError):
        Foca(config_file=conf)


def test_foca_constructor_invalid_service_info(tmp_path: Path) -> None:
    """Invalid 'service_info' field, naming the invalid field."""
    conf = tmp_path / "config.yaml"
    conf.write_text(safe_dump({"service_info": {"service": {"id": "my-service"}}}))
    with pytest.raises(ValidationError, match=r"`service_info\.service`: 'name' is a"):
        Foca(config_file=conf)


def test_foca_create_app_no_access_control() -> None:
    """Ensure access control is not registered by default."""
    foca = Foca(config_file=VALID_DB_CONF)
    app = foca.create_app()
    assert set(app.app.config.foca.db.dbs) == {"my-db"}
    assert "casbin_adapter" not in app.app.config
    assert not any("permissions" in rule.rule for rule in app.app.url_map.iter_rules())


@pytest.mark.usefixtures("mongo_clients")
def test_foca_create_app_access_control() -> None:
    """Ensure access control is registered if enabled."""
    foca = Foca(config_file=VALID_ACCESS_CONTROL_CONF)
    app = foca.create_app()
    assert "test_db" in app.app.config.foca.db.dbs
    assert "casbin_adapter" in app.app.config
    assert any(
        rule.rule == "/admin/access-control/permissions"
        for rule in app.app.url_map.iter_rules()
    )


@pytest.mark.usefixtures("mongo_clients")
def test_foca_create_app_access_control_defaults() -> None:
    """Ensure access control is registered with defaults if only enabled."""
    foca = Foca(config_file=ACCESS_CONTROL_ENABLED_CONF)
    app = foca.create_app()
    dbs = app.app.config.foca.db.dbs
    assert set(dbs) == {"access_control_db"}
    assert set(dbs["access_control_db"].collections) == {"policy_rules"}


@pytest.mark.usefixtures("mongo_clients")
def test_foca_create_app_config_unchanged() -> None:
    """Ensure creating apps does not change access control settings."""
    foca = Foca(config_file=ACCESS_CONTROL_ENABLED_CONF)
    before = foca.conf.security.model_dump()
    foca.create_app()
    foca.create_app()
    assert foca.conf.security.model_dump() == before


def _access_control_conf(tmp_path: Path, **access_control: Any) -> Path:
    """Write a configuration with access control enabled."""
    conf = safe_load(ACCESS_CONTROL_ENABLED_CONF.read_text(encoding="utf-8"))
    conf["security"]["access_control"].update(access_control)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(safe_dump(conf), encoding="utf-8")
    return config_file


def test_foca_create_app_no_access_control_checks(
    log: pytest.LogCaptureFixture,
) -> None:
    """Access control settings are not checked without access control."""
    Foca(config_file=VALID_DB_CONF).create_app()
    assert not [
        record
        for record in log.records
        if record.name.startswith("foca.security.access_control")
    ]


@pytest.mark.usefixtures("mongo_clients")
def test_foca_create_app_access_control_checks(
    tmp_path: Path, log: pytest.LogCaptureFixture
) -> None:
    """Access control settings are checked when creating apps."""
    Foca(config_file=_access_control_conf(tmp_path)).create_app()
    assert "No group or service client is mapped to the role 'admin'" in log.text


@pytest.mark.usefixtures("mongo_clients")
def test_foca_create_app_require_admin(tmp_path: Path) -> None:
    """Apps refuse to start without admins with `require_admin`."""
    foca = Foca(config_file=_access_control_conf(tmp_path, require_admin=True))
    with pytest.raises(ValueError, match="require_admin"):
        foca.create_app()
    config_file = _access_control_conf(
        tmp_path,
        require_admin=True,
        claim_groups="groups",
        group_mapping={"admins": "admin"},
        keep_group_subjects=True,
    )
    app = Foca(config_file=config_file).create_app()
    assert "casbin_adapter" in app.app.config


@pytest.mark.parametrize("field", ["api_controllers", "api_route", "nonexistent"])
def test_access_control_removed_fields(field: str) -> None:
    """Removed access control settings fail validation."""
    with pytest.raises(ValidationError):
        AccessControlConfig(**{field: "x"})  # type: ignore[arg-type]  # invalid on purpose


@pytest.mark.usefixtures("mongo_clients")
def test_foca_create_app_twice() -> None:
    """Ensure an app can be created repeatedly from the same instance."""
    foca = Foca(config_file=VALID_ACCESS_CONTROL_CONF)
    foca.create_app()
    app = foca.create_app()
    dbs = app.app.config.foca.db.dbs
    assert set(dbs) == {"test_db"}
    assert set(dbs["test_db"].collections) == {"test_collection"}


def test_foca_create_app_jobs() -> None:
    """Ensure a Connexion app instance is returned; valid 'jobs' field."""
    foca = Foca(config_file=JOBS_CONF)
    app = foca.create_app()
    assert app.app.config.foca.jobs == foca.conf.jobs
    assert foca.conf.jobs is not None


def test_foca_create_app_api(tmpdir: os.PathLike[str]) -> None:
    """Ensure a Connexion app instance is returned; valid 'api' field."""
    temp_file = create_modified_api_conf(
        API_CONF,
        tmpdir,
        PATH_SPECS_2_YAML_ORIGINAL,
    )
    foca = Foca(config_file=temp_file)
    app = foca.create_app()
    rules = {rule.rule for rule in app.app.url_map.iter_rules()}
    assert {"/v1/pets", "/v1/pets/<petId>"} <= rules


def test_foca_db() -> None:
    """Ensure a Connexion app instance is returned; valid 'db' field."""
    foca = Foca(config_file=VALID_DB_CONF)
    app = foca.create_app()
    my_db = app.app.config.foca.db.dbs["my-db"]
    my_coll = my_db.collections["my-col-1"]
    assert isinstance(my_db.client, Database)
    assert isinstance(my_coll.client, Collection)
    assert (my_db.client.name, my_coll.client.name) == ("my-db", "my-col-1")


@pytest.mark.parametrize(
    "config_file,enabled",
    [(VALID_CORS_CONF_ENABLED, True), (VALID_CORS_CONF_DISABLED, False)],
)
def test_foca_cors(config_file: Path, enabled: bool) -> None:
    """Ensures CORS is enabled or disabled as configured."""
    foca = Foca(config_file=config_file)
    app = foca.create_app()
    assert app.app.config.foca.security.cors.enabled is enabled
    response = app.app.test_client().get(
        "/unknown", headers={"Origin": "https://example.org"}
    )
    assert ("Access-Control-Allow-Origin" in response.headers) is enabled


def test_foca_invalid_access_control() -> None:
    """Ensures app refuses to start with access control options only.

    Access control options are set without enabling access control.
    """
    with pytest.raises(ValidationError):
        Foca(config_file=INVALID_ACCESS_CONTROL_CONF)


def test_foca_enforce_access_control_without_enabled() -> None:
    """Ensures app refuses to start if enforcement is enabled only.

    Enforcement is enabled without access control.
    """
    with pytest.raises(ValidationError):
        Foca(config_file=ENFORCE_WITHOUT_ENABLED_CONF)


def test_foca_valid_access_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensures access control settings are set correctly."""
    # without database server, as the clients are checked
    monkeypatch.setattr(Adapter, "has_configured_policies", lambda self: False)
    foca = Foca(config_file=VALID_ACCESS_CONTROL_CONF)
    app = foca.create_app()
    my_db = app.app.config.foca.db.dbs["test_db"]
    my_coll = my_db.collections["test_collection"]
    assert isinstance(my_db.client, Database)
    assert isinstance(my_coll.client, Collection)
    adapter = app.app.config["casbin_adapter"]
    assert adapter._collection.full_name == "test_db.test_collection"


def test_foca_create_celery_app() -> None:
    """Ensure a Celery app instance is returned."""
    foca = Foca(config_file=JOBS_CONF)
    app = foca.create_celery_app()
    assert isinstance(app, Celery)
    assert app.conf.foca is foca.conf
    jobs = foca.conf.jobs
    assert app.conf.broker_url == f"pyamqp://{jobs.host}:{jobs.port}//"


def test_foca_create_celery_app_without_jobs_field() -> None:
    """Ensure that Celery app creation fails if 'jobs' field missing."""
    foca = Foca(config_file=NO_JOBS_CONF)
    with pytest.raises(ValueError):
        foca.create_celery_app()


def test_foca_one_mongo_client(
    mongo_clients: MongoClients,
) -> None:
    """Ensure app databases and the Casbin adapter share one client."""
    foca = Foca(config_file=[VALID_DB_CONF, ACCESS_CONTROL_ENABLED_CONF])
    app = foca.create_app()
    assert len(mongo_clients) == 1
    client = mongo_clients[0][1]
    dbs = app.app.config.foca.db.dbs
    assert set(dbs) == {"my-db", "access_control_db"}
    assert all(db.client.client is client for db in dbs.values())
    adapter = app.app.config["casbin_adapter"]
    assert adapter._collection.database.client is client


@pytest.mark.usefixtures("mongo_clients")
def test_foca_relative_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure relative paths are resolved against the config file directory.

    This holds irrespective of the working directory.
    """
    app_dir = tmp_path / "app"
    (app_dir / "specs").mkdir(parents=True)
    (app_dir / "policies").mkdir()
    shutil.copy2(DIR / "openapi_3_petstore.original.yaml", app_dir / "specs")
    default_model = Path(AccessControlConfig().model)  # type: ignore[arg-type]  # default model is set
    shutil.copy2(default_model, app_dir / "policies" / "model.conf")
    config_file = app_dir / "config.yaml"
    config_file.write_text(
        "api:\n"
        "  specs:\n"
        "    - path: specs/openapi_3_petstore.original.yaml\n"
        "      disable_auth: true\n"
        "      connexion:\n"
        "        resolver_error: 501\n"
        "security:\n"
        "  auth:\n"
        "    issuers: [https://login.example.org/oidc/]\n"
        "    audience: [my-app]\n"
        "  access_control:\n"
        "    enabled: true\n"
        "    model: policies/model.conf\n"
    )
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    foca = Foca(config_file=config_file)
    assert foca.config_dir == app_dir
    assert foca.conf.api.specs[0].path == [
        app_dir / "specs" / "openapi_3_petstore.original.yaml"
    ]
    model = app_dir / "policies" / "model.conf"
    assert foca.conf.security.access_control.model == str(model)
    app = foca.create_app()
    assert app.app.config["CASBIN_MODEL"] == str(model)
    assert any(rule.rule.endswith("/pets") for rule in app.app.url_map.iter_rules())


def test_foca_relative_paths_without_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure relative paths are resolved against the working directory.

    This holds if no config file is used.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    foca = Foca()
    assert foca.config_dir == tmp_path
    assert foca.config_files == []
    conf = AccessControlConfig(model="model.conf", api_specs="specs.yaml")
    assert conf.model == str(tmp_path / "model.conf")
    assert conf.api_specs == str(tmp_path / "specs.yaml")


# INVALID_JOBS_CONF = DIR / "conf_invalid_jobs.yaml"


@pytest.mark.parametrize(
    "allow_headers,warned",
    [
        (["*"], False),
        (["Authorization", "x-request-roles"], False),
        (["Authorization", "X-.*"], False),
        (["Authorization"], True),
        # names are no patterns
        (["Authorization", "X-Request-Role", "X-"], True),
    ],
)
def test_foca_opt_in_roles(
    tmp_path: Path,
    log: pytest.LogCaptureFixture,
    allow_headers: list[str],
    warned: bool,
) -> None:
    """Responses vary by the header of opt-in roles, which CORS must allow."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        safe_dump(
            {
                "security": {
                    "access_control": {
                        "claim_groups": "groups",
                        "group_mapping": {"admins": "admin"},
                        "opt_in_roles": ["admin"],
                    },
                    "cors": {"allow_headers": allow_headers},
                }
            }
        )
    )
    app = Foca(config_file=config_file).create_app()
    response = app.app.test_client().get("/unknown")
    assert "X-Request-Roles" in response.headers["Vary"]
    assert "Opt-in roles: admin (header 'X-Request-Roles')" in log.text
    assert ("is not allowed in cross-origin requests" in log.text) is warned


def test_foca_no_opt_in_roles() -> None:
    """Without opt-in roles, responses do not vary by their header."""
    app = Foca(config_file=VALID_DB_CONF).create_app()
    response = app.app.test_client().get("/unknown")
    assert "X-Request-Roles" not in response.headers.get("Vary", "")


@pytest.mark.parametrize(
    "group_mapping,unmapped",
    [
        (None, "admin, ops"),
        ({}, "admin, ops"),
        ({"admins": "admin"}, "ops"),
        ({"admins": "admin", "operators": "ops"}, None),
    ],
)
def test_check_opt_in_roles_unmapped(
    log: pytest.LogCaptureFixture,
    group_mapping: dict[str, str] | None,
    unmapped: str | None,
) -> None:
    """Opt-in roles that no group is mapped to are warned of."""
    access_control = AccessControlConfig(
        claim_groups="groups",
        group_mapping=group_mapping,
        opt_in_roles=["admin", "ops"],
    )
    check_opt_in_roles(access_control, CORSConfig())
    warnings = [
        record.getMessage() for record in log.records if record.levelname == "WARNING"
    ]
    if unmapped is None:
        assert not warnings
    else:
        assert len(warnings) == 1
        assert warnings[0].startswith(
            f"No group is mapped to the opt-in roles "
            f"('security.access_control.group_mapping'): {unmapped}."
        )


def test_foca_unmapped_opt_in_roles(
    tmp_path: Path, log: pytest.LogCaptureFixture
) -> None:
    """Apps start with opt-in roles that no group is mapped to, and warn."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        safe_dump({"security": {"access_control": {"opt_in_roles": ["admin"]}}})
    )
    app = Foca(config_file=config_file).create_app()
    assert "X-Request-Roles" in app.app.test_client().get("/unknown").headers["Vary"]
    assert "No group is mapped to the opt-in roles" in log.text


def test_check_opt_in_roles_invalid_pattern(log: pytest.LogCaptureFixture) -> None:
    """Invalid patterns of allowed headers allow no header."""
    access_control = AccessControlConfig(
        claim_groups="groups",
        group_mapping={"admins": "admin"},
        opt_in_roles=["admin"],
    )
    check_opt_in_roles(access_control, CORSConfig(allow_headers=["(invalid"]))
    assert "is not allowed in cross-origin requests" in log.text
    log.clear()
    check_opt_in_roles(
        access_control, CORSConfig(enabled=False, allow_headers=["Authorization"])
    )
    assert "is not allowed in cross-origin requests" not in log.text
