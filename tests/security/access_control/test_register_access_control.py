"""Tests for registering access control."""

import logging
import re
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import mongomock
import pytest
import yaml
from connexion import App
from pymongo.database import Database
from pymongo.errors import PyMongoError

from foca.config.access import _get_app_config
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
from foca.security.access_control import (
    register_access_control as register_access_control_module,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_API_SPEC_PATH,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.register_access_control import (
    _check_admin_role,
    _check_collection_clash,
    _check_group_config,
    _with_error_media_type,
    register_access_control,
    register_permission_specs,
)
from foca.security.auth import InvalidTokenResolver
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

    def add_api(specification: Any, **kwargs: Any) -> Any:
        specs.append(specification)
        api = mock.Mock(base_path="/test")
        api.specification.raw = {"paths": {}}
        return api

    monkeypatch.setattr(cnx_app, "add_api", add_api)
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

    def add_api(specification: Any, **kwargs: Any) -> Any:
        specs.append(specification)
        api = mock.Mock(base_path="/test")
        api.specification.raw = {"paths": {}}
        return api

    monkeypatch.setattr(cnx_app, "add_api", add_api)
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
            "app_db": DBConfig(
                collections={
                    "policy_rules": CollectionConfig(),
                    "audit_changes": CollectionConfig(),
                }
            ),
            "other_db": DBConfig(collections=None),
        }
    )
    if clashes:
        with pytest.raises(ValueError, match="policy_rules"):
            _check_collection_clash(mongo_config, db_name, "policy_rules")
    else:
        _check_collection_clash(mongo_config, db_name, "policy_rules")
    _check_collection_clash(mongo_config, db_name, "other")
    # Changes of rules are recorded in collection `<collection_name>_changes`
    if clashes:
        with pytest.raises(ValueError, match="audit_changes"):
            _check_collection_clash(mongo_config, db_name, "audit")
    else:
        _check_collection_clash(mongo_config, db_name, "audit")


