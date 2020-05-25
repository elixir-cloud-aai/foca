"""Tests for register_mongodb.py"""

from foca.database.register_mongodb import create_mongo_client

from flask import Flask
from flask_pymongo import PyMongo

DB_CONFIG_OK = ["tests/database/mock_config.yaml"]
MY_DICT = {
    "database": {
        "host": "mongodb",
        "port": "27017",
        "name": "mock_db"
    }
}


def test_create_mongo_client(monkeypatch):
    """When MONGO_USERNAME environement variable IS defined"""
    monkeypatch.setenv("MONGO_USERNAME", "TestingUser")
    app = Flask(__name__)
    res = create_mongo_client(app, MY_DICT)
    assert isinstance(res, PyMongo)


def test_create_mongo_client_env(monkeypatch):
    """When MONGO_USERNAME environment variable IS defined BUT null"""
    monkeypatch.setenv("MONGO_USERNAME", "")
    app = Flask(__name__)
    res = create_mongo_client(app, MY_DICT)
    assert isinstance(res, PyMongo)
