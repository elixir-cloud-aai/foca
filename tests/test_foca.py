"""Tests for `foca.py` module."""

import shutil
from pathlib import Path

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
from foca.models.config import AccessControlConfig

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
ENFORCE_WITHOUT_ENABLED_CONF = (
    DIR / "conf_enforce_access_control_without_enabled.yaml"
)


def create_modified_api_conf(path, temp_dir, api_specs_in):
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


def test_foca_constructor_empty_conf():
    """Empty config."""
    with pytest.raises(ValidationError):
        Foca(config_file=EMPTY_CONF)


def test_foca_constructor_invalid_conf():
    """Invalid config file format."""
    with pytest.raises(ValueError):
        Foca(config_file=INVALID_CONF)


def test_foca_constructor_invalid_conf_log():
    """Invalid 'log' field."""
    with pytest.raises(ValidationError):
        Foca(config_file=INVALID_LOG_CONF)


def test_foca_constructor_invalid_conf_jobs():
    """Invalid 'jobs' field."""
    with pytest.raises(ValidationError):
        Foca(config_file=INVALID_JOBS_CONF)


def test_foca_constructor_invalid_conf_db():
    """Invalid 'db' field."""
    with pytest.raises(ValidationError):
        Foca(config_file=INVALID_DB_CONF)


def test_foca_create_app_output_defaults():
    """Ensure an app with FOCA's error handler, CORS and no access control
    is created; defaults only."""
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


def test_foca_create_app_error_media_type(tmp_path):
    """Ensure error responses have the configured media type."""
    conf = tmp_path / "config.yaml"
    conf.write_text(
        safe_dump({"exceptions": {"media_type": "application/json"}})
    )
    app = Foca(config_file=conf).create_app()
    response = app.app.test_client().get("/unknown")
    assert response.status_code == 404
    assert response.content_type == "application/json"
    assert response.json == {"title": "Not Found", "status": 404}


def test_foca_constructor_invalid_error_media_type(tmp_path):
    """Invalid 'exceptions.media_type' field."""
    conf = tmp_path / "config.yaml"
    conf.write_text(safe_dump({"exceptions": {"media_type": "text/plain"}}))
    with pytest.raises(ValidationError):
        Foca(config_file=conf)


def test_foca_create_app_no_access_control():
    """Ensure access control is not registered by default."""
    foca = Foca(config_file=VALID_DB_CONF)
    app = foca.create_app()
    assert set(app.app.config.foca.db.dbs) == {"my-db"}
    assert "casbin_adapter" not in app.app.config
    assert not any(
        "permissions" in rule.rule for rule in app.app.url_map.iter_rules()
    )


def test_foca_create_app_access_control():
    """Ensure access control is registered if enabled."""
    foca = Foca(config_file=VALID_ACCESS_CONTROL_CONF)
    app = foca.create_app()
    assert "test_db" in app.app.config.foca.db.dbs
    assert "casbin_adapter" in app.app.config
    assert any(
        rule.rule == "/admin/access-control/permissions"
        for rule in app.app.url_map.iter_rules()
    )


def test_foca_create_app_access_control_defaults():
    """Ensure access control is registered with defaults if only enabled."""
    foca = Foca(config_file=ACCESS_CONTROL_ENABLED_CONF)
    app = foca.create_app()
    dbs = app.app.config.foca.db.dbs
    assert set(dbs) == {"access_control_db"}
    assert set(dbs["access_control_db"].collections) == {"policy_rules"}


def test_foca_create_app_config_unchanged():
    """Ensure creating apps does not change access control settings."""
    foca = Foca(config_file=ACCESS_CONTROL_ENABLED_CONF)
    before = foca.conf.security.model_dump()
    foca.create_app()
    foca.create_app()
    assert foca.conf.security.model_dump() == before


@pytest.mark.parametrize(
    "field", ["api_controllers", "api_route", "nonexistent"]
)
def test_access_control_removed_fields(field):
    """Removed access control settings fail validation."""
    with pytest.raises(ValidationError):
        AccessControlConfig(**{field: "x"})


