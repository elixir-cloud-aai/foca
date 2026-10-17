"""Tests for register_mongodb.py"""

import logging

import pytest
from flask import Flask
from flask_pymongo import PyMongo

from foca.database.register_mongodb import (
    _create_mongo_client,
    get_mongo_uri,
    register_mongodb,
)
from foca.models.config import MongoConfig

MONGO_ENV_VARS = [
    "MONGO_URI",
    "MONGO_HOST",
    "MONGO_PORT",
    "MONGO_DBNAME",
    "MONGO_USERNAME",
    "MONGO_PASSWORD",
]


@pytest.fixture
def clean_env(monkeypatch):
    """Unset MongoDB-related environment variables."""
    for var in MONGO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestGetMongoUri:
    def test_defaults(self, clean_env):
        """URI is built from arguments."""
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://db:1234/my_db"
        )

    def test_env_vars(self, clean_env):
        """Individual environment variables override arguments."""
        clean_env.setenv("MONGO_HOST", "other")
        clean_env.setenv("MONGO_PORT", "4321")
        clean_env.setenv("MONGO_DBNAME", "other_db")
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://other:4321/other_db"
        )

    def test_credentials_escaped(self, clean_env):
        """Credentials are percent-escaped."""
        clean_env.setenv("MONGO_USERNAME", "us@r")
        clean_env.setenv("MONGO_PASSWORD", "p:w/d")
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://us%40r:p%3Aw%2Fd@db:1234/my_db"
        )

    def test_username_without_password(self, clean_env):
        """Unset password is passed as 'None', as in previous versions."""
        clean_env.setenv("MONGO_USERNAME", "user")
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://user:None@db:1234/my_db"
        )

    def test_empty_username(self, clean_env):
        """Empty username is ignored."""
        clean_env.setenv("MONGO_USERNAME", "")
        clean_env.setenv("MONGO_PASSWORD", "secret")
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://db:1234/my_db"
        )

    def test_uri(self, clean_env):
        """`MONGO_URI` takes precedence; database name is set per database."""
        clean_env.setenv(
            "MONGO_URI", "mongodb://u:p@h1:1,h2:2/?replicaSet=rs&tls=true"
        )
        clean_env.setenv("MONGO_HOST", "ignored")
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://u:p@h1:1,h2:2/my_db?replicaSet=rs&tls=true"
            "&authSource=admin"
        )

    def test_uri_without_path(self, clean_env):
        """`MONGO_URI` without path and options."""
        clean_env.setenv("MONGO_URI", "mongodb://h1:1")
        assert get_mongo_uri(db="my_db") == "mongodb://h1:1/my_db"

    def test_uri_srv(self, clean_env):
        """`MONGO_URI` with SRV scheme."""
        clean_env.setenv("MONGO_URI", "mongodb+srv://u:p@cluster.example.org")
        assert get_mongo_uri(db="my_db") == (
            "mongodb+srv://u:p@cluster.example.org/my_db?authSource=admin"
        )

    def test_uri_auth_db(self, clean_env):
        """Database in `MONGO_URI` is kept as authentication database."""
        clean_env.setenv("MONGO_URI", "mongodb://u:p@h1:1/admin?tls=true")
        assert get_mongo_uri(db="my_db") == (
            "mongodb://u:p@h1:1/my_db?tls=true&authSource=admin"
        )

    def test_uri_auth_source(self, clean_env):
        """Explicit `authSource` in `MONGO_URI` is kept."""
        clean_env.setenv("MONGO_URI", "mongodb://u:p@h1:1/admin?authSource=x")
        assert get_mongo_uri(db="my_db") == (
            "mongodb://u:p@h1:1/my_db?authSource=x"
        )

    def test_uri_without_path_no_credentials(self, clean_env):
        """No `authSource` is added without credentials."""
        clean_env.setenv("MONGO_URI", "mongodb://h1:1/?tls=true")
        assert get_mongo_uri(db="my_db") == "mongodb://h1:1/my_db?tls=true"

    def test_uri_semicolon_separator(self, clean_env):
        """Options separated by semicolons are recognized and kept."""
        clean_env.setenv("MONGO_URI", "mongodb://u:p@h1:1/admin?tls=true;w=1")
        assert get_mongo_uri(db="my_db") == (
            "mongodb://u:p@h1:1/my_db?tls=true;w=1;authSource=admin"
        )

    def test_uri_semicolon_auth_source(self, clean_env):
        """Explicit `authSource` after semicolon is kept."""
        clean_env.setenv(
            "MONGO_URI", "mongodb://u:p@h1:1/admin?tls=true;authSource=x"
        )
        assert get_mongo_uri(db="my_db") == (
            "mongodb://u:p@h1:1/my_db?tls=true;authSource=x"
        )

    def test_uri_dbname(self, clean_env):
        """`MONGO_DBNAME` overrides database name with `MONGO_URI`."""
        clean_env.setenv("MONGO_URI", "mongodb://h1:1")
        clean_env.setenv("MONGO_DBNAME", "other_db")
        assert get_mongo_uri(db="my_db") == "mongodb://h1:1/other_db"

    def test_uri_invalid_scheme(self, clean_env):
        """`MONGO_URI` with invalid scheme."""
        clean_env.setenv("MONGO_URI", "http://h1:1")
        with pytest.raises(ValueError, match="MONGO_URI"):
            get_mongo_uri(db="my_db")


