"""Tests for registering access control."""

import logging
from copy import deepcopy
from importlib.resources import files
from pathlib import Path

import mongomock
import pytest
import yaml
from connexion import App

from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (
    AccessControlConfig,
    CollectionConfig,
    Config,
    DBConfig,
    IndexConfig,
    MongoConfig,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_API_SPEC_PATH,
)
from foca.security.access_control.register_access_control import (
    _with_error_media_type,
    register_access_control,
    register_permission_specs,
)

DEFAULT_SPEC = Path(
    str(files(ACCESS_CONTROL_BASE_PATH).joinpath(DEFAULT_API_SPEC_PATH))
)


def _content(spec: dict, path: str, method: str, status: str) -> set[str]:
    """Media types of a response of a specification."""
    return set(spec["paths"][path][method]["responses"][status]["content"])


@pytest.mark.parametrize(
    "path,method,status",
    [
        ("/permissions", "post", "400"),
        ("/permissions", "post", "401"),
        ("/permissions", "get", "500"),
        ("/permissions/{id}", "delete", "404"),
    ],
)
def test_with_error_media_type(path, method, status):
    """Error responses of the permissions API declare the media type."""
    default = yaml.safe_load(DEFAULT_SPEC.read_text(encoding="utf-8"))
    assert _content(default, path, method, status) == {
        "application/problem+json"
    }
    spec = _with_error_media_type(
        spec_path=DEFAULT_SPEC, media_type="application/json"
    )
    assert _content(spec, path, method, status) == {"application/json"}
    assert "application/problem+json" not in str(spec)
    assert _content(spec, "/permissions", "post", "200") == {
        "application/json"
    }


@pytest.mark.parametrize("api_specs", [None, str(DEFAULT_SPEC)])
def test_register_permission_specs_without_config(monkeypatch, api_specs):
    """Apps without FOCA configuration get the specification unchanged."""
    cnx_app = App(__name__)
    specs = []
    monkeypatch.setattr(
        cnx_app,
        "add_api",
        lambda specification, **kwargs: specs.append(specification),
    )
    register_permission_specs(
        app=cnx_app,
        access_control_config=AccessControlConfig(api_specs=api_specs),
    )
    assert specs == [DEFAULT_SPEC]


@pytest.mark.parametrize(
    "media_type,api_specs,expected",
    [
        ("application/problem+json", None, Path),
        ("application/json", None, dict),
        ("application/json", str(DEFAULT_SPEC), Path),
    ],
)
def test_register_permission_specs_media_type(
    monkeypatch, media_type, api_specs, expected
):
    """Only the default specification is changed, and only for other media
    types than the default.
    """
    cnx_app = create_connexion_app(
        Config(exceptions={"media_type": media_type})
    )
    specs = []
    monkeypatch.setattr(
        cnx_app,
        "add_api",
        lambda specification, **kwargs: specs.append(specification),
    )
    register_permission_specs(
        app=cnx_app,
        access_control_config=AccessControlConfig(api_specs=api_specs),
    )
    assert isinstance(specs[0], expected)
    if expected is dict:
        assert "application/problem+json" not in str(specs[0])


class TestRegisterAccessControlSetup:
    """Tests for registering the access control database and API."""

    @pytest.fixture(autouse=True)
    def _mongo_clients(self, mongo_clients):
        """Use in-memory MongoDB clients, as indexes are created."""

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

    def test_effective_db_collection_clash(self, monkeypatch):
        """Policy collection must not be an app collection in the database
        that both are stored in, as set via `MONGO_DBNAME`."""
        mongo_config = MongoConfig(
            dbs={
                "app_db": DBConfig(
                    collections={"policy_rules": CollectionConfig()}
                )
            }
        )
        self._register(deepcopy(mongo_config))
        monkeypatch.setenv("MONGO_DBNAME", "shared_db")
        with pytest.raises(ValueError, match="'shared_db'"):
            self._register(mongo_config)

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

    @pytest.mark.parametrize(
        "group_mapping,warned", [(None, True), ({"a": "admin"}, False)]
    )
    def test_keep_group_subjects_without_mapping(
        self, caplog, group_mapping, warned
    ):
        """Setting `keep_group_subjects` without effect is warned about."""
        with caplog.at_level(logging.WARNING):
            self._register(
                None,
                claim_groups="groups",
                group_mapping=group_mapping,
                keep_group_subjects=True,
            )
        assert ("keep_group_subjects' has no effect" in caplog.text) is warned

    def test_logs(self, caplog):
        """Successful registration steps are logged."""
        with caplog.at_level(logging.INFO):
            self._register(None)
        assert "Casbin enforcer registered." in caplog.text
        assert "Permission management API registered" in caplog.text
