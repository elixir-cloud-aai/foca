"""Unit tests for endpoint controllers."""

import os
from copy import deepcopy
from importlib.resources import path as resource_path
from unittest import TestCase

import mongomock
import pytest
from flask import Flask
from pymongo import MongoClient

from foca.errors.exceptions import BadRequest, NotFound
from foca.models.config import AccessControlConfig, Config, MongoConfig
from foca.security.access_control.access_control_server import (
    deletePermission,
    getAllPermissions,
    getPermission,
    postPermission,
    putPermission,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_MODEL_FILE,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from tests.mock_data import (
    ACCESS_CONTROL_CONFIG,
    MOCK_ID,
    MOCK_PERMISSION,
    MOCK_PERMISSIONS_INVALID,
    MOCK_RULE,
    MOCK_RULE_USER_INPUT_OUTPUT,
    MONGO_CONFIG,
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
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client.insert_one(mock_resp)
        del mock_resp["_id"]

        data = deepcopy(MOCK_RULE_USER_INPUT_OUTPUT)
        data["id"] = MOCK_ID
        with app.app_context():
            res = getPermission.__wrapped__(id=MOCK_ID)
            assert res == data

    def test_getPermission_extra_fields(self):
        """Test that only rule fields of stored permissions are returned."""
        app = Flask(__name__)
        base_config = Config(db=self.db)
        base_config.security.access_control = self.access_control
        app.config.foca = base_config
        mock_resp = deepcopy(MOCK_RULE)
        mock_resp["id"] = MOCK_ID
        mock_resp["foo"] = "bar"
        mock_resp["v5"] = None
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client.insert_one(mock_resp)

        data = deepcopy(MOCK_RULE_USER_INPUT_OUTPUT)
        data["id"] = MOCK_ID
        with app.app_context():
            assert getPermission.__wrapped__(id=MOCK_ID) == data
            assert getAllPermissions.__wrapped__() == [data]

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
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client.insert_one(mock_resp)
        del mock_resp["_id"]

        with app.app_context(), pytest.raises(NotFound):
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
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client.insert_one(mock_resp)

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
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client.insert_one(mock_resp)

        with app.app_context(), pytest.raises(NotFound):
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
        mock_resp["id"] = MOCK_ID
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client.insert_one(mock_resp)

        data = deepcopy(MOCK_RULE_USER_INPUT_OUTPUT)
        data["id"] = MOCK_ID
        with app.app_context():
            res = getAllPermissions.__wrapped__()
            assert res == [data]

    def test_getAllPermissions_filters(self):
        """Test for getting a list of all available permissions.

        All defined filters specified.
        """
        app = Flask(__name__)
        base_config = Config(db=MongoConfig(**MONGO_CONFIG))
        base_config.security.access_control = AccessControlConfig(
            **ACCESS_CONTROL_CONFIG
        )
        app.config.foca = base_config
        mock_resp = deepcopy(MOCK_RULE)
        mock_resp["id"] = MOCK_ID
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client.insert_one(mock_resp)

        data = deepcopy(MOCK_RULE_USER_INPUT_OUTPUT)
        data["id"] = MOCK_ID
        with app.app_context():
            res = getAllPermissions.__wrapped__(limit=1)
            assert res == [data]


class TestIncompletePermissions(TestCase):
    """Test class for reading stored rules lacking fields."""

    def test_incomplete(self):
        access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
        app = Flask(__name__)
        base_config = Config(db=MongoConfig(**MONGO_CONFIG))
        base_config.security.access_control = access_control
        app.config.foca = base_config
        collection = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[access_control.db_name].collections[
            access_control.collection_name
        ].client = collection
        collection.insert_many(
            [
                {"ptype": "p", "v0": "a", "v1": "b", "v2": "GET"},
                {"id": "noType", "v0": "a"},
                {"id": 1, "ptype": "g", "v0": "a", "v1": 2},
                {"id": "mockId", "ptype": "g", "v0": "a", "v1": "b"},
            ]
        )
        with app.app_context():
            assert getAllPermissions.__wrapped__() == [
                {
                    "policy_type": "g",
                    "rule": {"v0": "a", "v1": "b"},
                    "id": MOCK_ID,
                },
                {"policy_type": "g", "rule": {"v0": "a"}},
                {
                    "policy_type": "p",
                    "rule": {"v0": "a", "v1": "b", "v2": "GET"},
                },
            ]
            assert getPermission.__wrapped__(id=MOCK_ID)["id"] == MOCK_ID
            with pytest.raises(NotFound):
                getPermission.__wrapped__(id="noType")


class TestPostPermission(BaseTestAccessControl):
    """Test class for post permission endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
        self.access_db = self.access_control.db_name
        self.access_col = self.access_control.collection_name
        self.db = MongoConfig(**MONGO_CONFIG)
        self.db_port = self.db.port

    def _app(self) -> Flask:
        app = Flask(__name__)
        base_config = Config(db=self.db)
        base_config.security.access_control = self.access_control
        app.config.foca = base_config
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client = mongomock.MongoClient().db.collection
        app.config["CASBIN_MODEL"] = self.access_control.model
        app.config["casbin_adapter"] = Adapter(
            uri=f"mongodb://localhost:{self.db_port}/",
            dbname=self.access_db,
            collection=self.access_col,
        )
        return app

    def test_postPermission(self):
        """Test for creating a permission; identifier assigned by
        implementation."""
        app = self._app()
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            res = postPermission.__wrapped__()
            assert isinstance(res, str)
        stored = app.config["casbin_adapter"]._collection.find_one({"id": res})
        assert stored["ptype"] == "p"
        assert [stored["v0"], stored["v1"], stored["v2"]] == [
            "alice",
            "data1",
            "POST",
        ]

    def test_postPermission_unique_ids(self):
        """Test that generated identifiers are unique and follow the
        configured characters and length."""
        app = self._app()
        app.config["casbin_adapter"].id_charset = "ab"
        app.config["casbin_adapter"].id_length = 8
        ids = set()
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            for _ in range(10):
                ids.add(postPermission.__wrapped__())
        assert len(ids) == 10
        assert all(len(id) == 8 and set(id) <= {"a", "b"} for id in ids)

    def test_postPermission_BadRequest(self):
        """Test for creating a permission for invalid request payload."""
        app = self._app()
        for payload in ["", *MOCK_PERMISSIONS_INVALID]:
            with (
                self.subTest(payload=payload),
                app.test_request_context(json=deepcopy(payload)),
                pytest.raises(BadRequest),
            ):
                postPermission.__wrapped__()
        assert (
            app.config["casbin_adapter"]._collection.count_documents({}) == 0
        )


class TestPutPermission(BaseTestAccessControl):
    """Test class for update permission endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
        self.access_db = self.access_control.db_name
        self.access_col = self.access_control.collection_name
        self.db = MongoConfig(**MONGO_CONFIG)
        self.db_port = self.db.port

    def _app(self) -> Flask:
        app = Flask(__name__)
        base_config = Config(db=MongoConfig(**MONGO_CONFIG))
        base_config.security.access_control = AccessControlConfig(
            **ACCESS_CONTROL_CONFIG
        )
        app.config.foca = base_config
        collection = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs[self.access_db].collections[
            self.access_col
        ].client = collection
        app.config["CASBIN_MODEL"] = self.access_control.model
        adapter = Adapter(
            uri=f"mongodb://localhost:{self.db_port}/",
            dbname=self.access_db,
            collection=self.access_col,
        )
        adapter._collection = collection
        app.config["casbin_adapter"] = adapter
        return app

    def _collection(self, app: Flask):
        return (
            app.config.foca.db.dbs[self.access_db]
            .collections[self.access_col]
            .client
        )

    def test_putPermission(self):
        """Test for updating a permission; only rule fields are stored."""
        app = self._app()
        self._collection(app).insert_one(
            {"id": MOCK_ID, "ptype": "g", "v0": "a", "v1": "b", "foo": "bar"}
        )
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            res = putPermission.__wrapped__(id=MOCK_ID)
            assert isinstance(res, str)
            assert res == MOCK_ID
        stored = self._collection(app).find_one(
            {"id": MOCK_ID}, projection={"_id": False}
        )
        assert stored == {
            "id": MOCK_ID,
            "ptype": "p",
            "v0": "alice",
            "v1": "data1",
            "v2": "POST",
        }

    def test_putPermission_same_shape_as_post(self):
        """Test that created and updated permissions are stored alike."""
        app = self._app()
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            post_id = postPermission.__wrapped__()
            putPermission.__wrapped__(id=MOCK_ID)
        posted, put = (
            self._collection(app).find_one(
                {"id": id}, projection={"_id": False, "id": False}
            )
            for id in [post_id, MOCK_ID]
        )
        assert posted == put

    def test_putPermission_invalid_id(self):
        """Test for updating a permission with an invalid identifier."""
        app = self._app()
        for id in ["mock_id", "mock", "mockIdentifier", "mock-1"]:
            with (
                self.subTest(id=id),
                app.test_request_context(json=deepcopy(MOCK_PERMISSION)),
                pytest.raises(BadRequest),
            ):
                putPermission.__wrapped__(id=id)
        assert self._collection(app).count_documents({}) == 0

    def test_putPermission_BadRequest(self):
        """Test for updating a permission for invalid request payload."""
        app = self._app()
        for payload in ["", *MOCK_PERMISSIONS_INVALID]:
            with (
                self.subTest(payload=payload),
                app.test_request_context(json=deepcopy(payload)),
                pytest.raises(BadRequest),
            ):
                putPermission.__wrapped__(id=MOCK_ID)
        assert self._collection(app).count_documents({}) == 0


class TestModelPathResolution(TestCase):
    """Test class for checking access control model input resolution"""

    def test_no_model_input(self):
        config = AccessControlConfig()
        with resource_path(
            ACCESS_CONTROL_BASE_PATH, DEFAULT_MODEL_FILE
        ) as _path:
            default_model_path = str(_path)
        assert config.model == default_model_path

    def test_relative_path_model_input(self):
        file_path = "access_model.conf"
        config = AccessControlConfig(model=file_path)
        assert config.model == os.path.abspath(file_path)

        # testing relative paths with relative parent directory notation used
        file_path = "../../test/access_model.conf"
        config = AccessControlConfig(model=file_path)
        assert config.model == os.path.abspath(file_path)

    def test_absolute_path_model_input(self):
        file_path = "/test/access_model.conf"
        config = AccessControlConfig(model=file_path)
        assert config.model == file_path
        assert config.model == os.path.abspath(file_path)