def test__create_mongo_client_uri(clean_env):
    """Client is configured with URI from `MONGO_URI`."""
    clean_env.setenv("MONGO_URI", "mongodb://u:p@h1:1/?tls=false")
    app = Flask(__name__)
    res = _create_mongo_client(app=app, db="my_db")
    assert isinstance(res, PyMongo)
    assert app.config["MONGO_URI"] == (
        "mongodb://u:p@h1:1/my_db?tls=false&authSource=admin"
    )


def test__create_mongo_client_no_secrets_logged(clean_env, caplog):
    """Credentials and options are not logged."""
    clean_env.setenv(
        "MONGO_URI",
        "mongodb://u:secret@h1:1/?tlsCertificateKeyFilePassword=secret",
    )
    with caplog.at_level(logging.INFO):
        _create_mongo_client(app=Flask(__name__), db="my_db")
    assert "mongodb://h1:1/my_db" in caplog.text
    assert "secret" not in caplog.text


MONGO_DICT_MIN = {
    "host": "mongodb",
    "port": 27017,
}
DB_DICT_NO_COLL = {"my_db": {"collections": None}}
DB_DICT_DEF_COLL = {
    "my_db": {
        "collections": {
            "my_collection": {
                "indexes": None,
            }
        }
    }
}
DB_DICT_CUST_COLL = {
    "my_db": {
        "collections": {
            "my_collection": {
                "indexes": [
                    {
                        "keys": {"indexed_field": 1},
                        "options": {"sparse": False},
                    }
                ]
            }
        }
    }
}
MONGO_CONFIG_MINIMAL = MongoConfig(**MONGO_DICT_MIN, dbs=None)
MONGO_CONFIG_NO_COLL = MongoConfig(**MONGO_DICT_MIN, dbs=DB_DICT_NO_COLL)
MONGO_CONFIG_DEF_COLL = MongoConfig(**MONGO_DICT_MIN, dbs=DB_DICT_DEF_COLL)
MONGO_CONFIG_CUST_COLL = MongoConfig(**MONGO_DICT_MIN, dbs=DB_DICT_CUST_COLL)


def test__create_mongo_client(monkeypatch):
    """When MONGO_USERNAME environement variable is NOT defined"""
    monkeypatch.setenv("MONGO_USERNAME", "None")
    app = Flask(__name__)
    res = _create_mongo_client(
        app=app,
    )
    assert isinstance(res, PyMongo)


def test__create_mongo_client_auth(monkeypatch):
    """When MONGO_USERNAME environement variable IS defined"""
    monkeypatch.setenv("MONGO_USERNAME", "TestingUser")
    app = Flask(__name__)
    res = _create_mongo_client(app)
    assert isinstance(res, PyMongo)


def test__create_mongo_client_auth_empty(monkeypatch):
    """When MONGO_USERNAME environment variable IS defined but empty"""
    monkeypatch.setenv("MONGO_USERNAME", "")
    app = Flask(__name__)
    res = _create_mongo_client(app)
    assert isinstance(res, PyMongo)


def test_register_mongodb_no_database():
    """Skip MongoDB client registration"""
    app = Flask(__name__)
    res = register_mongodb(
        app=app,
        conf=MONGO_CONFIG_MINIMAL,
    )
    assert isinstance(res, MongoConfig)


def test_register_mongodb_no_collections():
    """Register MongoDB database without any collections"""
    app = Flask(__name__)
    res = register_mongodb(
        app=app,
        conf=MONGO_CONFIG_NO_COLL,
    )
    assert isinstance(res, MongoConfig)


def test_register_mongodb_def_collections():
    """Register MongoDB with collection and default index"""
    app = Flask(__name__)
    res = register_mongodb(
        app=app,
        conf=MONGO_CONFIG_DEF_COLL,
    )
    assert isinstance(res, MongoConfig)


def test_register_mongodb_cust_collections(monkeypatch):
    """Register MongoDB with collections and custom indexes"""
    monkeypatch.setattr(
        "pymongo.collection.Collection.create_index",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "pymongo.collection.Collection.drop_indexes",
        lambda *args, **kwargs: None,
    )
    app = Flask(__name__)
    res = register_mongodb(
        app=app,
        conf=MONGO_CONFIG_CUST_COLL,
    )
    assert isinstance(res, MongoConfig)
