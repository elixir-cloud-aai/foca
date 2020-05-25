"""Tests for register_mongodb.py"""

from flask import Flask
from flask_pymongo import PyMongo
from pymongo import DESCENDING
from pymongo.operations import IndexModel

from foca.database.register_mongodb import (
    create_mongo_client,
    register_mongodb,
)

MONGO_CONFIG = {
    "database": {
        "host": "mongodb",
        "port": "27017",
        "name": "mock_db"
    }
}
DB_NAME = "my_database"
COLL_NO_INDEXES = {
    "coll_1": [],
    "coll_2": [],
}
INDEX_MODEL = IndexModel(
    [('my_id', DESCENDING)],
    unique=True,
    sparse=True
)
COLL_WITH_INDEXES = {
    "coll_index": [INDEX_MODEL],
}


def test_create_mongo_client():
    """When MONGO_USERNAME environement variable is NOT defined"""
    app = Flask(__name__)
    res = create_mongo_client(app, MONGO_CONFIG)
    assert isinstance(res, PyMongo)


def test_create_mongo_client_auth(monkeypatch):
    """When MONGO_USERNAME environement variable IS defined"""
    monkeypatch.setenv("MONGO_USERNAME", "TestingUser")
    app = Flask(__name__)
    res = create_mongo_client(app, MONGO_CONFIG)
    assert isinstance(res, PyMongo)


def test_create_mongo_client_auth_empty(monkeypatch):
    """When MONGO_USERNAME environment variable IS defined BUT null"""
    monkeypatch.setenv("MONGO_USERNAME", "")
    app = Flask(__name__)
    res = create_mongo_client(app, MONGO_CONFIG)
    assert isinstance(res, PyMongo)


def test_register_mongodb_no_collections():
    """Register MongoDB database without any collections"""
    app = Flask(__name__)
    app.config.update(MONGO_CONFIG)
    res = register_mongodb(
        app=app,
        db=DB_NAME,
    )
    assert isinstance(res, Flask)


def test_register_mongodb_collections_with_default_index():
    """Register MongoDB with collections and default indexes"""
    app = Flask(__name__)
    app.config.update(MONGO_CONFIG)
    res = register_mongodb(
        app=app,
        db=DB_NAME,
        collections=COLL_NO_INDEXES,
    )
    assert isinstance(res, Flask)


def test_register_mongodb_collections_with_custom_index(monkeypatch):
    """Register MongoDB with collections and custom indexes"""
    monkeypatch.setattr(
        'pymongo.collection.Collection.create_indexes',
        lambda *args: None,
    )
    app = Flask(__name__)
    app.config.update(MONGO_CONFIG)
    res = register_mongodb(
        app=app,
        db=DB_NAME,
        collections=COLL_WITH_INDEXES,
    )
    assert isinstance(res, Flask)
