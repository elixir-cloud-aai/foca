"""Tests for registering the Casbin enforcer."""

import logging
import time
from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock

import mongomock
import pytest
from connexion import App
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import (
    ConnectionFailure,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from foca.database import register_mongodb
from foca.models.config import AccessControlConfig, MongoConfig
from foca.security.access_control import register_access_control as module
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    GENERATION_MARKER_ID,
    RESOURCE_ROLE_MODEL_FILE,
)
from tests.mock_data import MongoClients

RESOURCE_ROLE_MODEL = str(
    module.files(ACCESS_CONTROL_BASE_PATH).joinpath(RESOURCE_ROLE_MODEL_FILE)
)
# Stored rules, without the document naming the active generation
RULES_FILTER = {"_id": {"$ne": GENERATION_MARKER_ID}}
POLICIES = [
    ["p", "*", "/pets", "(GET)|(POST)"],
    ["p", "owner", "/pets/:id", "(GET)|(DELETE)"],
    ["g", "user:bob", "admin"],
]


@pytest.fixture
def clients(
    mongo_clients: MongoClients,
) -> MongoClients:
    """In-memory MongoDB clients created by FOCA, with their URIs."""
    return mongo_clients


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture arguments passed to the Casbin adapter."""
    captured: dict[str, Any] = {}

    def mock_adapter(collection: Any, **kwargs: Any) -> MagicMock:
        captured.update(collection=collection, **kwargs)
        return MagicMock(model=None)

    monkeypatch.setattr(module, "Adapter", mock_adapter)
    return captured


def test_adapter_shares_app_client(
    clients: MongoClients, captured: dict[str, Any]
) -> None:
    """Casbin adapter uses the app's MongoDB client and policy collection."""
    mongo_config = MongoConfig()
    module.register_casbin_enforcer(
        app=App(__name__),
        access_control_config=AccessControlConfig(
            db_name="access_control_db",
            collection_name="policy_rules",
            id_charset="abcdef",
            id_length=8,
        ),
        mongo_config=mongo_config,
    )
    assert len(clients) == 1
    collection = captured.pop("collection")
    assert collection.database.client is mongo_config._client
    assert (collection.database.name, collection.name) == (
        "access_control_db",
        "policy_rules",
    )
    assert captured == {
        "id_charset": "abcdef",
        "id_length": 8,
        # rules are validated against the app's model
        "model": AccessControlConfig().model,
    }


