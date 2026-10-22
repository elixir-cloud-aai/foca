"""Tests for registering the Casbin enforcer."""

import logging
from typing import Any
from unittest.mock import MagicMock

import mongomock
import pytest
from connexion import App
from pymongo import MongoClient
from pymongo.collection import Collection

from foca.database import register_mongodb
from foca.models.config import AccessControlConfig, MongoConfig
from foca.security.access_control import register_access_control as module
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    RESOURCE_ROLE_MODEL_FILE,
)
from tests.mock_data import MongoClients

RESOURCE_ROLE_MODEL = str(
    module.files(ACCESS_CONTROL_BASE_PATH).joinpath(RESOURCE_ROLE_MODEL_FILE)
)
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
    policies: list[list[str]] | dict[str, list[str]] | None,
    model: str = RESOURCE_ROLE_MODEL,
) -> tuple[Collection[dict[str, Any]], list[list[str]]]:
    """Register enforcer with policies; return stored rules."""
    mongo_config = MongoConfig()
    app = App(__name__)
    module.register_casbin_enforcer(
        app=app,
        access_control_config=AccessControlConfig(model=model, policies=policies),
        mongo_config=mongo_config,
    )
    collection = app.app.config["casbin_adapter"]._collection
    return collection, [
        [rule["ptype"]]
        + [rule[f"v{i}"] for i in range(6) if rule.get(f"v{i}") is not None]
        for rule in collection.find()
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
        assert all(rule["id"] for rule in collection.find())

    def test_none(self, clients: MongoClient[dict[str, Any]]) -> None:
        """No rules are stored by default."""
        _, rules = _register_policies(None)
        assert rules == []

    def test_not_managed(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Stored rules are kept if policies are not set."""
        _register_policies(POLICIES)
        _, rules = _register_policies(None)
        assert rules == POLICIES

    def test_all_removed(self, clients: MongoClient[dict[str, Any]]) -> None:
        """With empty policies, all configured rules are removed."""
        _register_policies(POLICIES)
        _, rules = _register_policies([])
        assert rules == []

    def test_marked(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Configured rules are marked as such."""
        collection, _ = _register_policies(POLICIES)
        assert all(rule["configured"] is True for rule in collection.find())

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

    def test_adopted(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Stored rules with the values of configured rules are adopted.

        Such rules (e.g., added via the permissions API, also with unset values
        stored as null) become configured rules instead of being duplicated.
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
        assert rules == POLICIES[:1]
        adopted = collection.find_one({"id": "other1"})
        assert adopted is not None
        assert adopted["configured"] is True

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
        assert collection.count_documents({}) == len(POLICIES)

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
        assert collection.count_documents({}) == len(POLICIES)

    def test_named_invalid(self, clients: MongoClient[dict[str, Any]]) -> None:
        """Invalid named rules prevent startup; errors name the rule."""
        with pytest.raises(ValueError, match="Invalid policy rule 'broken': "):
            _register_policies({"ok": POLICIES[0], "broken": ["p", "*", "/pets"]})
        collection = clients["access_control_db"]["policy_rules"]
        assert collection.count_documents({}) == 0
