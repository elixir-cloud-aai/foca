"""Unit tests for endpoint controllers."""

from copy import deepcopy
from unittest import TestCase
from flask import Flask
import mongomock
from pymongo import MongoClient
import pytest

from foca.security.access_control.access_control_server import (
    deletePermission,
    getPermission,
    getAllPermissions,
    postPermission,
    putPermission
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.errors.exceptions import BadRequest, InternalServerError, NotFound
from foca.models.config import (AccessControlConfig, Config, MongoConfig)
from tests.mock_data import (
    ACCESS_CONTROL_CONFIG,
    MOCK_ID,
    MOCK_RULE,
    MOCK_RULE_USER_INPUT_OUTPUT,
    MOCK_RULE_INVALID,
    MONGO_CONFIG
)


class BaseTestAccessControl(TestCase):
    """Base test class for access control server tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_db = None
        self.access_col = None
        self.db_port = None

    def clear_db(self):
        client = MongoClient(f"mongodb://localhost:{self.db_port}")
        client.drop_database(self.access_db)

    def setUp(self):
        self.clear_db()

    def tearDown(self):
        self.clear_db()


class TestGetPermission(BaseTestAccessControl):
    """Test class for get permission endpoint"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
        self.access_db = self.access_control.db_name
        self.access_col = self.access_control.collection_name
        self.db = MongoConfig(**MONGO_CONFIG)
        self.db_port = self.db.port

    def test_getPermission(self):
        """Test for getting a permission rule associated with a given
        identifier.
        """
        app = Flask(__name__)
        base_config = Config(db=self.db)
        base_config.security.access_control = self.access_control
        app.config.foca = base_config
        mock_resp = deepcopy(MOCK_RULE)
        mock_resp["id"] = MOCK_ID
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client.insert_one(mock_resp)
        del mock_resp["_id"]

        data = deepcopy(MOCK_RULE_USER_INPUT_OUTPUT)
        data["id"] = MOCK_ID
        with app.app_context():
            res = getPermission.__wrapped__(id=MOCK_ID)
            assert res == data

    def test_getPermission_NotFound(self):
        """Test for getting a permission rule associated with a given
        identifier when the identifier is not available.
        """
        app = Flask(__name__)
        base_config = Config(db=MongoConfig(**MONGO_CONFIG))
        base_config.security.access_control = AccessControlConfig(
            **ACCESS_CONTROL_CONFIG
        )
        app.config.foca = base_config
        mock_resp = deepcopy(MOCK_RULE)
        mock_resp["id"] = MOCK_ID
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client.insert_one(mock_resp)
        del mock_resp["_id"]

        with app.app_context():
            with pytest.raises(NotFound):
                getPermission.__wrapped__(id=MOCK_ID + MOCK_ID)


class TestDeletePermission(BaseTestAccessControl):
    """Test class for delete permission endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
        self.access_db = self.access_control.db_name
        self.access_col = self.access_control.collection_name
        self.db = MongoConfig(**MONGO_CONFIG)
        self.db_port = self.db.port

    def test_deletePermission(self):
        """Test for deleting a permission."""
        app = Flask(__name__)
        base_config = Config(db=self.db)
        base_config.security.access_control = self.access_control
        app.config.foca = base_config
        mock_resp = deepcopy(MOCK_RULE)
        mock_resp["id"] = MOCK_ID
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client.insert_one(mock_resp)

        with app.app_context():
            res = deletePermission.__wrapped__(id=MOCK_ID)
            assert res == MOCK_ID

    def test_deletePermission_NotFound(self):
        """Test `DELETE /permissions/{id}` endpoint with unavailable id."""
        app = Flask(__name__)
        base_config = Config(db=MongoConfig(**MONGO_CONFIG))
        base_config.security.access_control = AccessControlConfig(
            **ACCESS_CONTROL_CONFIG
        )
        app.config.foca = base_config
        mock_resp = deepcopy(MOCK_RULE)
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client.insert_one(mock_resp)

        with app.app_context():
            with pytest.raises(NotFound):
                deletePermission.__wrapped__(id=MOCK_ID)


class TestGetAllPermissions(BaseTestAccessControl):
    """Test class for get all permissions endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
        self.access_db = self.access_control.db_name
        self.access_col = self.access_control.collection_name
        self.db = MongoConfig(**MONGO_CONFIG)
        self.db_port = self.db.port

    def test_getAllPermissions(self):
        """Test for getting a list of all available permissions; no filters
        specified.
        """
        app = Flask(__name__)
        base_config = Config(db=MongoConfig(**MONGO_CONFIG))
        base_config.security.access_control = AccessControlConfig(
            **ACCESS_CONTROL_CONFIG
        )
        app.config.foca = base_config
        mock_resp = deepcopy(MOCK_RULE)
        mock_resp['id'] = MOCK_ID
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client.insert_one(mock_resp)

        data = deepcopy(MOCK_RULE_USER_INPUT_OUTPUT)
        data['id'] = MOCK_ID
        with app.app_context():
            res = getAllPermissions.__wrapped__()
            assert res == [data]

    def test_getAllPermissions_filters(self):
        """Test for getting a list of all available permissions; all defined filters
        specified.
        """
        app = Flask(__name__)
        base_config = Config(db=MongoConfig(**MONGO_CONFIG))
        base_config.security.access_control = AccessControlConfig(
            **ACCESS_CONTROL_CONFIG
        )
        app.config.foca = base_config
        mock_resp = deepcopy(MOCK_RULE)
        mock_resp['id'] = MOCK_ID
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client.insert_one(mock_resp)

        data = deepcopy(MOCK_RULE_USER_INPUT_OUTPUT)
        data['id'] = MOCK_ID
        with app.app_context():
            res = getAllPermissions.__wrapped__(limit=1)
            assert res == [data]


class TestPostPermission(BaseTestAccessControl):
    """Test class for post permission endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
        self.access_db = self.access_control.db_name
        self.access_col = self.access_control.collection_name
        self.db = MongoConfig(**MONGO_CONFIG)
        self.db_port = self.db.port

    def test_postPermission(self):
        """Test for creating a permission; identifier assigned by
        implementation."""
        app = Flask(__name__)
        base_config = Config(db=self.db)
        base_config.security.access_control = self.access_control
        app.config.foca = base_config
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config["casbin_adapter"] = Adapter(
            uri=f"mongodb://localhost:{self.db_port}/",
            dbname=self.access_db,
            collection=self.access_col
        )

        with app.test_request_context(json=deepcopy(MOCK_RULE)):
            res = postPermission.__wrapped__()
            assert isinstance(res, str)

    def test_postPermission_InternalServerError(self):
        """Test for creating a permission for invalid request."""
        app = Flask(__name__)
        base_config = Config(db=self.db)
        base_config.security.access_control = self.access_control
        app.config.foca = base_config
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config["casbin_adapter"] = Adapter(
            uri=f"mongodb://localhost:{self.db_port}/",
            dbname=self.access_db,
            collection=self.access_col
        )

        with app.test_request_context(json=deepcopy(MOCK_RULE_INVALID)):
            with pytest.raises(InternalServerError):
                postPermission.__wrapped__()

    def test_postPermission_BadRequest(self):
        """Test for creating a permission for invalid request payload."""
        app = Flask(__name__)
        base_config = Config(db=self.db)
        base_config.security.access_control = self.access_control
        app.config.foca = base_config
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config["casbin_adapter"] = Adapter(
            uri=f"mongodb://localhost:{self.db_port}/",
            dbname=self.access_db,
            collection=self.access_col
        )

        with app.test_request_context(json=""):
            with pytest.raises(BadRequest):
                postPermission.__wrapped__()


class TestPutPermission(BaseTestAccessControl):
    """Test class for update permission endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
        self.access_db = self.access_control.db_name
        self.access_col = self.access_control.collection_name
        self.db = MongoConfig(**MONGO_CONFIG)
        self.db_port = self.db.port

    def test_putPermission(self):
        """Test for updating a permission; identifier assigned by
        implementation."""
        app = Flask(__name__)
        base_config = Config(db=MongoConfig(**MONGO_CONFIG))
        base_config.security.access_control = AccessControlConfig(
            **ACCESS_CONTROL_CONFIG
        )
        app.config.foca = base_config
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection

        with app.test_request_context(json=deepcopy(MOCK_RULE)):
            res = putPermission.__wrapped__(id=MOCK_ID)
            assert isinstance(res, str)
            assert res == MOCK_ID

    def test_putPermission_InternalServerError(self):
        """Test for updating a permission for invalid request."""
        app = Flask(__name__)
        base_config = Config(db=MongoConfig(**MONGO_CONFIG))
        base_config.security.access_control = AccessControlConfig(
            **ACCESS_CONTROL_CONFIG
        )
        app.config.foca = base_config
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection

        with app.test_request_context(json=deepcopy(MOCK_RULE_INVALID)):
            with pytest.raises(InternalServerError):
                putPermission.__wrapped__(id=MOCK_ID)

    def test_putPermission_BadRequest(self):
        """Test for updating a permission for invalid request payload."""
        app = Flask(__name__)
        base_config = Config(db=MongoConfig(**MONGO_CONFIG))
        base_config.security.access_control = AccessControlConfig(
            **ACCESS_CONTROL_CONFIG
        )
        app.config.foca = base_config
        app.config.foca.db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection

        with app.test_request_context(json=""):
            with pytest.raises(BadRequest):
                putPermission.__wrapped__(id=MOCK_ID)
