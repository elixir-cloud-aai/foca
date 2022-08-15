"""Unit tests for endpoint controllers."""

from copy import deepcopy

from flask import Flask
from foca.models.config import (AccessControlConfig, Config, MongoConfig)
import mongomock
import pytest

from tests.mock_data import (
    ACCESS_CONTROL_CONFIG,
    MOCK_ID,
    MOCK_RULE,
    MONGO_CONFIG
)
from foca.access_control.access_control_server import (
    deletePermission,
    getPermission,
    getAllPermissions
)
from foca.errors.exceptions import NotFound


# GET /permissions/{id}
def test_getPermission():
    """Test for getting a permission rule associated with a given identifier.
    """
    app = Flask(__name__)
    app.config["FOCA"] = Config(
        db=MongoConfig(**MONGO_CONFIG),
        access_control=AccessControlConfig(**ACCESS_CONTROL_CONFIG)
    )
    mock_resp = deepcopy(MOCK_RULE)
    mock_resp["id"] = MOCK_ID
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client = mongomock.MongoClient().db.collection
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client.insert_one(mock_resp)
    del mock_resp["_id"]

    data = deepcopy(MOCK_RULE)
    data["id"] = MOCK_ID
    with app.app_context():
        res = getPermission.__wrapped__(id=MOCK_ID)
        assert res == data


def test_getPermission_NotFound():
    """Test for getting a permission rule associated with a given identifier
    when the identifier is not available.
    """
    app = Flask(__name__)
    app.config["FOCA"] = Config(
        db=MongoConfig(**MONGO_CONFIG),
        access_control=AccessControlConfig(**ACCESS_CONTROL_CONFIG)
    )
    mock_resp = deepcopy(MOCK_RULE)
    mock_resp["id"] = MOCK_ID
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client = mongomock.MongoClient().db.collection
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client.insert_one(mock_resp)
    del mock_resp["_id"]

    with app.app_context():
        with pytest.raises(NotFound):
            getPermission.__wrapped__(id=MOCK_ID + MOCK_ID)


# DELETE /permissions/{id}
def test_deletePermission():
    """Test for deleting a permission."""
    app = Flask(__name__)
    app.config["FOCA"] = Config(
        db=MongoConfig(**MONGO_CONFIG),
        access_control=AccessControlConfig(**ACCESS_CONTROL_CONFIG)
    )
    mock_resp = deepcopy(MOCK_RULE)
    mock_resp["id"] = MOCK_ID
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client = mongomock.MongoClient().db.collection
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client.insert_one(mock_resp)

    with app.app_context():
        res = deletePermission.__wrapped__(id=MOCK_ID)
        assert res == MOCK_ID


def test_deletePermission_NotFound():
    """Test `DELETE /permissions/{id}` endpoint with unavailable id."""
    app = Flask(__name__)
    app.config["FOCA"] = Config(
        db=MongoConfig(**MONGO_CONFIG),
        access_control=AccessControlConfig(**ACCESS_CONTROL_CONFIG)
    )
    mock_resp = deepcopy(MOCK_RULE)
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client = mongomock.MongoClient().db.collection
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client.insert_one(mock_resp)

    with app.app_context():
        with pytest.raises(NotFound):
            deletePermission.__wrapped__(id=MOCK_ID)


# GET /permissions
def test_getAllPermissions():
    """Test for getting a list of all available permissions; no filters
    specified.
    """
    app = Flask(__name__)
    app.config["FOCA"] = Config(
        db=MongoConfig(**MONGO_CONFIG),
        access_control=AccessControlConfig(**ACCESS_CONTROL_CONFIG)
    )
    mock_resp = deepcopy(MOCK_RULE)
    mock_resp['id'] = MOCK_ID
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client = mongomock.MongoClient().db.collection
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client.insert_one(mock_resp)

    data = deepcopy(MOCK_RULE)
    data['id'] = MOCK_ID
    with app.app_context():
        res = getAllPermissions.__wrapped__()
        assert res == [data]


def test_getAllPermissions_filters():
    """Test for getting a list of all available permissions; all defined filters
    specified.
    """
    app = Flask(__name__)
    app.config["FOCA"] = Config(
        db=MongoConfig(**MONGO_CONFIG),
        access_control=AccessControlConfig(**ACCESS_CONTROL_CONFIG)
    )
    mock_resp = deepcopy(MOCK_RULE)
    mock_resp['id'] = MOCK_ID
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client = mongomock.MongoClient().db.collection
    app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
        .client.insert_one(mock_resp)

    data = deepcopy(MOCK_RULE)
    data['id'] = MOCK_ID
    with app.app_context():
        res = getAllPermissions.__wrapped__(limit=1)
        assert res == [data]
