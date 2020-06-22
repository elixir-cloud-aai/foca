"""Tests for register_mongodb.py"""

from flask import Flask
from flask_pymongo import PyMongo
from pymongo import DESCENDING

from foca.database.register_mongodb import (
    create_mongo_client,
    register_mongodb,
)
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, DBConfig, CollectionConfig

CONFIG = Config(db=DBConfig(host="mongodb", port=27017, name="mock_db"))

DB_NAME = "my_database"
COLL_NO_INDEXES = {
    "coll_1": CollectionConfig(),
    "coll_2": CollectionConfig(),
}

COLL_WITH_INDEXES = {
    "coll_index": CollectionConfig(key=[('my_id', DESCENDING)],
                                   unique=True,
                                   sparse=True)
}


def test_create_mongo_client():
    """When MONGO_USERNAME environement variable is NOT defined"""
    # app = Flask(__name__)
    app = create_connexion_app(CONFIG)
    res = create_mongo_client(app.app, CONFIG.db)
    assert isinstance(res, PyMongo)


def test_create_mongo_client_auth(monkeypatch):
    """When MONGO_USERNAME environement variable IS defined"""
    monkeypatch.setenv("MONGO_USERNAME", "TestingUser")
    # app = Flask(__name__)
    app = create_connexion_app(CONFIG)
    res = create_mongo_client(app.app, CONFIG.db)
    assert isinstance(res, PyMongo)


def test_create_mongo_client_auth_empty(monkeypatch):
    """When MONGO_USERNAME environment variable IS defined BUT null"""
    monkeypatch.setenv("MONGO_USERNAME", "")
    # app = Flask(__name__)
    app = create_connexion_app(CONFIG)
    res = create_mongo_client(app.app, CONFIG.db)
    assert isinstance(res, PyMongo)


def test_register_mongodb_no_collections():
    """Register MongoDB database without any collections"""
    # app = Flask(__name__)
    app = create_connexion_app(CONFIG)
    temp_config = CONFIG.db.collections = COLL_NO_INDEXES
    app.app.config.update(temp_config)
    res = register_mongodb(
        app=app.app
    )
    assert isinstance(res, Flask)


def test_register_mongodb_collections_with_default_index():
    """Register MongoDB with collections and default indexes"""
    # app = Flask(__name__)
    app = create_connexion_app(CONFIG)
    app.app.config.update(CONFIG)
    res = register_mongodb(
        app=app.app
    )
    assert isinstance(res, Flask)


def test_register_mongodb_collections_with_custom_index(monkeypatch):
    """Register MongoDB with collections and custom indexes"""
    monkeypatch.setattr(
        'pymongo.collection.Collection.create_indexes',
        lambda *args: None,
    )
    # app = Flask(__name__)
    app = create_connexion_app(CONFIG)
    temp_config = CONFIG.db.collections = COLL_WITH_INDEXES
    app.app.config.update(temp_config)
    res = register_mongodb(
        app=app.app
    )
    assert isinstance(res, Flask)
