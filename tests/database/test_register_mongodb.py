"""Tests for `register_mongodb.py`."""

from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from pymongo.collection import Collection
from pymongo.database import Database

from foca.database import register_mongodb as module
from foca.database.register_mongodb import (
    create_mongo_client,
    get_mongo_db_name,
    get_mongo_uri,
    register_mongodb,
)
from foca.models.config import MongoConfig
from tests.mock_data import MongoClients

MONGO_ENV_VARS = [
    "MONGO_URI",
    "MONGO_HOST",
    "MONGO_PORT",
    "MONGO_DBNAME",
    "MONGO_USERNAME",
    "MONGO_PASSWORD",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Unset MongoDB-related environment variables."""
    for var in MONGO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestGetMongoUri:
    """Tests for `get_mongo_uri()`."""

    def test_defaults(self, clean_env: pytest.MonkeyPatch) -> None:
        """URI is built from arguments."""
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://db:1234/my_db"
        )

    def test_env_vars(self, clean_env: pytest.MonkeyPatch) -> None:
        """Individual environment variables override arguments."""
        clean_env.setenv("MONGO_HOST", "other")
        clean_env.setenv("MONGO_PORT", "4321")
        clean_env.setenv("MONGO_DBNAME", "other_db")
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://other:4321/other_db"
        )

    def test_credentials_escaped(self, clean_env: pytest.MonkeyPatch) -> None:
        """Credentials are percent-escaped."""
        clean_env.setenv("MONGO_USERNAME", "us@r")
        clean_env.setenv("MONGO_PASSWORD", "p:w/d")
        uri = urlsplit(get_mongo_uri(host="db", port=1234, db="my_db"))
        assert (uri.hostname, uri.port, uri.path) == ("db", 1234, "/my_db")
        assert unquote(uri.username or "") == "us@r"
        assert unquote(uri.password or "") == "p:w/d"

    def test_username_without_password(self, clean_env: pytest.MonkeyPatch) -> None:
        """Unset password is passed as 'None', as in previous versions."""
        clean_env.setenv("MONGO_USERNAME", "user")
        uri = urlsplit(get_mongo_uri(host="db", port=1234, db="my_db"))
        assert (uri.username, uri.password) == ("user", "None")

    def test_empty_username(self, clean_env: pytest.MonkeyPatch) -> None:
        """Empty username is ignored."""
        clean_env.setenv("MONGO_USERNAME", "")
        clean_env.setenv("MONGO_PASSWORD", "secret")
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://db:1234/my_db"
        )

    @pytest.mark.parametrize(
        "uri",
        [
            "mongodb://h1:1,h2:2/?replicaSet=rs&tls=true",
            "mongodb://h1:1",
            "mongodb+srv://cluster.example.org",
            "mongodb://h1:1/admin?tls=true;w=1",
            "mongodb://h1:1/accounts&tls=false",
            "mongodb://alice%40EXAMPLE.COM@h1:1/?authMechanism=GSSAPI",
        ],
    )
    def test_uri(self, clean_env: pytest.MonkeyPatch, uri: str) -> None:
        """`MONGO_URI` takes precedence and is returned unchanged."""
        clean_env.setenv("MONGO_URI", uri)
        clean_env.setenv("MONGO_HOST", "ignored")
        clean_env.setenv("MONGO_DBNAME", "ignored")
        assert get_mongo_uri(host="db", port=1234, db="my_db") == uri

    def test_uri_invalid_scheme(self, clean_env: pytest.MonkeyPatch) -> None:
        """`MONGO_URI` with invalid scheme."""
        clean_env.setenv("MONGO_URI", "http://h1:1")
        with pytest.raises(ValueError, match="MONGO_URI"):
            get_mongo_uri(db="my_db")


class TestGetMongoDbName:
    """Tests for `get_mongo_db_name()`."""

    def test_default(self, clean_env: pytest.MonkeyPatch) -> None:
        """Configured database name is used."""
        assert get_mongo_db_name("my_db") == "my_db"

    def test_override(self, clean_env: pytest.MonkeyPatch) -> None:
        """`MONGO_DBNAME` overrides configured database name."""
        clean_env.setenv("MONGO_DBNAME", "other_db")
        assert get_mongo_db_name("my_db") == "other_db"


@pytest.fixture
def clients(mongo_clients: MongoClients) -> MongoClients:
    """In-memory MongoDB clients created by FOCA, with their URIs."""
    return mongo_clients


class TestCreateMongoClient:
    """Tests for `create_mongo_client()`."""

    def test_uri(self, clients: MongoClients) -> None:
        """Client is created from the connection string."""
        res = create_mongo_client(host="db", port=1234, db="my_db")
        assert clients == [("mongodb://db:1234/my_db", res)]

    def test_no_db(self, clients: MongoClients) -> None:
        """Connection string names no database by default."""
        create_mongo_client(host="db", port=1234)
        assert clients[0][0] == "mongodb://db:1234/"

    @pytest.mark.parametrize(
        "uri", ["mongodb://h1:1/?tls=false", "mongodb://h1:1/admin"]
    )
    def test_mongo_uri(
        self, clients: MongoClients, monkeypatch: pytest.MonkeyPatch, uri: str
    ) -> None:
        """`MONGO_URI` is used unchanged."""
        monkeypatch.setenv("MONGO_URI", uri)
        create_mongo_client(db="my_db")
        assert clients[0][0] == uri

    def test_no_secrets_logged(
        self, clients: MongoClients, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Credentials and options are not logged."""
        monkeypatch.setenv(
            "MONGO_URI",
            "mongodb://u:secret@h1:1/?tlsCertificateKeyFilePassword=secret",
        )
        messages: list[str] = []
        monkeypatch.setattr(
            module.logger, "info", lambda msg, *args: messages.append(msg % args)
        )
        create_mongo_client(db="my_db")
        assert messages == ["Created MongoDB client for 'mongodb://h1:1/'."]


MONGO_DICT_MIN: dict[str, Any] = {
    "host": "mongodb",
    "port": 27017,
}
INDEX = {"keys": {"indexed_field": 1}, "options": {"name": "my_index"}}


def _config(**dbs: Any) -> MongoConfig:
    return MongoConfig(**MONGO_DICT_MIN, dbs=dbs or None)


def _database(conf: MongoConfig, db: str) -> Database[Any]:
    """Get the client of a registered database."""
    assert conf.dbs is not None
    client = conf.dbs[db].client
    assert client is not None
    return client


def _collection(conf: MongoConfig, db: str, collection: str) -> Collection[Any]:
    """Get the client of a registered collection."""
    assert conf.dbs is not None
    collections = conf.dbs[db].collections
    assert collections is not None
    client = collections[collection].client
    assert client is not None
    return client


class TestRegisterMongodb:
    """Tests for `register_mongodb()`."""

    def test_no_database(self, clients: MongoClients) -> None:
        """No client is created without databases."""
        res = register_mongodb(_config())
        assert isinstance(res, MongoConfig)
        assert clients == []

    def test_one_client(self, clients: MongoClients) -> None:
        """All databases share one client."""
        conf = _config(
            db1={"collections": {"c1": {}, "c2": {"indexes": None}}},
            db2={"collections": None},
        )
        register_mongodb(conf)
        assert len(clients) == 1
        uri, client = clients[0]
        # authenticates against the first database
        assert uri == "mongodb://mongodb:27017/db1"
        assert conf._client is client
        db1 = _database(conf, "db1")
        assert db1.name == "db1"
        assert db1.client is client
        assert _collection(conf, "db1", "c1").name == "c1"
        assert _database(conf, "db2").name == "db2"
        # repeated registration reuses the client
        register_mongodb(conf)
        assert len(clients) == 1

    def test_dbname_override(
        self, clients: MongoClients, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`MONGO_DBNAME` points all databases to the same database."""
        monkeypatch.setenv("MONGO_DBNAME", "other_db")
        conf = _config(db1={}, db2={})
        register_mongodb(conf)
        assert clients[0][0] == "mongodb://mongodb:27017/other_db"
        assert {_database(conf, db).name for db in ("db1", "db2")} == {"other_db"}

    def test_indexes_created_and_kept(self, clients: MongoClients) -> None:
        """Indexes are created; other existing indexes are kept."""
        conf = _config(db1={"collections": {"c1": {"indexes": [INDEX]}}})
        register_mongodb(conf)
        collection = _collection(conf, "db1", "c1")
        collection.create_index("other_field", name="manual_index")
        # restart: new config, same database
        conf = _config(db1={"collections": {"c1": {"indexes": [INDEX]}}})
        conf._client = clients[0][1]
        register_mongodb(conf)
        assert set(collection.index_information()) == {
            "_id_",
            "my_index",
            "manual_index",
        }

    def test_index_conflict(self, clients: MongoClients) -> None:
        """Conflicting indexes fail with a clear message."""
        conf = _config(db1={"collections": {"c1": {"indexes": [INDEX]}}})
        register_mongodb(conf)
        conflicting = {
            "keys": {"other_field": 1},
            "options": {"name": "my_index"},
        }
        conf = _config(db1={"collections": {"c1": {"indexes": [conflicting]}}})
        conf._client = clients[0][1]
        with pytest.raises(ValueError, match="conflicts with an existing index"):
            register_mongodb(conf)

    def test_index_without_keys(self, clients: MongoClients) -> None:
        """Indexes without keys are skipped."""
        conf = _config(db1={"collections": {"c1": {"indexes": [{}]}}})
        register_mongodb(conf)
        collection = _collection(conf, "db1", "c1")
        assert set(collection.index_information()) <= {"_id_"}
