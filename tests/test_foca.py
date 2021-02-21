"""Tests for `foca.py` module."""

import pathlib
import pytest
import shutil
import os

from connexion import App
from pydantic import ValidationError
from pymongo.collection import Collection
from pymongo.database import Database
from yaml import safe_load, safe_dump, YAMLError

from foca.foca import foca

DIR = pathlib.Path(__file__).parent / "test_files"
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
VALID_CORS_DIS_CONF = str(DIR / "conf_valid_cors_disabled.yaml")
VALID_CORS_ENA_CONF = str(DIR / "conf_valid_cors_enabled.yaml")


def create_temporary_copy(path, CONF):
    """Create a copy of a configuration YAML file"""
    temp_path = os.path.join(DIR, 'temp_test.yaml')
    shutil.copy2(path, temp_path)
    with open(temp_path, 'r+') as conf_file:
        conf = safe_load(conf_file)
        conf["api"]["specs"][0]["path"] = CONF
        conf["api"]["specs"][0]["path_out"] = PATH_SPECS_2_YAML_MODIFIED
        conf_file.seek(0)
        safe_dump(conf, conf_file)
        conf_file.truncate()
    return temp_path


def test_foca_output_defaults():
    """Ensure foca() returns a Connexion app instance; defaults only."""
    app = foca()
    assert isinstance(app, App)


def test_foca_empty_conf():
    """Ensure foca() require non-empty configuration parameters (if a field is
    present)"""
    with pytest.raises(ValidationError):
        foca(EMPTY_CONF)


def test_foca_invalid_structure():
    """Test foca(); invalid configuration file structure."""
    with pytest.raises(YAMLError):
        foca(INVALID_CONF)


def test_foca_invalid_log():
    """Test foca(); invalid log field"""
    with pytest.raises(ValidationError):
        foca(INVALID_LOG_CONF)


def test_foca_jobs():
    """Ensure foca() returns a Connexion app instance; valid jobs field"""
    app = foca(JOBS_CONF)
    assert isinstance(app, App)


def test_foca_invalid_jobs():
    """Test foca(); invalid jobs field"""
    with pytest.raises(ValidationError):
        foca(INVALID_JOBS_CONF)


def test_foca_api():
    """Ensure foca() returns a Connexion app instance; valid api field"""
    temp_file = create_temporary_copy(
        API_CONF,
        PATH_SPECS_2_YAML_ORIGINAL,
    )
    app = foca(temp_file)
    assert isinstance(app, App)
    os.remove(temp_file)


def test_foca_db():
    """Ensure foca() returns a Connexion app instance; valid db field"""
    app = foca(VALID_DB_CONF)
    my_db = app.app.config['FOCA'].db.dbs['my-db']
    my_coll = my_db.collections['my-col-1']
    assert isinstance(my_db.client, Database)
    assert isinstance(my_coll.client, Collection)
    assert isinstance(app, App)


def test_foca_invalid_db():
    """Test foca(); invalid db field"""
    with pytest.raises(ValidationError):
        foca(INVALID_DB_CONF)


def test_foca_CORS_enabled():
    """Ensures CORS is enabled for Microservice"""
    app = foca(VALID_CORS_ENA_CONF)
    assert app.app.config['FOCA'].security.cors.enabled is True


def test_foca_CORS_disabled():
    """Ensures CORS is disabled if user explicitly mentions"""
    app = foca(VALID_CORS_DIS_CONF)
    assert app.app.config['FOCA'].security.cors.enabled is False
