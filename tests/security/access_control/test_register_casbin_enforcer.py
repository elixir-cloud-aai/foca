"""Tests for registering the Casbin enforcer."""

import mongomock
import pytest
from connexion import App

from foca.database import register_mongodb
from foca.models.config import AccessControlConfig, MongoConfig
from foca.security.access_control import register_access_control as module
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    RESOURCE_ROLE_MODEL_FILE,
)

RESOURCE_ROLE_MODEL = str(
    module.files(ACCESS_CONTROL_BASE_PATH).joinpath(RESOURCE_ROLE_MODEL_FILE)
)
POLICIES = [
    ["p", "*", "/pets", "(GET)|(POST)"],
    ["p", "owner", "/pets/:id", "(GET)|(DELETE)"],
    ["g", "user:bob", "admin"],
]


@pytest.fixture
def clients(mongo_clients):
    """In-memory MongoDB clients created by FOCA, with their URIs."""
    return mongo_clients


@pytest.fixture
def captured(monkeypatch):
    """Capture arguments passed to the Casbin adapter."""
    captured: dict = {}

    def mock_adapter(collection, **kwargs):
        captured.update(collection=collection, **kwargs)

    monkeypatch.setattr(module, "Adapter", mock_adapter)
    return captured


def test_adapter_shares_app_client(clients, captured):
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


def test_adapter_dbname_override(clients, captured, monkeypatch):
    """Casbin adapter honours `MONGO_DBNAME`, like other database clients."""
    monkeypatch.setenv("MONGO_DBNAME", "other_db")
    module.register_casbin_enforcer(
        app=App(__name__),
        access_control_config=AccessControlConfig(),
        mongo_config=MongoConfig(host="mongodb", port=27017),
    )
    assert clients[0][0] == "mongodb://mongodb:27017/other_db"
    assert captured["collection"].database.name == "other_db"


def _register_policies(policies, model=RESOURCE_ROLE_MODEL):
    """Register enforcer with policies; return stored rules."""
    mongo_config = MongoConfig()
    app = App(__name__)
    module.register_casbin_enforcer(
        app=app,
        access_control_config=AccessControlConfig(
            model=model, policies=policies
        ),
        mongo_config=mongo_config,
    )
    collection = app.app.config["casbin_adapter"]._collection
    return collection, [
        [rule["ptype"]] + [rule[f"v{i}"] for i in range(6) if f"v{i}" in rule]
        for rule in collection.find()
    ]


class TestPolicies:
    """Tests for adding configured policy rules."""

    @pytest.fixture
    def clients(self, monkeypatch, mongo_clients):
        """Share one in-memory MongoDB client among app registrations, as
        with a persistent database."""
        client = mongomock.MongoClient()
        monkeypatch.setattr(
            register_mongodb, "MongoClient", lambda uri: client
        )
        return client

    def test_added(self, clients):
        """Configured rules are stored, with identifiers."""
        collection, rules = _register_policies(POLICIES)
        assert rules == POLICIES
        assert all(rule["id"] for rule in collection.find())

    def test_none(self, clients):
        """No rules are stored by default."""
        _, rules = _register_policies([])
        assert rules == []

    def test_not_duplicated(self, clients):
        """Rules are only added if they are not stored yet; other stored rules
        are kept."""
        collection, _ = _register_policies(POLICIES[:1])
        collection.insert_one(
            {"id": "other1", "ptype": "p", "v0": "a", "v1": "/b", "v2": "GET"}
        )
        _, rules = _register_policies(POLICIES)
        assert rules == [POLICIES[0], ["p", "a", "/b", "GET"], *POLICIES[1:]]

    def test_prefix_not_duplicate(self, clients):
        """Rules extending stored rules by further values are added."""
        model = RESOURCE_ROLE_MODEL
        _register_policies([["g", "user:bob", "admin"]], model=model)
        _, rules = _register_policies(
            [["g2", "user:bob", "admin", "/pets/1"]], model=model
        )
        assert ["g2", "user:bob", "admin", "/pets/1"] in rules

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
    def test_invalid(self, clients, rule):
        """Invalid rules prevent startup; no rules are stored then."""
        with pytest.raises(ValueError):
            _register_policies([POLICIES[0], rule])
        collection = clients["access_control_db"]["policy_rules"]
        assert collection.count_documents({}) == 0
