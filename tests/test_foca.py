"""Tests for `foca.py` module."""

from pathlib import Path
import pytest
import shutil

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
PATH_SPECS_INVALID_OPENAPI = str(DIR / "invalid_openapi_2.yaml")
EMPTY_CONF = str(DIR / "empty_conf.yaml")
INVALID_CONF = str(DIR / "invalid_conf.yaml")
VALID_CONF = str(DIR / "conf_valid.yaml")
VALID_DB_CONF = str(DIR / "conf_db.yaml")
INVALID_DB_CONF = str(DIR / "invalid_conf_db.yaml")
INVALID_LOG_CONF = str(DIR / "conf_invalid_log_level.yaml")
JOBS_CONF = str(DIR / "conf_jobs.yaml")
INVALID_JOBS_CONF = str(DIR / "conf_invalid_jobs.yaml")
API_CONF = str(DIR / "conf_api.yaml")
VALID_CORS_CONF_DISABLED = str(DIR / "conf_valid_cors_disabled.yaml")
VALID_CORS_CONF_ENABLED = str(DIR / "conf_valid_cors_enabled.yaml")


def create_modified_api_conf(path, temp_dir, api_specs_in, api_specs_out):
    """Create a copy of a configuration YAML file"""
    temp_path = Path(temp_dir) / 'temp_test.yaml'
    shutil.copy2(path, temp_path)
    with open(temp_path, 'r+') as conf_file:
        conf = safe_load(conf_file)
        conf["api"]["specs"][0]["path"] = api_specs_in
        conf["api"]["specs"][0]["path_out"] = api_specs_out
        conf_file.seek(0)
        safe_dump(conf, conf_file)
        conf_file.truncate()
    return temp_path


def test_foca_output_defaults():
    """Ensure foca() returns a Connexion app instance; defaults only."""
    foca = Foca()
    app = foca.create_app()
    assert isinstance(app, App)


def test_foca_empty_conf():
    """Ensure foca() require non-empty configuration parameters (if a field is
    present)"""
    foca = Foca(config_file=EMPTY_CONF)
    with pytest.raises(ValidationError):
        foca.create_app()


def test_foca_invalid_structure():
    """Test foca(); invalid configuration file structure."""
    foca = Foca(config_file=INVALID_CONF)
    with pytest.raises(ValueError):
        foca.create_app()


def test_foca_invalid_log():
    """Test foca(); invalid log field"""
    foca = Foca(config_file=INVALID_LOG_CONF)
    with pytest.raises(ValidationError):
        foca.create_app()


def test_foca_jobs():
    """Ensure foca() returns a Connexion app instance; valid jobs field"""
    foca = Foca(config_file=JOBS_CONF)
    app = foca.create_app()
    assert isinstance(app, App)


def test_foca_invalid_jobs():
    """Test foca(); invalid jobs field"""
    foca = Foca(config_file=INVALID_JOBS_CONF)
    with pytest.raises(ValidationError):
        foca.create_app()


def test_foca_api(tmpdir):
    """Ensure foca() returns a Connexion app instance; valid api field"""
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
    """Ensure foca() returns a Connexion app instance; valid db field"""
    foca = Foca(config_file=VALID_DB_CONF)
    app = foca.create_app()
    my_db = app.app.config.foca.db.dbs['my-db']
    my_coll = my_db.collections['my-col-1']
    assert isinstance(my_db.client, Database)
    assert isinstance(my_coll.client, Collection)
    assert isinstance(app, App)


def test_foca_invalid_db():
    """Test foca(); invalid db field"""
    foca = Foca(config_file=INVALID_DB_CONF)
    with pytest.raises(ValidationError):
        foca.create_app()


def test_foca_CORS_enabled():
    """Ensures CORS is enabled for Microservice"""
    foca = Foca(config_file=VALID_CORS_CONF_ENABLED)
    app = foca.create_app()
    assert app.app.config.foca.security.cors.enabled is True


def test_foca_CORS_disabled():
    """Ensures CORS is disabled if user explicitly mentions"""
    foca = Foca(config_file=VALID_CORS_CONF_DISABLED)
    app = foca.create_app()
    assert app.app.config.foca.security.cors.enabled is False
