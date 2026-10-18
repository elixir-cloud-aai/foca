"""Tests for registering the Casbin enforcer."""

import pytest
from connexion import App

from foca.models.config import AccessControlConfig, MongoConfig
from foca.security.access_control import register_access_control as module


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