def test_adapter_dbname_override(
    clients: MongoClients,
    captured: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Casbin adapter honours `MONGO_DBNAME`, like other database clients."""
    monkeypatch.setenv("MONGO_DBNAME", "other_db")
    module.register_casbin_enforcer(
        app=App(__name__),
        access_control_config=AccessControlConfig(),
        mongo_config=MongoConfig(host="mongodb", port=27017),
    )
    assert clients[0][0] == "mongodb://mongodb:27017/other_db"
    assert captured["collection"].database.name == "other_db"


def _register_policies(
    policies: list[list[str]] | Mapping[str, list[str] | None] | None,
    model: str = RESOURCE_ROLE_MODEL,
) -> tuple[Collection[dict[str, Any]], list[list[str]]]:
    """Register enforcer with policies; return stored rules."""
    mongo_config = MongoConfig()
    app = App(__name__)
    module.register_casbin_enforcer(
        app=app,
        access_control_config=AccessControlConfig.model_validate(
            {"model": model, "policies": policies}
        ),
        mongo_config=mongo_config,
    )
    collection = app.app.config["casbin_adapter"]._collection
    return collection, [
        [rule["ptype"]]
        + [rule[f"v{i}"] for i in range(6) if rule.get(f"v{i}") is not None]
        for rule in collection.find(RULES_FILTER)
    ]


class TestPolicies:
    """Tests for adding configured policy rules."""

    @pytest.fixture
    def clients(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mongo_clients: MongoClients,
    ) -> MongoClient[dict[str, Any]]:
        """Share one in-memory MongoDB client among app registrations.

        Registrations thus behave as with a persistent database.
        """
        client: MongoClient[dict[str, Any]] = mongomock.MongoClient()
        monkeypatch.setattr(register_mongodb, "MongoClient", lambda uri: client)
        return client

    def test_added(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Configured rules are stored, with identifiers."""
        collection, rules = _register_policies(POLICIES)
        assert rules == POLICIES
        assert all(rule["id"] for rule in collection.find(RULES_FILTER))

    def test_none(self, clients: MongoClient[dict[str, Any]]) -> None:
        """No rules are stored by default, nor is anything else."""
        collection, rules = _register_policies(None)
        assert rules == []
        assert collection.count_documents({}) == 0

    def test_unset_removes(
        self,
        clients: MongoClient[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Stored configured rules are removed if policies are not set.

        E.g., if set via an overlay that was removed. Other rules are kept.
        """
        collection, _ = _register_policies(POLICIES)
        collection.insert_one(
            {"id": "other1", "ptype": "p", "v0": "a", "v1": "/b", "v2": "GET"}
        )
        monkeypatch.setattr(module.logger, "disabled", False)
        with caplog.at_level(logging.WARNING, logger=module.logger.name):
            _, rules = _register_policies(None)
        assert rules == [["p", "a", "/b", "GET"]]
        assert "Removing stored configured policy rules" in caplog.text

    def test_unset_removes_legacy(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Configured rules stored by FOCA 0.16 are removed if not set."""
        collection = clients["access_control_db"]["policy_rules"]
        collection.insert_one(
            {"id": "legacy", "ptype": "g", "v0": "a", "v1": "b", "configured": True}
        )
        _, rules = _register_policies(None)
        assert rules == []

    def test_unset_removes_inactive(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Without policies, rules of inactive generations are removed, too.

        E.g., left by an instance that failed while storing them.
        """
        collection, _ = _register_policies([])
        collection.insert_one(
            {
                "id": "pendng",
                "ptype": "g",
                "v0": "a",
                "v1": "b",
                "configured": True,
                "generations": ["0" * 24],
            }
        )
        _, rules = _register_policies(None)
        assert rules == []

    def test_unset_without_database(
        self,
        clients: MongoClient[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Without database, apps without policies refuse to start.

        So that stored configured rules never stay in effect.
        """

        def unavailable(self: Any) -> bool:
            raise ServerSelectionTimeoutError("unavailable")

        monkeypatch.setattr(module.Adapter, "has_configured_policies", unavailable)
        monkeypatch.setattr(module.logger, "disabled", False)
        with (
            caplog.at_level(logging.ERROR, logger=module.logger.name),
            pytest.raises(ServerSelectionTimeoutError),
        ):
            _register_policies(None)
        assert "refusing to start" in caplog.text

    def test_unset_check_times_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The check for configured rules gives up soon without database.

        Rather than after the client's server selection timeout (30 s by
        default), e.g., so that gunicorn workers fail to boot before their
        timeout.
        """
        monkeypatch.setattr(module, "CONFIGURED_POLICIES_CHECK_TIMEOUT", 0.1)
        # Nothing listens on port 1; the client connects only when used, and
        # retries refused connections until server selection times out
        client: MongoClient[dict[str, Any]] = MongoClient(
            "mongodb://127.0.0.1:1", connect=False
        )
        adapter = module.Adapter(collection=client["db"]["rules"])
        start = time.monotonic()
        with pytest.raises(ServerSelectionTimeoutError):
            module._remove_policies(adapter)
        client.close()
        assert time.monotonic() - start < 10

    def test_unset_check_error(
        self,
        clients: MongoClient[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Errors other than connection failures from the check are raised."""

        def denied(self: Any) -> bool:
            raise OperationFailure("not authorized")

        monkeypatch.setattr(module.Adapter, "has_configured_policies", denied)
        with pytest.raises(OperationFailure):
            _register_policies(None)

    @pytest.mark.parametrize(
        "error",
        [OperationFailure("not authorized"), ConnectionFailure("lost")],
    )
    def test_unset_removal_error(
        self,
        clients: MongoClient[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        error: PyMongoError,
    ) -> None:
        """Errors while removing stored configured rules are raised.

        Also connection failures, so that apps do not start with rules that
        were removed from their configuration, as with `policies` set.
        """
        _register_policies(POLICIES)

        def fail(self: Any, rules: list[list[str]]) -> None:
            raise error

        monkeypatch.setattr(module.Adapter, "set_configured_policies", fail)
        with pytest.raises(type(error)):
            _register_policies(None)

    def test_all_removed(self, clients: MongoClient[dict[str, Any]]) -> None:
        """With empty policies, all configured rules are removed."""
        _register_policies(POLICIES)
        _, rules = _register_policies([])
        assert rules == []

    def test_marked(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Configured rules are marked as such."""
        collection, _ = _register_policies(POLICIES)
        assert all(rule["configured"] is True for rule in collection.find(RULES_FILTER))

    def test_not_duplicated(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Rules are only added if they are not stored yet.

        Other stored rules are kept.
        """
        collection, _ = _register_policies(POLICIES[:1])
        collection.insert_one(
            {"id": "other1", "ptype": "p", "v0": "a", "v1": "/b", "v2": "GET"}
        )
        _, rules = _register_policies(POLICIES)
        assert rules == [POLICIES[0], ["p", "a", "/b", "GET"], *POLICIES[1:]]

    def test_removed(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Configured rules removed from the configuration are removed.

        Other rules are kept.
        """
        collection, _ = _register_policies(POLICIES)
        collection.insert_one(
            {"id": "other1", "ptype": "p", "v0": "a", "v1": "/b", "v2": "GET"}
        )
        _, rules = _register_policies(POLICIES[1:])
        assert rules == [*POLICIES[1:], ["p", "a", "/b", "GET"]]

    def test_duplicates_removed(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Duplicates of configured rules are removed.

        E.g., added by replicas starting concurrently.
        """
        collection, _ = _register_policies(POLICIES[:1])
        duplicate = collection.find_one({}, projection={"_id": False})
        assert duplicate is not None
        collection.insert_one({**duplicate, "id": "dupl01"})
        _, rules = _register_policies(POLICIES[:1])
        assert rules == POLICIES[:1]

    def test_unmarked_copy_kept(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Stored rules with the values of configured rules are kept.

        Such rules (e.g., added via the permissions API, also with unset values
        stored as null) stay rules that are not configured, so that they are
        in effect throughout; a configured copy is added.
        """
        _, _ = _register_policies([])
        collection = clients["access_control_db"]["policy_rules"]
        collection.insert_one(
            {
                "id": "other1",
                "ptype": "p",
                "v0": "*",
                "v1": "/pets",
                "v2": "(GET)|(POST)",
                "v3": None,
            }
        )
        _, rules = _register_policies(POLICIES[:1])
        assert rules == POLICIES[:1] * 2
        kept = collection.find_one({"id": "other1"})
        assert kept is not None
        assert "configured" not in kept
        assert collection.count_documents({"configured": True}) == 1

    @pytest.mark.parametrize(
        "rule",
        [
            ["x", "*", "/pets", "GET"],  # policy type not in model
            ["p", "*", "/pets"],  # too few values
            ["p", "*", "/pets", "("],  # invalid action
            ["p", "a,b", "/pets", "GET"],  # invalid value
            ["p"],  # no values
        ],
    )
    def test_invalid(
        self, clients: MongoClient[dict[str, Any]], rule: list[str]
    ) -> None:
        """Invalid rules prevent startup; no rules are stored then."""
        with pytest.raises(ValueError):
            _register_policies([POLICIES[0], rule])
        collection = clients["access_control_db"]["policy_rules"]
        assert collection.count_documents({}) == 0

    def test_invalid_keeps_stored(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Invalid configurations do not change stored rules."""
        _register_policies(POLICIES)
        with pytest.raises(ValueError):
            _register_policies([["p", "*", "/pets"]])
        collection = clients["access_control_db"]["policy_rules"]
        assert collection.count_documents(RULES_FILTER) == len(POLICIES)

    def test_named(
        self,
        clients: MongoClient[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Named rules are stored like listed ones; their names are logged."""
        monkeypatch.setattr(module.logger, "disabled", False)
        named = {"read-write": POLICIES[0], "owner": POLICIES[1], "bob": POLICIES[2]}
        with caplog.at_level(logging.INFO, logger=module.logger.name):
            _, rules = _register_policies(named)
        assert rules == POLICIES
        assert "Named policy rules applied: read-write, owner, bob" in caplog.messages
        # switching between named and listed rules keeps stored rules
        collection, _ = _register_policies(POLICIES)
        assert collection.count_documents(RULES_FILTER) == len(POLICIES)

    def test_named_invalid(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Invalid named rules prevent startup; errors name the rule."""
        with pytest.raises(ValueError, match="Invalid policy rule 'broken': "):
            _register_policies({"ok": POLICIES[0], "broken": ["p", "*", "/pets"]})
        collection = clients["access_control_db"]["policy_rules"]
        assert collection.count_documents({}) == 0

    def test_named_all_removed(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Removing all named rules removes all configured rules.

        E.g., an overlay setting each rule of the app to `null`.
        """
        _register_policies({"read-write": POLICIES[0]})
        _, rules = _register_policies({"read-write": None})
        assert rules == []
