"""Tests for register_mongodb.py"""

from urllib.parse import unquote, urlsplit

import pytest

from foca.database import register_mongodb as module
from foca.database.register_mongodb import (
    create_mongo_client,
    get_mongo_db_name,
    get_mongo_uri,
    register_mongodb,
)
from foca.models.config import MongoConfig

MONGO_ENV_VARS = [
    "MONGO_URI",
    "MONGO_HOST",
    "MONGO_PORT",
    "MONGO_DBNAME",
    "MONGO_USERNAME",
    "MONGO_PASSWORD",
]


@pytest.fixture
def clean_env(monkeypatch):
    """Unset MongoDB-related environment variables."""
    for var in MONGO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestGetMongoUri:
    def test_defaults(self, clean_env):
        """URI is built from arguments."""
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://db:1234/my_db"
        )

    def test_env_vars(self, clean_env):
        """Individual environment variables override arguments."""
        clean_env.setenv("MONGO_HOST", "other")
        clean_env.setenv("MONGO_PORT", "4321")
        clean_env.setenv("MONGO_DBNAME", "other_db")
        assert get_mongo_uri(host="db", port=1234, db="my_db") == (
            "mongodb://other:4321/other_db"
        )

    def test_credentials_escaped(self, clean_env):
        """Credentials are percent-escaped."""
        clean_env.setenv("MONGO_USERNAME", "us@r")
        clean_env.setenv("MONGO_PASSWORD", "p:w/d")
        uri = urlsplit(get_mongo_uri(host="db", port=1234, db="my_db"))
        assert (uri.hostname, uri.port, uri.path) == ("db", 1234, "/my_db")
        assert unquote(uri.username) == "us@r"
        assert unquote(uri.password) == "p:w/d"

    def test_username_without_password(self, clean_env):
        """Unset password is passed as 'None', as in previous versions."""
        clean_env.setenv("MONGO_USERNAME", "user")
        uri = urlsplit(get_mongo_uri(host="db", port=1234, db="my_db"))
        assert (uri.username, uri.password) == ("user", "None")

    def test_empty_username(self, clean_env):
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
    def test_uri(self, clean_env, uri):
        """`MONGO_URI` takes precedence and is returned unchanged."""
        clean_env.setenv("MONGO_URI", uri)
        clean_env.setenv("MONGO_HOST", "ignored")
        clean_env.setenv("MONGO_DBNAME", "ignored")
        assert get_mongo_uri(host="db", port=1234, db="my_db") == uri

    def test_uri_invalid_scheme(self, clean_env):
        """`MONGO_URI` with invalid scheme."""
        clean_env.setenv("MONGO_URI", "http://h1:1")
        with pytest.raises(ValueError, match="MONGO_URI"):
            get_mongo_uri(db="my_db")


class TestGetMongoDbName:
    def test_default(self, clean_env):
        """Configured database name is used."""
        assert get_mongo_db_name("my_db") == "my_db"

    def test_override(self, clean_env):
        """`MONGO_DBNAME` overrides configured database name."""
        clean_env.setenv("MONGO_DBNAME", "other_db")
        assert get_mongo_db_name("my_db") == "other_db"


@pytest.fixture
def clients(mongo_clients):
    """In-memory MongoDB clients created by FOCA, with their URIs."""
    return mongo_clients


class TestCreateMongoClient:
    def test_uri(self, clients):
        """Client is created from the connection string."""
        res = create_mongo_client(host="db", port=1234, db="my_db")
        assert clients == [("mongodb://db:1234/my_db", res)]

    def test_no_db(self, clients):
        """Connection string names no database by default."""
        create_mongo_client(host="db", port=1234)
        assert clients[0][0] == "mongodb://db:1234/"

    @pytest.mark.parametrize(
        "uri", ["mongodb://h1:1/?tls=false", "mongodb://h1:1/admin"]
    )
    def test_mongo_uri(self, clients, monkeypatch, uri):
        """`MONGO_URI` is used unchanged."""
        monkeypatch.setenv("MONGO_URI", uri)
        create_mongo_client(db="my_db")
        assert clients[0][0] == uri

    def test_no_secrets_logged(self, clients, monkeypatch):
        """Credentials and options are not logged."""
        monkeypatch.setenv(
            "MONGO_URI",
            "mongodb://u:secret@h1:1/?tlsCertificateKeyFilePassword=secret",
        )
        messages = []
        monkeypatch.setattr(module.logger, "info", messages.append)
        create_mongo_client(db="my_db")
        assert messages == ["Created MongoDB client for 'mongodb://h1:1/'."]


MONGO_DICT_MIN = {
    "host": "mongodb",
    "port": 27017,
}
INDEX = {"keys": {"indexed_field": 1}, "options": {"name": "my_index"}}


def _config(**dbs) -> MongoConfig:
    return MongoConfig(**MONGO_DICT_MIN, dbs=dbs or None)


class TestRegisterMongodb:
    def test_no_database(self, clients):
        """No client is created without databases."""
        res = register_mongodb(_config())
        assert isinstance(res, MongoConfig)
        assert clients == []

    def test_one_client(self, clients):
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
        db1 = conf.dbs["db1"]
        assert db1.client.name == "db1"
        assert db1.client.client is client
        assert db1.collections["c1"].client.name == "c1"
        assert conf.dbs["db2"].client.name == "db2"
        # repeated registration reuses the client
        register_mongodb(conf)
        assert len(clients) == 1

    def test_dbname_override(self, clients, monkeypatch):
        """`MONGO_DBNAME` points all databases to the same database."""
        monkeypatch.setenv("MONGO_DBNAME", "other_db")
        conf = _config(db1={}, db2={})
        register_mongodb(conf)
        assert clients[0][0] == "mongodb://mongodb:27017/other_db"
        assert {db.client.name for db in conf.dbs.values()} == {"other_db"}

    def test_indexes_created_and_kept(self, clients):
        """Indexes are created; other existing indexes are kept."""
        conf = _config(db1={"collections": {"c1": {"indexes": [INDEX]}}})
        register_mongodb(conf)
        collection = conf.dbs["db1"].collections["c1"].client
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

    def test_index_conflict(self, clients):
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

    def test_index_without_keys(self, clients):
        """Indexes without keys are skipped."""
        conf = _config(db1={"collections": {"c1": {"indexes": [{}]}}})
        register_mongodb(conf)
        collection = conf.dbs["db1"].collections["c1"].client
        assert set(collection.index_information()) <= {"_id_"}
