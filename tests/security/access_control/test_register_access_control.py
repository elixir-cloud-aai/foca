"""Tests for registering access control"""

import logging
from types import SimpleNamespace
from flask import Flask
import mongomock
from pymongo import MongoClient
from unittest import TestCase
import pytest

from foca.security.access_control.register_access_control import (
    check_permissions,
    register_access_control,
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


def test_register_access_control_logs_setup_steps(monkeypatch, caplog):
    """Test setup logs for access control registration flow."""
    logger_name = "foca.security.access_control.register_access_control"
    access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
    mongo_config = MongoConfig(**MONGO_CONFIG)

    dummy_app = SimpleNamespace(
        app=SimpleNamespace(config=SimpleNamespace(foca=SimpleNamespace(db=None)))
    )

    monkeypatch.setattr(
        "foca.security.access_control.register_access_control.add_new_database",
        lambda app, conf, db_conf, db_name: None,
    )
    monkeypatch.setattr(
        "foca.security.access_control.register_access_control.register_casbin_enforcer",
        lambda app, mongo_config, access_control_config: app,
    )
    monkeypatch.setattr(
        "foca.security.access_control.register_access_control.register_permission_specs",
        lambda app, access_control_config: app,
    )

    with caplog.at_level(logging.INFO, logger=logger_name):
        updated_app = register_access_control(
            cnx_app=dummy_app,
            mongo_config=mongo_config,
            access_control_config=access_control,
        )

    assert updated_app is dummy_app
    records = [r for r in caplog.records if r.name == logger_name]
    assert any("Access control enforcer registered." in r.getMessage() for r in records)
    assert any("Access control permission specifications registered." in r.getMessage() for r in records)