def test_foca_create_app_twice():
    """Ensure an app can be created repeatedly from the same instance."""
    foca = Foca(config_file=VALID_ACCESS_CONTROL_CONF)
    foca.create_app()
    app = foca.create_app()
    dbs = app.app.config.foca.db.dbs
    assert set(dbs) == {"test_db"}
    assert set(dbs["test_db"].collections) == {"test_collection"}


def test_foca_create_app_jobs():
    """Ensure a Connexion app instance is returned; valid 'jobs' field."""
    foca = Foca(config_file=JOBS_CONF)
    app = foca.create_app()
    assert app.app.config.foca.jobs == foca.conf.jobs
    assert foca.conf.jobs is not None


def test_foca_create_app_api(tmpdir):
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


def test_foca_db():
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
def test_foca_cors(config_file, enabled):
    """Ensures CORS is enabled or disabled as configured."""
    foca = Foca(config_file=config_file)
    app = foca.create_app()
    assert app.app.config.foca.security.cors.enabled is enabled
    response = app.app.test_client().get(
        "/unknown", headers={"Origin": "https://example.org"}
    )
    assert ("Access-Control-Allow-Origin" in response.headers) is enabled


def test_foca_invalid_access_control():
    """Ensures app refuses to start if access control options are set
    without enabling access control."""
    with pytest.raises(ValidationError):
        Foca(config_file=INVALID_ACCESS_CONTROL_CONF)


def test_foca_enforce_access_control_without_enabled():
    """Ensures app refuses to start if enforcement is enabled without access
    control."""
    with pytest.raises(ValidationError):
        Foca(config_file=ENFORCE_WITHOUT_ENABLED_CONF)


def test_foca_valid_access_control():
    """Ensures access control settings are set correctly."""
    foca = Foca(config_file=VALID_ACCESS_CONTROL_CONF)
    app = foca.create_app()
    my_db = app.app.config.foca.db.dbs["test_db"]
    my_coll = my_db.collections["test_collection"]
    assert isinstance(my_db.client, Database)
    assert isinstance(my_coll.client, Collection)
    adapter = app.app.config["casbin_adapter"]
    assert adapter._collection.full_name == "test_db.test_collection"


def test_foca_create_celery_app():
    """Ensure a Celery app instance is returned."""
    foca = Foca(config_file=JOBS_CONF)
    app = foca.create_celery_app()
    assert isinstance(app, Celery)
    assert app.conf.foca is foca.conf
    jobs = foca.conf.jobs
    assert app.conf.broker_url == f"pyamqp://{jobs.host}:{jobs.port}//"


def test_foca_create_celery_app_without_jobs_field():
    """Ensure that Celery app creation fails if 'jobs' field missing."""
    foca = Foca(config_file=NO_JOBS_CONF)
    with pytest.raises(ValueError):
        foca.create_celery_app()


def test_foca_one_mongo_client(mongo_clients):
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


def test_foca_relative_paths(tmp_path, monkeypatch):
    """Ensure relative paths are resolved against the config file directory,
    irrespective of the working directory."""
    app_dir = tmp_path / "app"
    (app_dir / "specs").mkdir(parents=True)
    (app_dir / "policies").mkdir()
    shutil.copy2(DIR / "openapi_3_petstore.original.yaml", app_dir / "specs")
    shutil.copy2(
        Path(AccessControlConfig().model), app_dir / "policies" / "model.conf"
    )
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
    assert any(
        rule.rule.endswith("/pets") for rule in app.app.url_map.iter_rules()
    )


def test_foca_relative_paths_without_file(tmp_path, monkeypatch):
    """Ensure relative paths are resolved against the working directory if no
    config file is used."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    foca = Foca()
    assert foca.config_dir == tmp_path
    assert foca.config_files == []
    conf = AccessControlConfig(model="model.conf", api_specs="specs.yaml")
    assert conf.model == str(tmp_path / "model.conf")
    assert conf.api_specs == str(tmp_path / "specs.yaml")


# INVALID_JOBS_CONF = DIR / "conf_invalid_jobs.yaml"