# Beginnings of the messages of the startup checks
NO_ADMIN = "No group or service client is mapped to the role 'admin'"
WHITESPACE = "'security.access_control.claim_groups' has surrounding whitespace"
NO_GROUP_CLAIM = "Groups are mapped to roles"
NO_GROUP_SUBJECTS = "'security.access_control.group_mapping' is set, but"
STORED_GROUP_SUBJECTS = "Stored policy rules have groups as their subjects"
EMPTY_MAPPING = "'security.access_control.group_mapping' is set (empty)"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Messages of the warnings logged."""
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


NO_EFFECT = "'security.access_control.keep_group_subjects' has no effect"
# Parts of the messages naming the setting to change
SET_CLAIM = "set 'security.access_control.claim_groups'"
NO_CLAIM = "no group claim is set ('security.access_control.claim_groups' or"
KEEP = "'security.access_control.keep_group_subjects' is not"


@pytest.mark.parametrize(
    "conf,expected",
    [
        ({}, []),
        ({"claim_groups": "groups"}, []),
        # nothing is lost without group subjects if no rule uses them
        ({"claim_groups": "groups", "group_mapping": {"a": "admin"}}, []),
        (
            {
                "claim_groups": "groups",
                "group_mapping": {"a": "admin"},
                "policies": [["g", "user:alice", "admin"], ["p", "admin", "/*", "GET"]],
            },
            [],
        ),
        (
            {
                "claim_groups": "groups",
                "group_mapping": {"a": "admin"},
                "policies": [["g", "group:a", "admin"]],
            },
            [(NO_GROUP_SUBJECTS, KEEP)],
        ),
        (
            {
                "claim_groups": "groups",
                "group_mapping": {"a": "admin"},
                "policies": [["p", "group:a", "/*", "GET"]],
            },
            [(NO_GROUP_SUBJECTS, KEEP)],
        ),
        # also an empty mapping drops group claim values
        ({"claim_groups": "groups", "group_mapping": {}}, []),
        (
            {
                "claim_groups": "groups",
                "group_mapping": {},
                "policies": [["g", "group:a", "admin"]],
            },
            [(NO_GROUP_SUBJECTS, KEEP)],
        ),
        (
            {
                "claim_groups": "groups",
                "group_mapping": {"a": "admin"},
                "keep_group_subjects": True,
            },
            [],
        ),
        # an empty mapping without a group claim is allowed, but has no effect
        (
            {"group_mapping": {}, "keep_group_subjects": True},
            [(EMPTY_MAPPING, NO_CLAIM)],
        ),
        ({"claim_groups": "", "group_mapping": {}}, [(EMPTY_MAPPING, NO_CLAIM)]),
        (
            {"claim_groups": "groups", "keep_group_subjects": True},
            [(NO_EFFECT, "without 'security.access_control.group_mapping'")],
        ),
    ],
)
def test_check_group_config(
    caplog: pytest.LogCaptureFixture,
    conf: dict[str, Any],
    expected: list[tuple[str, str]],
) -> None:
    """Group settings that keep groups or their roles from applying are warned about.

    Messages name the setting to change, and no claim values.
    """
    with caplog.at_level(logging.WARNING):
        _check_group_config(AccessControlConfig(**conf))
    warnings = _warnings(caplog)
    assert len(warnings) == len(expected)
    for warning, (start, part) in zip(warnings, expected, strict=True):
        assert warning.startswith(start)
        assert part in warning


@pytest.mark.parametrize(
    "mapping,logged",
    [(None, False), ({"a": "admin"}, True), ({}, True)],
)
def test_check_group_config_logs_dropped_group_subjects(
    caplog: pytest.LogCaptureFixture,
    mapping: dict[str, str] | None,
    logged: bool,
) -> None:
    """Dropped group subjects are logged at `INFO` if no rule uses them."""
    with caplog.at_level(logging.INFO):
        _check_group_config(
            AccessControlConfig(claim_groups="groups", group_mapping=mapping)
        )
    infos = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and r.getMessage().startswith(NO_GROUP_SUBJECTS)
    ]
    assert bool(infos) is logged


@pytest.mark.parametrize(
    "conf,expected",
    [
        ({"claim_groups": " groups"}, (WHITESPACE, "(' groups')")),
        ({"claim_groups": "groups\n"}, (WHITESPACE, "('groups\\n')")),
        (
            {
                "claim_groups": " groups",
                "group_mapping": {"a": "admin"},
                "keep_group_subjects": True,
            },
            (WHITESPACE, "remove the whitespace"),
        ),
        (
            {"group_mapping": {"a": "admin"}, "keep_group_subjects": True},
            (NO_GROUP_CLAIM, SET_CLAIM),
        ),
        (
            {"claim_groups": "", "group_mapping": {"a": "user"}},
            (NO_GROUP_CLAIM, "or unset 'security.access_control.group_mapping'"),
        ),
    ],
)
def test_check_group_config_errors(
    conf: dict[str, Any], expected: tuple[str, str]
) -> None:
    """Group settings that keep all group mappings from applying are rejected.

    Messages name the setting to change.
    """
    with pytest.raises(ValueError) as exc_info:
        _check_group_config(AccessControlConfig(**conf))
    start, part = expected
    assert str(exc_info.value).startswith(start)
    assert part in str(exc_info.value)


def test_check_admin_role_groups(caplog: pytest.LogCaptureFixture) -> None:
    """Groups mapped to the role `admin` are logged, sorted and escaped."""
    conf = AccessControlConfig(
        claim_groups="groups",
        group_mapping={"b-admins": "admin", "a\nadmins": "admin", "users": "user"},
        require_admin=True,
    )
    with caplog.at_level(logging.INFO):
        _check_admin_role(conf)
    assert _warnings(caplog) == []
    assert "Groups mapped to the role 'admin': a\\nadmins, b-admins." in caplog.text


@pytest.mark.parametrize(
    "policies,admin",
    [
        (None, False),
        ([], False),
        ([["g", "user:alice", "admin"]], True),
        ([["g", "group:x", "superadmin"], ["g", "superadmin", "admin"]], True),
        ([["g", "user:alice", "user"]], False),
        ([["p", "admin", "/pets", "GET"]], False),
        ([["g2", "user:alice", "admin", "/pets/1"]], False),
        # group subjects are used without `group_mapping`, if a claim is set
        ([["g", "group:admins", "admin"]], False),
    ],
)
def test_check_admin_role_policies(
    caplog: pytest.LogCaptureFixture, policies: list[list[str]] | None, admin: bool
) -> None:
    """Configured global role rules granting the role `admin` count."""
    conf = AccessControlConfig(policies=policies)
    with caplog.at_level(logging.INFO):
        _check_admin_role(conf)
    assert (NO_ADMIN in caplog.text) is not admin
    assert "Groups mapped to the role 'admin'" not in caplog.text


@pytest.mark.parametrize(
    "conf,admin",
    [
        ({"claim_groups": "groups"}, True),
        ({"claim_groups": "groups", "group_mapping": {}}, False),
        (
            {
                "claim_groups": "groups",
                "group_mapping": {},
                "keep_group_subjects": True,
            },
            True,
        ),
        # no group subjects without a group claim
        ({"group_mapping": {}, "keep_group_subjects": True}, False),
    ],
)
def test_check_admin_role_group_rules(
    caplog: pytest.LogCaptureFixture, conf: dict[str, Any], admin: bool
) -> None:
    """Configured rules for groups count only if group subjects are used."""
    policies = [["g", "group:admins", "admin"]]
    with caplog.at_level(logging.WARNING):
        _check_admin_role(AccessControlConfig(policies=policies, **conf))
    assert (NO_ADMIN in caplog.text) is not admin


def test_issuer_group_claims(caplog: pytest.LogCaptureFixture) -> None:
    """Group claims of issuers count for the group checks."""
    auth = AuthConfig.model_validate(
        {
            "issuers": [
                {"issuer": "https://a.example.org", "claim_groups": "roles"},
                "https://b.example.org",
            ],
            "audience": ["my-app"],
        }
    )
    conf = AccessControlConfig(
        group_mapping={"a": "user"},
        keep_group_subjects=True,
        policies=[["g", "group:admins", "admin"]],
    )
    with caplog.at_level(logging.WARNING):
        _check_group_config(conf, auth)
        _check_admin_role(conf, auth)
    assert _warnings(caplog) == []
    # without issuers, only the global group claim counts
    with pytest.raises(ValueError, match="no group claim"):
        _check_group_config(conf, AuthConfig())


@pytest.mark.parametrize(
    "claim_groups,warned",
    [
        # issuers' own claims count
        ("roles", False),
        # no group claim anywhere
        (None, True),
        ("", True),
    ],
)
def test_issuer_group_claims_empty_mapping(
    caplog: pytest.LogCaptureFixture, claim_groups: str | None, warned: bool
) -> None:
    """Empty group mappings without any group claim are warned about."""
    issuer: dict[str, Any] = {"issuer": "https://a.example.org"}
    if claim_groups is not None:
        issuer["claim_groups"] = claim_groups
    auth = AuthConfig.model_validate({"issuers": [issuer], "audience": ["my-app"]})
    conf = AccessControlConfig(
        group_mapping={},
        keep_group_subjects=True,
        policies=[["g", "group:admins", "admin"]],
    )
    with caplog.at_level(logging.WARNING):
        _check_group_config(conf, auth)
        _check_admin_role(conf, auth)
    assert (EMPTY_MAPPING in caplog.text) is warned
    # group subjects count for `admin` only with a group claim
    assert (NO_ADMIN in caplog.text) is warned
    # the message names group claims of issuers, too
    assert ("'security.auth.issuers[].claim_groups' set" in caplog.text) is warned


def test_check_admin_role_clients(caplog: pytest.LogCaptureFixture) -> None:
    """Service clients mapped to the role `admin` count, and are logged."""
    conf = AccessControlConfig(
        client_mapping={"ops": "admin", "gateway": "user"}, require_admin=True
    )
    with caplog.at_level(logging.INFO):
        _check_admin_role(conf)
    assert _warnings(caplog) == []
    assert "Service clients mapped to the role 'admin': ops." in caplog.text


def test_check_admin_role_missing(caplog: pytest.LogCaptureFixture) -> None:
    """Missing admins are warned about; roles of stored rules still apply."""
    conf = AccessControlConfig(claim_groups="groups", group_mapping={"a": "user"})
    with caplog.at_level(logging.WARNING):
        _check_admin_role(conf)
    (warning,) = _warnings(caplog)
    assert warning.startswith(NO_ADMIN)
    assert "'security.access_control.group_mapping'" in warning
    assert "'security.access_control.client_mapping'" in warning
    assert "'security.access_control.policies'" in warning
    assert "granted via stored policy rules" in warning
    assert "still apply" in warning


def test_check_admin_role_required() -> None:
    """Missing admins are an error with `require_admin`."""
    conf = AccessControlConfig(require_admin=True)
    with pytest.raises(ValueError, match=NO_ADMIN) as exc_info:
        _check_admin_role(conf)
    assert "unset 'require_admin'" in str(exc_info.value)
    assert "stored policy rules then still apply" in str(exc_info.value)


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
            mongo_config = _get_app_config(cnx_app.app).db
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

    def test_unenforced_role_controller(self, tmp_path: Path) -> None:
        """Unenforced routes to the role controllers are refused early.

        Permissions APIs routing such operations are refused before
        configured policies are stored.
        """
        spec = {
            "openapi": "3.0.2",
            "info": {"title": "Permissions", "version": "1.0.0"},
            "servers": [{"url": "/admin"}],
            "paths": {
                "/tasks/{id}/permissions": {
                    "get": {
                        "operationId": (
                            "foca.security.access_control.ownership_server."
                            "getResourceRoles"
                        ),
                        "x-foca-self-service": True,
                        "parameters": [
                            {
                                "name": "id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(yaml.safe_dump(spec))
        cnx_app = create_connexion_app(Config())
        with pytest.raises(ValueError, match="getResourceRoles"):
            register_access_control(
                cnx_app=cnx_app,
                mongo_config=None,
                access_control_config=AccessControlConfig(
                    api_specs=str(spec_file),
                    self_service=True,
                    policies=[["p", "*", "/tasks", "GET"]],
                ),
            )
        assert "casbin_adapter" not in cnx_app.app.config

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
        "issuers,claim_groups,warned",
        [
            (["https://a.example.org/"], "groups", False),
            # duplicates are the same issuer
            (["https://a.example.org/", "https://a.example.org/"], "groups", False),
            (["https://a.example.org/", "https://b.example.org/"], "groups", True),
            # no groups at all, or from one issuer only
            (["https://a.example.org/", "https://b.example.org/"], None, False),
            (
                [
                    "https://a.example.org/",
                    {"issuer": "https://b.example.org/", "claim_groups": ""},
                ],
                "groups",
                False,
            ),
            (
                [
                    {"issuer": "https://a.example.org/", "claim_groups": "roles"},
                    "https://b.example.org/",
                ],
                None,
                False,
            ),
        ],
    )
    def test_several_issuers(
        self,
        caplog: pytest.LogCaptureFixture,
        issuers: list[Any],
        claim_groups: str | None,
        warned: bool,
    ) -> None:
        """Group claim values of several issuers may collide (warned about)."""
        config = Config(
            security=SecurityConfig(
                auth=AuthConfig.model_validate(
                    {"issuers": issuers, "audience": ["my-app"]}
                )
            )
        )
        with caplog.at_level(logging.WARNING):
            register_access_control(
                cnx_app=create_connexion_app(config),
                mongo_config=None,
                access_control_config=AccessControlConfig(claim_groups=claim_groups),
            )
        assert ("trusted token issuers" in caplog.text) is warned
        if warned:
            assert "subjects are namespaced by issuer" in caplog.text
            assert "group claim values are not" in caplog.text

    def test_require_admin(self) -> None:
        """Apps refuse to start without admins before registering anything."""
        mongo_config = self._app_db_config()
        with pytest.raises(ValueError, match=NO_ADMIN):
            self._register(mongo_config, require_admin=True)
        assert set(_dbs(mongo_config)) == {"app_db"}

    @pytest.mark.parametrize(
        "conf,match",
        [
            ({"claim_groups": "groups "}, WHITESPACE),
            ({"group_mapping": {"admins": "admin"}}, NO_GROUP_CLAIM),
        ],
    )
    def test_group_config_errors(self, conf: dict[str, Any], match: str) -> None:
        """Apps refuse to start with group mappings that cannot apply.

        No access control database is registered before.
        """
        mongo_config = self._app_db_config()
        with pytest.raises(ValueError, match=re.escape(match)):
            self._register(mongo_config, **conf)
        assert set(_dbs(mongo_config)) == {"app_db"}

    def test_group_config_checked_first(self, caplog: pytest.LogCaptureFixture) -> None:
        """Groups mapped to `admin` without a group claim don't count as admins.

        The group settings are checked before the admins, which count mapped
        groups whatever the group claim.
        """
        with (
            caplog.at_level(logging.INFO),
            pytest.raises(ValueError, match=NO_GROUP_CLAIM),
        ):
            self._register(None, group_mapping={"admins": "admin"}, require_admin=True)
        assert "Groups mapped to the role 'admin'" not in caplog.text

    def test_startup_checks(self, caplog: pytest.LogCaptureFixture) -> None:
        """Group settings and admins are checked on registration."""
        with caplog.at_level(logging.WARNING):
            self._register(
                None,
                claim_groups="groups",
                group_mapping={"a": "user"},
                policies=[["g", "group:a", "user"]],
            )
        warnings = _warnings(caplog)
        assert any(warning.startswith(NO_ADMIN) for warning in warnings)
        assert any(warning.startswith(NO_GROUP_SUBJECTS) for warning in warnings)

    @pytest.mark.parametrize(
        "conf,warned",
        [
            ({"claim_groups": "groups", "group_mapping": {"a": "user"}}, True),
            # the mapping doesn't drop them
            ({"claim_groups": "groups"}, False),
            (
                {
                    "claim_groups": "groups",
                    "group_mapping": {"a": "user"},
                    "keep_group_subjects": True,
                },
                False,
            ),
            # warned about once, for the configured rule
            (
                {
                    "claim_groups": "groups",
                    "group_mapping": {"a": "user"},
                    "policies": [["g", "group:b", "user"]],
                },
                False,
            ),
        ],
    )
    def test_stored_group_subjects(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        conf: dict[str, Any],
        warned: bool,
    ) -> None:
        """Stored rules for groups that won't apply are warned about."""
        monkeypatch.setattr(Adapter, "has_stored_group_subjects", lambda self: True)
        with caplog.at_level(logging.WARNING):
            self._register(None, **conf)
        warnings = [w for w in _warnings(caplog) if w.startswith(STORED_GROUP_SUBJECTS)]
        assert bool(warnings) is warned

    def test_stored_group_subjects_none(self, caplog: pytest.LogCaptureFixture) -> None:
        """Without stored rules for groups, there is no warning."""
        with caplog.at_level(logging.WARNING):
            self._register(None, claim_groups="groups", group_mapping={"a": "user"})
        assert not [w for w in _warnings(caplog) if w.startswith(STORED_GROUP_SUBJECTS)]

    def test_stored_group_subjects_unknown(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Startup continues if stored rules cannot be checked."""

        def fail(self: Adapter) -> bool:
            raise PyMongoError("unavailable")

        monkeypatch.setattr(Adapter, "has_stored_group_subjects", fail)
        with caplog.at_level(logging.WARNING):
            self._register(None, claim_groups="groups", group_mapping={"a": "user"})
        assert any(
            w.startswith("Could not check for stored policy rules")
            for w in _warnings(caplog)
        )

    def test_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Successful registration steps are logged."""
        with caplog.at_level(logging.INFO):
            self._register(None)
        assert "Casbin enforcer registered." in caplog.text
        assert "Permission management API registered" in caplog.text


@pytest.mark.parametrize("reject", [True, False])
def test_register_permission_specs_invalid_tokens(
    monkeypatch: pytest.MonkeyPatch, reject: bool
) -> None:
    """The permissions API rejects invalid tokens like the app's APIs do."""
    cnx_app = create_connexion_app(
        Config.model_validate(
            {
                "security": {
                    "auth": {
                        "issuers": ["https://idp.example.org"],
                        "audience": ["foca"],
                        "reject_invalid_tokens": reject,
                    }
                }
            }
        )
    )
    add_api = MagicMock()
    monkeypatch.setattr(cnx_app, "add_api", add_api)
    register_permission_specs(app=cnx_app, access_control_config=AccessControlConfig())
    resolver = add_api.call_args.kwargs["resolver"]
    assert isinstance(resolver, InvalidTokenResolver) is reject


class TestWarnDefaultBasePath:
    """Tests for the warning about rules for the former path of the API."""

    @pytest.mark.parametrize(
        ("base_path", "rule", "warned"),
        [
            ("/acl", ["p", "admin", "/admin/access-control/policies", "GET"], True),
            ("/acl", ["p", "admin", "/admin/*", "GET"], True),
            ("/acl", ["p", "admin", "/*", "GET"], False),
            ("/acl", ["p", "/admin/access-control/x", "/tasks", "GET"], False),
            ("/acl", ["g", "alice", "/admin/access-control/x"], False),
            ("/acl", ["p", "admin"], False),
            (
                "/admin/access-control/v2",
                ["p", "admin", "/admin/access-control/policies", "GET"],
                True,
            ),
            (
                "/admin/access-control/v2",
                ["p", "admin", "/admin/access-control/*", "GET"],
                False,
            ),
        ],
    )
    def test_rules(
        self,
        caplog: pytest.LogCaptureFixture,
        base_path: str,
        rule: list[str],
        warned: bool,
    ) -> None:
        """Only rules whose object applied to the former path are named."""
        conf = AccessControlConfig.model_construct(
            api_enabled=True, api_specs=None, api_base_path=base_path, policies=[rule]
        )
        register_access_control_module._warn_default_base_path(conf)
        assert ("names the default path" in caplog.text) is warned


ISSUER = "https://login.example.org/oidc/"
UNKNOWN_USERS = "Configured policy rules at positions"
STORED_UNKNOWN_USERS = "Stored policy rules name users without the namespace"


def _auth(*issuers: Any) -> AuthConfig:
    """Get authentication settings trusting issuers.

    Args:
        *issuers: Trusted issuers, as URLs or settings.

    Returns:
        Authentication settings.
    """
    return AuthConfig.model_validate({"issuers": list(issuers), "audience": ["foca"]})


class TestPolicyUsers:
    """Tests for the checks of users named in rules."""

    @pytest.fixture(autouse=True)
    def _mongo_clients(self, mongo_clients: MongoClients) -> None:
        """Use in-memory MongoDB clients, as indexes are created."""

    @pytest.mark.parametrize(
        "issuers,policies,positions",
        [
            # bare users of the default namespace, the issuer
            ([ISSUER], [["g", "user:alice", "admin"]], [0]),
            (
                [ISSUER],
                [
                    ["p", "*", "/x", "GET"],
                    ["g", f"user:{ISSUER}|alice", "admin"],
                    ["g2", "user:bob", "owner", "/x"],
                    ["p", "group:a", "/x", "GET"],
                ],
                [2],
            ),
            ([{"issuer": ISSUER, "namespace": "ls"}], [["g", "user:ls|a", "x"]], []),
            # an issuer keeps its subjects bare
            ([{"issuer": ISSUER, "namespace": ""}], [["g", "user:a|b", "x"]], []),
            (
                [{"issuer": ISSUER, "namespace": ""}, "https://other.example.org/"],
                [["g", "user:alice", "admin"], ["g", "user:a|b", "admin"]],
                [1],
            ),
            # no trusted issuers, or no rules
            ([], [["g", "user:alice", "admin"]], []),
            ([ISSUER], None, []),
        ],
    )
    def test_configured(
        self,
        caplog: pytest.LogCaptureFixture,
        issuers: list[Any],
        policies: list[list[str]] | None,
        positions: list[int],
    ) -> None:
        """Configured rules naming unknown users are warned about, by position."""
        conf = AccessControlConfig(policies=policies)
        with caplog.at_level(logging.WARNING):
            register_access_control_module._check_policy_users(conf, _auth(*issuers))
        warnings = [w for w in _warnings(caplog) if w.startswith(UNKNOWN_USERS)]
        if positions:
            assert warnings == [warnings[0]]
            assert f"positions {positions} of" in warnings[0]
            assert "alice" not in warnings[0]
            assert "bob" not in warnings[0]
        else:
            assert not warnings

    def test_configured_on_registration(self, caplog: pytest.LogCaptureFixture) -> None:
        """Configured rules are checked when access control is registered."""
        cnx_app = create_connexion_app(
            Config(security=SecurityConfig(auth=_auth(ISSUER)))
        )
        conf = AccessControlConfig(
            db_name="access_control_db",
            collection_name="policy_rules",
            policies=[["g", "user:alice", "admin"]],
        )
        with caplog.at_level(logging.WARNING):
            register_access_control(
                cnx_app=cnx_app, mongo_config=None, access_control_config=conf
            )
        assert any(w.startswith(UNKNOWN_USERS) for w in _warnings(caplog))

    @pytest.mark.parametrize(
        "issuers,stored,warned",
        [
            ([ISSUER], True, True),
            ([ISSUER], False, False),
            # not checked without namespaces
            ([{"issuer": ISSUER, "namespace": ""}], True, False),
        ],
    )
    def test_stored(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        issuers: list[Any],
        stored: bool,
        warned: bool,
    ) -> None:
        """Stored rules naming unknown users are warned about."""
        check = MagicMock(return_value=stored)
        monkeypatch.setattr(Adapter, "has_stored_unknown_users", check)
        cnx_app = create_connexion_app(
            Config(security=SecurityConfig(auth=_auth(*issuers)))
        )
        conf = AccessControlConfig(
            db_name="access_control_db", collection_name="policy_rules"
        )
        with caplog.at_level(logging.WARNING):
            register_access_control(
                cnx_app=cnx_app, mongo_config=None, access_control_config=conf
            )
        warnings = [w for w in _warnings(caplog) if w.startswith(STORED_UNKNOWN_USERS)]
        assert bool(warnings) is warned
        if warned:
            assert check.call_args.args == ("user:", [ISSUER])

    def test_stored_unknown(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Startup continues if stored rules cannot be checked."""
        failure = MagicMock(side_effect=PyMongoError("unavailable"))
        monkeypatch.setattr(Adapter, "has_stored_unknown_users", failure)
        cnx_app = create_connexion_app(
            Config(security=SecurityConfig(auth=_auth(ISSUER)))
        )
        conf = AccessControlConfig(
            db_name="access_control_db", collection_name="policy_rules"
        )
        with caplog.at_level(logging.WARNING):
            register_access_control(
                cnx_app=cnx_app, mongo_config=None, access_control_config=conf
            )
        assert any(
            w.startswith("Could not check for stored policy rules with unknown users")
            for w in _warnings(caplog)
        )
