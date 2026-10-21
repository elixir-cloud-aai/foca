"""Tests for registering access control."""

import logging
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any

import mongomock
import pytest
import yaml
from connexion import App
from pymongo.database import Database

from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (
    AccessControlConfig,
    AuthConfig,
    CollectionConfig,
    Config,
    DBConfig,
    ExceptionConfig,
    IndexConfig,
    MongoConfig,
    SecurityConfig,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_API_SPEC_PATH,
)
from foca.security.access_control.register_access_control import (
    _check_collection_clash,
    _with_error_media_type,
    register_access_control,
    register_permission_specs,
)
from tests.mock_data import MongoClients

DEFAULT_SPEC = Path(
    str(files(ACCESS_CONTROL_BASE_PATH).joinpath(DEFAULT_API_SPEC_PATH))
)


def _dbs(mongo_config: MongoConfig) -> dict[str, DBConfig]:
    """Database configurations of a MongoDB configuration."""
    assert mongo_config.dbs is not None
    return mongo_config.dbs


def _collections(dbs: dict[str, DBConfig], db_name: str) -> dict[str, CollectionConfig]:
    """Collection configurations of a database configuration."""
    collections = dbs[db_name].collections
    assert collections is not None
    return collections


def _first_index_keys(
    collection: CollectionConfig,
) -> dict[str, Any] | list[tuple[Any, ...]] | None:
    """Keys of the first index of a collection configuration."""
    assert collection.indexes is not None
    return collection.indexes[0].keys


def _content(spec: dict[str, Any], path: str, method: str, status: str) -> set[str]:
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
def test_with_error_media_type(path: str, method: str, status: str) -> None:
    """Error responses of the permissions API declare the media type."""
    default = yaml.safe_load(DEFAULT_SPEC.read_text(encoding="utf-8"))
    assert _content(default, path, method, status) == {"application/problem+json"}
    spec = _with_error_media_type(spec_path=DEFAULT_SPEC, media_type="application/json")
    assert _content(spec, path, method, status) == {"application/json"}
    assert "application/problem+json" not in str(spec)
    assert _content(spec, "/permissions", "post", "200") == {"application/json"}


