"""Tests for registering access control."""

import logging

import mongomock
import pytest

from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (
    AccessControlConfig,
    CollectionConfig,
    Config,
    DBConfig,
    IndexConfig,
    MongoConfig,
)
from foca.security.access_control.register_access_control import (
    register_access_control,
)


class TestRegisterAccessControlSetup:
    """Tests for registering the access control database and API."""

    @staticmethod
    def _register(mongo_config, times=1, **access_control_kwargs):
        access_control_kwargs = {
            "db_name": "access_control_db",
            "collection_name": "policy_rules",
            **access_control_kwargs,
        }
        access_control_config = AccessControlConfig(
            **access_control_kwargs,
        )
        for _ in range(times):
            cnx_app = create_connexion_app(Config(db=mongo_config))
            register_access_control(
                cnx_app=cnx_app,
                mongo_config=mongo_config,
                access_control_config=access_control_config,
            )
            mongo_config = cnx_app.app.config.foca.db
        return mongo_config

    @staticmethod
    def _app_db_config():
        return MongoConfig(
            dbs={
                "app_db": DBConfig(
                    collections={
                        "items": CollectionConfig(
                            indexes=[IndexConfig(keys={"id": 1})]
                        ),
                    },
                ),
            },
        )

    def test_separate_db(self):
        """Access control database is added next to app databases."""
        db = self._register(self._app_db_config())
        assert set(db.dbs) == {"app_db", "access_control_db"}
        assert set(db.dbs["app_db"].collections) == {"items"}
        assert set(db.dbs["access_control_db"].collections) == {"policy_rules"}

    def test_shared_db(self):
        """Policy collection is added to app database of the same name."""
        db = self._register(self._app_db_config(), db_name="app_db")
        assert set(db.dbs) == {"app_db"}
        collections = db.dbs["app_db"].collections
        assert set(collections) == {"items", "policy_rules"}
        assert collections["items"].indexes[0].keys == [("id", 1)]
        assert collections["policy_rules"].client is not None

    def test_shared_db_registered(self):
        """Policy collection uses client of registered app database."""
        mongo_config = self._app_db_config()
        app_db = mongomock.MongoClient().db
        mongo_config.dbs["app_db"].client = app_db
        items_client = app_db["items"]
        mongo_config.dbs["app_db"].collections["items"].client = items_client
        db = self._register(mongo_config, db_name="app_db")
        collections = db.dbs["app_db"].collections
        assert collections["items"].client is items_client
        assert collections["policy_rules"].client == app_db["policy_rules"]

    def test_separate_db_repeated(self):
        """Repeated registration reuses the access control database."""
        db = self._register(self._app_db_config(), times=2)
        assert set(db.dbs) == {"app_db", "access_control_db"}
        assert set(db.dbs["app_db"].collections) == {"items"}
        assert set(db.dbs["access_control_db"].collections) == {"policy_rules"}

    def test_shared_db_repeated(self):
        """Repeated registration reuses the policy collection."""
        db = self._register(self._app_db_config(), times=2, db_name="app_db")
        collections = db.dbs["app_db"].collections
        assert set(collections) == {"items", "policy_rules"}
        assert collections["policy_rules"].client is not None

    def test_shared_db_collection_clash(self):
        """Policy collection must not be an app collection."""
        with pytest.raises(ValueError, match="items"):
            self._register(
                self._app_db_config(),
                db_name="app_db",
                collection_name="items",
            )

    def test_shared_db_without_collections(self):
        """Policy collection is added to app database without collections."""
        mongo_config = MongoConfig(dbs={"app_db": DBConfig(collections=None)})
        db = self._register(mongo_config, db_name="app_db")
        assert set(db.dbs["app_db"].collections) == {"policy_rules"}

    def test_shared_db_collection_clash_repeated(self):
        """Rejected registration does not affect repeated registration."""
        mongo_config = self._app_db_config()
        access_control_config = AccessControlConfig(
            db_name="app_db",
            collection_name="items",
        )
        for _ in range(2):
            with pytest.raises(ValueError, match="items"):
                register_access_control(
                    cnx_app=create_connexion_app(Config(db=mongo_config)),
                    mongo_config=mongo_config,
                    access_control_config=access_control_config,
                )
        items = mongo_config.dbs["app_db"].collections["items"]
        assert items.indexes[0].keys == [("id", 1)]

    def test_no_db_config(self):
        """Access control database is registered without app databases."""
        db = self._register(None)
        assert set(db.dbs) == {"access_control_db"}

    def test_logs(self, caplog):
        """Successful registration steps are logged."""
        with caplog.at_level(logging.INFO):
            self._register(None)
        assert "Casbin enforcer registered." in caplog.text
        assert "Permission management API registered" in caplog.text
