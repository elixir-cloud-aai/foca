"""Tests for registering access control"""

from flask import Flask
import mongomock
from pymongo import MongoClient
from unittest import TestCase
import pytest

from foca.security.access_control.register_access_control import (
    check_permissions
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.errors.exceptions import Forbidden
from foca.models.config import AccessControlConfig, Config, MongoConfig
from tests.mock_data import (
    ACCESS_CONTROL_CONFIG,
    MOCK_REQUEST,
    MONGO_CONFIG,
    MOCK_PERMISSION
)


class TestRegisterAccessControl(TestCase):
    """Test class for register access control."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db = MongoConfig(**MONGO_CONFIG)
        self.access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
        self.access_db = self.access_control.db_name
        self.access_col = self.access_control.collection_name
        self.db_port = self.db.port

    def clear_db(self):
        client = MongoClient(f"mongodb://localhost:{self.db_port}")
        client.drop_database(self.access_db)

    def setUp(self):
        self.clear_db()

    def tearDown(self):
        self.clear_db()

    def test_check_permission_allowed(self):
        """Test to check only valid user requests are permitted via
        enforcer."""
        app = Flask(__name__)
        app.config["FOCA"] = Config(
            db=self.db,
            access_control=self.access_control
        )
        app.config["FOCA"].db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config["casbin_adapter"] = Adapter(
            uri=f"mongodb://localhost:{self.db_port}/",
            dbname=self.access_db,
            collection=self.access_col
        )
        app.config["casbin_adapter"].save_policy_line(
            ptype="p",
            rule=MOCK_PERMISSION
        )
        app.config["CASBIN_MODEL"] = self.access_control.model
        app.config["CASBIN_OWNER_HEADERS"] = self.access_control.owner_headers
        app.config["CASBIN_USER_NAME_HEADERS"] = self.access_control.\
            user_headers

        @check_permissions
        def mock_func():
            return "pass"

        with app.test_request_context(
            environ_base=MOCK_REQUEST,
            headers={"X-User": "alice"}
        ):
            response = mock_func()
            assert response == "pass"

    def test_check_permission_not_allowed(self):
        """Test to check invalid user request is not allowed."""
        assert check_permissions() is not None

    def test_check_permission_allowed_casbin_permission_not_found(self):
        """Test to check only user forbidden in case permission is not
        present."""
        app = Flask(__name__)
        app.config["FOCA"] = Config(
            db=self.db,
            access_control=self.access_control
        )
        app.config["FOCA"].db.dbs[self.access_db].collections[self.access_col]\
            .client = mongomock.MongoClient().db.collection
        app.config["casbin_adapter"] = Adapter(
            uri=f"mongodb://localhost:{self.db_port}/",
            dbname=self.access_db,
            collection=self.access_col
        )
        app.config["CASBIN_MODEL"] = self.access_control.model
        app.config["CASBIN_OWNER_HEADERS"] = self.access_control.owner_headers
        app.config["CASBIN_USER_NAME_HEADERS"] = self.access_control.\
            user_headers

        @check_permissions
        def mock_func():
            return "pass"

        with app.test_request_context(
            environ_base=MOCK_REQUEST,
            headers={"X-Admin": "alice"}
        ):
            with pytest.raises(Forbidden):
                mock_func()
