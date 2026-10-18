"""Tests for registering the Casbin enforcer."""

from urllib.parse import urlsplit

from connexion import App

from foca.models.config import AccessControlConfig, MongoConfig
from foca.security.access_control import register_access_control as module


def test_adapter_uri(monkeypatch):
    """Casbin adapter connects with the app's MongoDB connection string."""
    for var in ["MONGO_HOST", "MONGO_PORT", "MONGO_DBNAME", "MONGO_PASSWORD"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MONGO_URI", "mongodb://h1:1,h2:2/?replicaSet=rs")
    captured = {}

    def mock_adapter(uri, dbname, collection, **kwargs):
        captured.update(
            uri=uri, dbname=dbname, collection=collection, **kwargs
        )

    monkeypatch.setattr(module, "Adapter", mock_adapter)
    module.register_casbin_enforcer(
        app=App(__name__),
        access_control_config=AccessControlConfig(
            db_name="access_control_db",
            collection_name="policy_rules",
            id_charset="abc",
            id_length=8,
        ),
        mongo_config=MongoConfig(),
    )
    assert captured == {
        "uri": "mongodb://h1:1,h2:2/?replicaSet=rs",
        "dbname": "access_control_db",
        "collection": "policy_rules",
        "id_charset": "abc",
        "id_length": 8,
    }


def test_adapter_uri_env_vars(monkeypatch):
    """Casbin adapter honours individual MongoDB environment variables."""
    for var in ["MONGO_URI", "MONGO_PORT", "MONGO_DBNAME"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MONGO_HOST", "other")
    monkeypatch.setenv("MONGO_USERNAME", "user")
    monkeypatch.setenv("MONGO_PASSWORD", "secret")
    captured = {}

    def mock_adapter(uri, dbname, collection, **kwargs):
        captured.update(uri=uri)

    monkeypatch.setattr(module, "Adapter", mock_adapter)
    module.register_casbin_enforcer(
        app=App(__name__),
        access_control_config=AccessControlConfig(
            db_name="access_control_db",
            collection_name="policy_rules",
        ),
        mongo_config=MongoConfig(host="mongodb", port=27017),
    )
    uri = urlsplit(captured["uri"])
    assert (uri.hostname, uri.port, uri.path) == (
        "other",
        27017,
        "/access_control_db",
    )
    assert (uri.username, uri.password) == ("user", "secret")


def test_adapter_dbname_override(monkeypatch):
    """Casbin adapter honours `MONGO_DBNAME`, like other database clients."""
    for var in ["MONGO_URI", "MONGO_HOST", "MONGO_PORT", "MONGO_USERNAME"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MONGO_DBNAME", "other_db")
    captured = {}

    def mock_adapter(uri, dbname, collection, **kwargs):
        captured.update(uri=uri, dbname=dbname)

    monkeypatch.setattr(module, "Adapter", mock_adapter)
    module.register_casbin_enforcer(
        app=App(__name__),
        access_control_config=AccessControlConfig(
            db_name="access_control_db",
            collection_name="policy_rules",
        ),
        mongo_config=MongoConfig(host="mongodb", port=27017),
    )
    assert captured == {
        "uri": "mongodb://mongodb:27017/other_db",
        "dbname": "other_db",
    }
