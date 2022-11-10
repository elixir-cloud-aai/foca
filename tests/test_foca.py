"""Tests for `foca.py` module."""

from pathlib import Path
import pytest
import shutil

from celery import Celery
from connexion import App
from pydantic import ValidationError
from pymongo.collection import Collection
from pymongo.database import Database
from yaml import (
    safe_load,
    safe_dump,
)

from foca import Foca

DIR = Path(__file__).parent / "test_files"
PATH_SPECS_2_YAML_ORIGINAL = str(DIR / "openapi_2_petstore.original.yaml")
PATH_SPECS_2_YAML_MODIFIED = str(DIR / "openapi_2_petstore.modified.yaml")
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


def create_modified_api_conf(path, temp_dir, api_specs_in, api_specs_out):
    """Create a copy of a configuration YAML file."""
    temp_path = Path(temp_dir) / "temp_test.yaml"
    shutil.copy2(path, temp_path)
    with open(temp_path, "r+") as conf_file:
        conf = safe_load(conf_file)
        conf["api"]["specs"][0]["path"] = api_specs_in
        conf["api"]["specs"][0]["path_out"] = api_specs_out
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
    """Ensure a Connexion app instance is returned; defaults only."""
    foca = Foca()
    app = foca.create_app()
    assert isinstance(app, App)


def test_foca_create_app_jobs():
    """Ensure a Connexion app instance is returned; valid 'jobs' field."""
    foca = Foca(config_file=JOBS_CONF)
    app = foca.create_app()
    assert isinstance(app, App)


def test_foca_create_app_api(tmpdir):
    """Ensure a Connexion app instance is returned; valid 'api' field."""
    temp_file = create_modified_api_conf(
        API_CONF,
        tmpdir,
        PATH_SPECS_2_YAML_ORIGINAL,
        PATH_SPECS_2_YAML_MODIFIED,
    )
    foca = Foca(config_file=temp_file)
    app = foca.create_app()
    assert isinstance(app, App)


def test_foca_db():
    """Ensure a Connexion app instance is returned; valid 'db' field."""
    foca = Foca(config_file=VALID_DB_CONF)
    app = foca.create_app()
    my_db = app.app.config.foca.db.dbs["my-db"]
    my_coll = my_db.collections["my-col-1"]
    assert isinstance(my_db.client, Database)
    assert isinstance(my_coll.client, Collection)
    assert isinstance(app, App)


def test_foca_cors_config_flag_enabled():
    """Ensures CORS config flag is set correctly (enabled)."""
    foca = Foca(config_file=VALID_CORS_CONF_ENABLED)
    app = foca.create_app()
    assert app.app.config.foca.security.cors.enabled is True


def test_foca_CORS_disabled():
    """Ensures CORS config flag is set correctly (disabled)."""
    foca = Foca(config_file=VALID_CORS_CONF_DISABLED)
    app = foca.create_app()
    assert app.app.config.foca.security.cors.enabled is False


def test_foca_invalid_access_control():
    """Ensures access control is not enabled if auth flag is disabled."""
    foca = Foca(config_file=INVALID_ACCESS_CONTROL_CONF)
    app = foca.create_app()
    assert app.app.config.foca.db is None


def test_foca_valid_access_control():
    """Ensures access control settings are set correctly."""
    foca = Foca(config_file=VALID_ACCESS_CONTROL_CONF)
    app = foca.create_app()
    my_db = app.app.config.foca.db.dbs["test_db"]
    my_coll = my_db.collections["test_collection"]
    assert isinstance(my_db.client, Database)
    assert isinstance(my_coll.client, Collection)
    assert isinstance(app, App)


def test_foca_create_celery_app():
    """Ensure a Celery app instance is returned."""
    foca = Foca(config_file=JOBS_CONF)
    app = foca.create_celery_app()
    assert isinstance(app, Celery)


def test_foca_create_celery_app_without_jobs_field():
    """Ensure that Celery app creation fails if 'jobs' field missing."""
    foca = Foca(config_file=NO_JOBS_CONF)
    with pytest.raises(ValueError):
        foca.create_celery_app()


# INVALID_JOBS_CONF = DIR / "conf_invalid_jobs.yaml"