@pytest.mark.parametrize("api_specs", [None, str(DEFAULT_SPEC)])
def test_register_permission_specs_without_config(
    monkeypatch: pytest.MonkeyPatch, api_specs: str | None
) -> None:
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
    monkeypatch: pytest.MonkeyPatch,
    media_type: str,
    api_specs: str | None,
    expected: type,
) -> None:
    """Only the default specification is changed.

    It is only changed for other media types than the default.
    """
    cnx_app = create_connexion_app(
        Config(exceptions=ExceptionConfig(media_type=media_type))
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


@pytest.mark.parametrize(
    "dbname_env,db_name,clashes",
    [
        # same configured database
        (None, "app_db", True),
        # other configured database, stored separately
        (None, "access_control_db", False),
        # other configured database, stored in the same database
        ("shared_db", "access_control_db", True),
    ],
)
def test_check_collection_clash(
    monkeypatch: pytest.MonkeyPatch,
    dbname_env: str | None,
    db_name: str,
    clashes: bool,
) -> None:
    """Collections clash if their databases' effective names match."""
    monkeypatch.delenv("MONGO_DBNAME", raising=False)
    if dbname_env is not None:
        monkeypatch.setenv("MONGO_DBNAME", dbname_env)
    mongo_config = MongoConfig(
        dbs={
            "app_db": DBConfig(collections={"policy_rules": CollectionConfig()}),
            "other_db": DBConfig(collections=None),
        }
    )
    if clashes:
        with pytest.raises(ValueError, match="policy_rules"):
            _check_collection_clash(mongo_config, db_name, "policy_rules")
    else:
        _check_collection_clash(mongo_config, db_name, "policy_rules")
    _check_collection_clash(mongo_config, db_name, "other")


class TestRegisterAccessControlSetup:
    """Tests for registering the access control database and API."""

    @pytest.fixture(autouse=True)
    def _mongo_clients(self, mongo_clients: MongoClients) -> None:
        """Use in-memory MongoDB clients, as indexes are created."""

    @staticmethod
    def _register(
        mongo_config: MongoConfig | None,
        times: int = 1,
        **access_control_kwargs: Any,
    ) -> MongoConfig:
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
        assert mongo_config is not None
        return mongo_config

    @staticmethod
    def _app_db_config() -> MongoConfig:
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

    def test_separate_db(self) -> None:
        """Access control database is added next to app databases."""
        dbs = _dbs(self._register(self._app_db_config()))
        assert set(dbs) == {"app_db", "access_control_db"}
        assert set(_collections(dbs, "app_db")) == {"items"}
        assert set(_collections(dbs, "access_control_db")) == {"policy_rules"}

    def test_shared_db(self) -> None:
        """Policy collection is added to app database of the same name."""
        dbs = _dbs(self._register(self._app_db_config(), db_name="app_db"))
        assert set(dbs) == {"app_db"}
        collections = _collections(dbs, "app_db")
        assert set(collections) == {"items", "policy_rules"}
        assert _first_index_keys(collections["items"]) == [("id", 1)]
        assert collections["policy_rules"].client is not None

    def test_shared_db_registered(self) -> None:
        """Policy collection uses client of registered app database."""
        mongo_config = self._app_db_config()
        app_db: Database[dict[str, Any]] = mongomock.MongoClient().db
        _dbs(mongo_config)["app_db"].client = app_db
        items_client = app_db["items"]
        _collections(_dbs(mongo_config), "app_db")["items"].client = items_client
        db = self._register(mongo_config, db_name="app_db")
        collections = _collections(_dbs(db), "app_db")
        assert collections["items"].client is items_client
        assert collections["policy_rules"].client == app_db["policy_rules"]

    def test_separate_db_repeated(self) -> None:
        """Repeated registration reuses the access control database."""
        dbs = _dbs(self._register(self._app_db_config(), times=2))
        assert set(dbs) == {"app_db", "access_control_db"}
        assert set(_collections(dbs, "app_db")) == {"items"}
        assert set(_collections(dbs, "access_control_db")) == {"policy_rules"}

    def test_shared_db_repeated(self) -> None:
        """Repeated registration reuses the policy collection."""
        db = self._register(self._app_db_config(), times=2, db_name="app_db")
        collections = _collections(_dbs(db), "app_db")
        assert set(collections) == {"items", "policy_rules"}
        assert collections["policy_rules"].client is not None

    def test_shared_db_collection_clash(self) -> None:
        """Policy collection must not be an app collection."""
        with pytest.raises(ValueError, match="items"):
            self._register(
                self._app_db_config(),
                db_name="app_db",
                collection_name="items",
            )

    def test_effective_db_collection_clash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Policy collection must not be an app collection in its database.

        That is the database that both are stored in, as set via
        `MONGO_DBNAME`.
        """
        mongo_config = MongoConfig(
            dbs={"app_db": DBConfig(collections={"policy_rules": CollectionConfig()})}
        )
        self._register(deepcopy(mongo_config))
        monkeypatch.setenv("MONGO_DBNAME", "shared_db")
        with pytest.raises(ValueError, match="'shared_db'"):
            self._register(mongo_config)

    def test_shared_db_without_collections(self) -> None:
        """Policy collection is added to app database without collections."""
        mongo_config = MongoConfig(dbs={"app_db": DBConfig(collections=None)})
        db = self._register(mongo_config, db_name="app_db")
        assert set(_collections(_dbs(db), "app_db")) == {"policy_rules"}

    def test_shared_db_collection_clash_repeated(self) -> None:
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
        items = _collections(_dbs(mongo_config), "app_db")["items"]
        assert _first_index_keys(items) == [("id", 1)]

    def test_no_db_config(self) -> None:
        """Access control database is registered without app databases."""
        db = self._register(None)
        assert set(_dbs(db)) == {"access_control_db"}

    @pytest.mark.parametrize(
        "group_mapping,warned", [(None, True), ({"a": "admin"}, False)]
    )
    def test_keep_group_subjects_without_mapping(
        self,
        caplog: pytest.LogCaptureFixture,
        group_mapping: dict[str, str] | None,
        warned: bool,
    ) -> None:
        """Setting `keep_group_subjects` without effect is warned about."""
        with caplog.at_level(logging.WARNING):
            self._register(
                None,
                claim_groups="groups",
                group_mapping=group_mapping,
                keep_group_subjects=True,
            )
        assert ("keep_group_subjects' has no effect" in caplog.text) is warned

    @pytest.mark.parametrize(
        "issuers,warned",
        [
            (["https://a.example.org/"], False),
            # duplicates are the same issuer
            (["https://a.example.org/", "https://a.example.org/"], False),
            (["https://a.example.org/", "https://b.example.org/"], True),
        ],
    )
    def test_several_issuers(
        self, caplog: pytest.LogCaptureFixture, issuers: list[str], warned: bool
    ) -> None:
        """Subjects of several issuers may collide, which is warned about."""
        config = Config(
            security=SecurityConfig(
                auth=AuthConfig(issuers=issuers, audience=["my-app"])
            )
        )
        with caplog.at_level(logging.WARNING):
            register_access_control(
                cnx_app=create_connexion_app(config),
                mongo_config=None,
                access_control_config=AccessControlConfig(),
            )
        assert ("trusted token issuers" in caplog.text) is warned

    def test_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Successful registration steps are logged."""
        with caplog.at_level(logging.INFO):
            self._register(None)
        assert "Casbin enforcer registered." in caplog.text
        assert "Permission management API registered" in caplog.text
