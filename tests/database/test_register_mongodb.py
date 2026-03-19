"""Tests for register_mongodb.py"""

from flask import Flask
from flask_pymongo import PyMongo

from foca.database.register_mongodb import (
    _create_mongo_client,
    register_mongodb,
)
from foca.models.config import MongoConfig

MONGO_DICT_MIN = {
    'host': 'mongodb',
    'port': 27017,
}
DB_DICT_NO_COLL = {
    'my_db': {
        'collections': None
    }
}
DB_DICT_DEF_COLL = {
    'my_db': {
        'collections': {
            'my_collection': {
                'indexes': None,
            }
        }
    }
}
DB_DICT_CUST_COLL = {
    'my_db': {
        'collections': {
            'my_collection': {
                'indexes': [{
                    'keys': {'indexed_field': 1},
                    'options': {'sparse': False}
                }]
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
    monkeypatch.setenv("MONGO_USERNAME", 'None')
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
    monkeypatch.setenv("MONGO_USERNAME", '')
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
        'pymongo.collection.Collection.create_index',
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        'pymongo.collection.Collection.drop_indexes',
        lambda *args, **kwargs: None,
    )
    app = Flask(__name__)
    res = register_mongodb(
        app=app,
        conf=MONGO_CONFIG_CUST_COLL,
    )
    assert isinstance(res, MongoConfig)


def test__create_mongo_client_with_mongo_uri(monkeypatch):
    """When MONGO_URI environment variable is set, it should be used as-is."""
    monkeypatch.setenv("MONGO_URI", "mongodb://myhost:12345/mydb")
    app = Flask(__name__)
    res = _create_mongo_client(app)
    assert isinstance(res, PyMongo)
    assert app.config['MONGO_URI'] == "mongodb://myhost:12345/mydb"


def test__create_mongo_client_mongo_uri_precedence(monkeypatch):
    """When MONGO_URI is set alongside individual vars, MONGO_URI takes precedence."""
    monkeypatch.setenv("MONGO_URI", "mongodb://override:9999/overridedb")
    monkeypatch.setenv("MONGO_HOST", "individual_host")
    monkeypatch.setenv("MONGO_PORT", "27017")
    monkeypatch.setenv("MONGO_DBNAME", "individual_db")
    app = Flask(__name__)
    res = _create_mongo_client(app)
    assert isinstance(res, PyMongo)
    assert app.config['MONGO_URI'] == "mongodb://override:9999/overridedb"


def test__create_mongo_client_mongo_uri_empty(monkeypatch):
    """When MONGO_URI is set to empty string, fall back to component-based URI."""
    monkeypatch.setenv("MONGO_URI", "")
    app = Flask(__name__)
    res = _create_mongo_client(app)
    assert isinstance(res, PyMongo)
    assert "mongodb" in app.config['MONGO_URI']


def test__create_mongo_client_mongo_uri_warns(monkeypatch, caplog):
    """When MONGO_URI and individual vars are set, a warning should be logged."""
    import logging
    monkeypatch.setenv("MONGO_URI", "mongodb://myhost:12345/mydb")
    monkeypatch.setenv("MONGO_HOST", "should_be_ignored")
    app = Flask(__name__)
    with caplog.at_level(logging.WARNING):
        _create_mongo_client(app)
    assert "MONGO_URI is set" in caplog.text
    assert "MONGO_HOST" in caplog.text


def test__create_mongo_client_mongo_uri_with_auth(monkeypatch):
    """MONGO_URI containing credentials should be used as-is."""
    monkeypatch.setenv("MONGO_URI", "mongodb://user:pass@myhost:12345/mydb")
    app = Flask(__name__)
    res = _create_mongo_client(app)
    assert isinstance(res, PyMongo)
    assert app.config['MONGO_URI'] == "mongodb://user:pass@myhost:12345/mydb"


def test_register_mongodb_with_mongo_uri(monkeypatch):
    """Full registration flow should work when MONGO_URI is set."""
    monkeypatch.setenv("MONGO_URI", "mongodb://testhost:27017/my_db")
    app = Flask(__name__)
    res = register_mongodb(app=app, conf=MONGO_CONFIG_NO_COLL)
    assert isinstance(res, MongoConfig)
