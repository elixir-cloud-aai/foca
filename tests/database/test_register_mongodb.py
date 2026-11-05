"""Tests for `register_mongodb.py`."""

from pathlib import Path
from traceback import format_exception
from typing import Any
from unittest.mock import MagicMock, Mock
from urllib.parse import unquote, urlsplit

import mongomock
import pytest
from pydantic import SecretStr, ValidationError
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import (
    ConfigurationError,
    InvalidURI,
    OperationFailure,
    PyMongoError,
)

from foca.database import register_mongodb as module
from foca.database.register_mongodb import (
    IndexConflictError,
    _register_database,
    create_mongo_client,
    get_mongo_client,
    get_mongo_db_name,
    get_mongo_uri,
    register_mongodb,
    reopen_mongodb,
)
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (
    AccessControlConfig,
    CollectionConfig,
    Config,
    DBConfig,
    MongoConfig,
    ServiceInfoConfig,
)
from foca.security.access_control.register_access_control import (
    register_access_control,
)
from tests.mock_data import MongoClients
from tests.test_files.service_info_data import FIELDS, REMOVE, service

# Assembled here, as secret scanners take such strings for real credentials
SECRET = "hunter2"  # noqa: S105 (made up)
AUTH = f"u:{SECRET}@"
SCHEME = "mongodb://"
SRV_SCHEME = "mongodb+srv://"
OPTION = f"tlsCertificateKeyFilePassword={SECRET}"

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


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What is logged on level `INFO`, whatever the logging setup of other tests."""
    messages: list[str] = []
    monkeypatch.setattr(
        module.logger, "info", lambda msg, *args: messages.append(msg % args)
    )
    return messages


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

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({}, "db:1234/my_db"),
            ({"MONGO_HOST": "h1", "MONGO_PORT": "1"}, "h1:1/my_db"),
            ({"MONGO_DBNAME": "other-db"}, "db:1234/other-db"),
            ({"MONGO_USERNAME": "u", "MONGO_PASSWORD": SECRET}, "db:1234/my_db"),
            # Connection strings; the app selects the database by name
            ({"MONGO_URI": f"{SCHEME}h1:1"}, "h1:1/my_db"),
            ({"MONGO_URI": f"{SCHEME}{AUTH}h1:1/"}, "h1:1/my_db"),
            ({"MONGO_URI": f"{SCHEME}{AUTH}h2:2,h1:1/admin"}, "h1:1,h2:2/my_db"),
            ({"MONGO_URI": f"{SCHEME}u:{SECRET}#x@h1:1/"}, "h1:1/my_db"),
            ({"MONGO_URI": f"{SRV_SCHEME}{AUTH}h1.org/db?a=b"}, "h1.org/my_db"),
            ({"MONGO_URI": f"{SCHEME}h1:1/?{OPTION}"}, "h1:1/my_db"),
            ({"MONGO_URI": f"{SCHEME}h1:1/{SECRET}"}, "h1:1/my_db"),
            ({"MONGO_URI": f"{SCHEME}h1:1/db;{OPTION}"}, "h1:1/my_db"),
            ({"MONGO_URI": f"{SCHEME}h1:1/db?{OPTION}@x"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SCHEME}h1:1?{OPTION}"}, "h1:1/my_db"),
            ({"MONGO_URI": f"{SCHEME}h1:1?{OPTION}@x"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SCHEME}{AUTH}h1:1?{OPTION}@x"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SCHEME}h1?x={SECRET}/y"}, "h1:27017/my_db"),
            # Unescaped characters in credentials, which PyMongo takes for hosts
            ({"MONGO_URI": f"{SCHEME}u/v:{SECRET}@h1"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SCHEME}u:12/{SECRET}?a=b@h1"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SCHEME}u:p@{SECRET}/w@h1:1"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SCHEME}u:12,{SECRET}/x@h1"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SCHEME}u:a@{SECRET}?x=y@h1:1/"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SRV_SCHEME}u:p@{SECRET}/w@h1"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SCHEME}u:{SECRET}.sock"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SCHEME}u:{SECRET}%2Fx.sock"}, "unknown/my_db"),
            ({"MONGO_URI": f"{SCHEME}%2Frun%2Fm.sock/"}, "/run/m.sock/my_db"),
            ({"MONGO_HOST": f"u:p@{SECRET}?w=x@h1"}, "unknown/my_db"),
            ({"MONGO_PORT": f"1,u:{SECRET}.sock"}, "unknown/my_db"),
            # Other variables
            ({"MONGO_HOST": f"{AUTH}h1"}, "h1:1234/my_db"),
            ({"MONGO_HOST": f"h1/?{OPTION}"}, "h1:27017/my_db"),
            ({"MONGO_HOST": f"h1?{OPTION}"}, "h1:27017/my_db"),
            ({"MONGO_HOST": f"h1?{OPTION}@x"}, "unknown/my_db"),
            ({"MONGO_PORT": f"1/?{OPTION}"}, "db:1/my_db"),
            ({"MONGO_PORT": f"1?{OPTION}@x"}, "unknown/my_db"),
            ({"MONGO_DBNAME": f"my_db?{OPTION}"}, "db:1234/(name not shown)"),
            ({"MONGO_DBNAME": f"my_db#{SECRET}"}, "db:1234/(name not shown)"),
            ({"MONGO_DBNAME": f"{AUTH}x"}, "unknown/(name not shown)"),
        ],
    )
    def test_only_location_logged(
        self,
        clean_env: pytest.MonkeyPatch,
        logged: list[str],
        env: dict[str, str],
        expected: str,
    ) -> None:
        """Only hosts, ports and the database are logged, however configured."""
        for name, value in env.items():
            clean_env.setenv(name, value)
        create_mongo_client(host="db", port=1234, db="my_db", connect=False).close()
        assert logged == [f"Created MongoDB client for '{expected}'."]
        assert SECRET not in "".join(logged)

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            (f"{AUTH}h1", "h1:1/my_db"),
            (f"h1?{OPTION}@x", "unknown/my_db"),
        ],
    )
    def test_credentials_in_configured_host_not_logged(
        self,
        clean_env: pytest.MonkeyPatch,
        logged: list[str],
        host: str,
        expected: str,
    ) -> None:
        """Credentials in the host of the configuration are not logged."""
        create_mongo_client(host=host, port=1, db="my_db", connect=False).close()
        assert logged == [f"Created MongoDB client for '{expected}'."]

    @pytest.mark.parametrize(
        ("env", "name", "content", "expected"),
        [
            ({}, "MONGO_URI", f"{SCHEME}{AUTH}h1:1/", "h1:1/my_db"),
            ({"MONGO_PASSWORD": "x"}, "MONGO_USERNAME", SECRET, "db:1234/my_db"),
            ({"MONGO_USERNAME": "u"}, "MONGO_PASSWORD", SECRET, "db:1234/my_db"),
        ],
    )
    def test_credentials_from_files_not_logged(
        self,
        clean_env: pytest.MonkeyPatch,
        logged: list[str],
        tmp_path: Path,
        env: dict[str, str],
        name: str,
        content: str,
        expected: str,
    ) -> None:
        """Credentials from files are not logged."""
        path = tmp_path / "secret"
        path.write_text(content)
        clean_env.setenv(f"{name}_FILE", str(path))
        for variable, value in env.items():
            clean_env.setenv(variable, value)
        create_mongo_client(host="db", port=1234, db="my_db", connect=False).close()
        assert logged == [f"Created MongoDB client for '{expected}'."]
        assert SECRET not in "".join(logged)

    def test_client_options_not_logged(
        self, clean_env: pytest.MonkeyPatch, logged: list[str]
    ) -> None:
        """Options of the client are not logged."""
        create_mongo_client(
            host="db",
            port=1234,
            connect=False,
            options={"tlsCertificateKeyFilePassword": SecretStr(SECRET)},
        ).close()
        assert logged == ["Created MongoDB client for 'db:1234/'."]

    @pytest.mark.parametrize(
        "uri",
        [
            f"{SCHEME}u:{SECRET}/x@h1:1/",
            f"{SCHEME}u:{SECRET}?x@h1:1/",
            f"{SCHEME}u:{SECRET}@x@h1:1/",
            f"{SCHEME}h1/u:{SECRET}@h2/db",
        ],
    )
    def test_rejected_uri_not_logged(
        self,
        clean_env: pytest.MonkeyPatch,
        logged: list[str],
        uri: str,
    ) -> None:
        """Connection strings that PyMongo rejects aren't logged."""
        clean_env.setenv("MONGO_URI", uri)
        with pytest.raises((InvalidURI, ValueError)):
            create_mongo_client(connect=False)
        assert SECRET not in "".join(logged)

    def test_stand_in_for_client(
        self, clients: MongoClients, logged: list[str]
    ) -> None:
        """Stand-ins of clients, as in the tests of apps, don't get in the way."""
        create_mongo_client(host="db", port=1234, db="my_db")
        assert logged == ["Created MongoDB client for 'unknown/my_db'."]

    @pytest.mark.parametrize("stand_in", [Mock(), MagicMock(), object()])
    def test_other_stand_ins_for_client(self, stand_in: Any) -> None:
        """Stand-ins of any kind have unknown hosts."""
        assert module._get_client_hosts(stand_in, "mongodb://h1") == "unknown"


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
        with pytest.raises(IndexConflictError, match="conflicts with an existing"):
            register_mongodb(conf)

    def test_index_without_keys(self, clients: MongoClients) -> None:
        """Indexes without keys are skipped."""
        conf = _config(db1={"collections": {"c1": {"indexes": [{}]}}})
        register_mongodb(conf)
        collection = _collection(conf, "db1", "c1")
        assert set(collection.index_information()) <= {"_id_"}


class TestReopenMongodb:
    """Tests for `reopen_mongodb()`."""

    def test_replaces_client(self, clients: MongoClients) -> None:
        """Handles of all databases and collections use a new client."""
        conf = MongoConfig(
            dbs={
                "a": DBConfig(collections={"c": CollectionConfig()}),
                "b": DBConfig(),
            },
            host="db",
        )
        register_mongodb(conf)
        previous = conf._client
        reopen_mongodb(conf)
        assert len(clients) == 2
        client = clients[1][1]
        assert conf._client is client
        assert client is not previous
        dbs = conf.dbs or {}
        assert dbs["a"].client == client["a"]
        assert dbs["b"].client == client["b"]
        assert (dbs["a"].collections or {})["c"].client == client["a"]["c"]

    def test_keeps_database_names(
        self, clients: MongoClients, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Names set through the environment stay the same."""
        monkeypatch.setenv("MONGO_DBNAME", "other")
        conf = MongoConfig(dbs={"a": DBConfig()})
        register_mongodb(conf)
        reopen_mongodb(conf)
        db = (conf.dbs or {})["a"].client
        assert db is not None
        assert db.name == "other"

    def test_closes_previous_client(self, clients: MongoClients) -> None:
        """The previous client is closed."""
        conf = MongoConfig(dbs={"a": DBConfig()})
        register_mongodb(conf)
        previous = MagicMock()
        conf._client = previous
        reopen_mongodb(conf)
        previous.close.assert_called_once_with()

    def test_skips_unregistered(self, clients: MongoClients) -> None:
        """Databases and collections without clients stay without one."""
        conf = MongoConfig(dbs={"a": DBConfig(collections={"c": CollectionConfig()})})
        get_mongo_client(conf)
        reopen_mongodb(conf)
        db_conf = (conf.dbs or {})["a"]
        assert db_conf.client is None
        assert (db_conf.collections or {})["c"].client is None
        register_mongodb(conf)
        (db_conf.collections or {})["c"].client = None
        reopen_mongodb(conf)
        assert db_conf.client is not None
        assert (db_conf.collections or {})["c"].client is None

    def test_no_client(self, clients: MongoClients) -> None:
        """Nothing happens without a client."""
        conf = MongoConfig(dbs={"a": DBConfig()})
        reopen_mongodb(conf)
        assert conf._client is None
        assert not clients

    def test_new_client_connects_on_first_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new client doesn't connect, nor start threads, until used."""
        for var in MONGO_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        created = MagicMock()
        monkeypatch.setattr(module, "MongoClient", created)
        conf = MongoConfig(dbs={"a": DBConfig()})
        conf._client = MagicMock()
        reopen_mongodb(conf)
        assert created.call_args.kwargs["connect"] is False


class TestClientOptions:
    """Tests for the options of MongoDB clients."""

    @pytest.fixture
    def options(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """Keyword arguments of the MongoDB clients created."""
        for var in MONGO_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        created: list[dict[str, Any]] = []

        def mongo_client(uri: str, **kwargs: Any) -> Any:
            """Record the keyword arguments of a client."""
            created.append(kwargs)
            return mongomock.MongoClient()

        monkeypatch.setattr(module, "MongoClient", mongo_client)
        return created

    def test_defaults(self, options: list[dict[str, Any]]) -> None:
        """Clients have the default timeouts, also without configuration."""
        create_mongo_client()
        assert options == [
            {
                "connect": True,
                "serverselectiontimeoutms": 5000,
                "connecttimeoutms": 5000,
            }
        ]

    def test_config(self, options: list[dict[str, Any]]) -> None:
        """Options of the config are passed, and win over defaults."""
        get_mongo_client(
            MongoConfig(
                client_options={
                    "maxPoolSize": 20,
                    "serverselectiontimeoutMS": 100,
                    "appname": "mine",
                }
            )
        )
        assert options == [
            {
                "connect": True,
                "maxpoolsize": 20,
                "serverselectiontimeoutms": 100,
                "connecttimeoutms": 5000,
                "appname": "mine",
            }
        ]

    def test_default_appname(self, options: list[dict[str, Any]]) -> None:
        """Without service info, the app is named after FOCA."""
        get_mongo_client(MongoConfig())
        assert options[0]["appname"] == "foca"

    @pytest.mark.parametrize(
        ("service_info", "expected"),
        [
            ({"service": service(id="org.example.my")}, "org.example.my"),
            ({"service": service(id="x" * 128)}, "x" * 128),
            ({"service": service(id="x" * 129)}, "foca"),
            ({"service": service(id="\u00e4" * 65)}, "foca"),
            (
                {
                    "service": service(id=REMOVE),
                    "runtime_fields": f"{FIELDS}.runtime_fields",
                    "package": "pytest",
                },
                "pytest",
            ),
            (
                {
                    "service": service(id=REMOVE),
                    "runtime_fields": f"{FIELDS}.runtime_fields",
                },
                "foca",
            ),
        ],
    )
    def test_appname_service_info(
        self,
        options: list[dict[str, Any]],
        service_info: dict[str, Any],
        expected: str,
    ) -> None:
        """The app is named after its service info."""
        conf = Config(db=MongoConfig(), service_info=ServiceInfoConfig(**service_info))
        assert conf.db is not None
        get_mongo_client(conf.db)
        assert options[0]["appname"] == expected

    def test_appname_without_db_section(self, options: list[dict[str, Any]]) -> None:
        """Apps with only a policy database are named after their service info."""
        conf = Config(
            service_info=ServiceInfoConfig(service=service(id="org.example.my"))
        )
        register_access_control(
            create_connexion_app(conf), None, AccessControlConfig(enabled=True)
        )
        assert options[0]["appname"] == "org.example.my"

    def test_uri_wins(
        self,
        options: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Options in the connection string win, with a warning on conflicts."""
        monkeypatch.setenv(
            "MONGO_URI",
            "mongodb://a:1,b:2/db?appName=uri&ConnectTimeoutMS=1&maxPoolSize=2",
        )
        warning = Mock()
        monkeypatch.setattr(module.logger, "warning", warning)
        get_mongo_client(
            MongoConfig(client_options={"maxPoolSize": "s3cr3t", "minPoolSize": 1})
        )
        assert options == [
            {
                "connect": True,
                "serverselectiontimeoutms": 5000,
                "minpoolsize": 1,
            }
        ]
        warning.assert_called_once()
        assert warning.call_args.args[1] == "maxPoolSize"
        assert "s3cr3t" not in str(warning.call_args)

    @pytest.mark.parametrize(
        "query",
        [
            "maxPoolSize=2;connectTimeoutMS=1;appname=uri",
            "appname=a#b&connectTimeoutMS=1&maxPoolSize=2",
            "maxPoolSize=2 connectTimeoutMS=1 appname=uri",
        ],
    )
    def test_uri_parsed_as_in_pymongo(
        self,
        options: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        query: str,
    ) -> None:
        """Each form of connection string that PyMongo accepts wins."""
        monkeypatch.setenv("MONGO_URI", f"mongodb://a/db?{query}")
        get_mongo_client(MongoConfig(client_options={"maxPoolSize": 9}))
        assert options == [{"connect": True, "serverselectiontimeoutms": 5000}]

    def test_no_warning_for_defaults(
        self,
        options: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The connection string overriding defaults isn't a conflict."""
        monkeypatch.setenv("MONGO_URI", "mongodb://a/?connectTimeoutMS=1&appname=x")
        warning = Mock()
        monkeypatch.setattr(module.logger, "warning", warning)
        get_mongo_client(MongoConfig())
        assert "connecttimeoutms" not in options[0]
        assert "appname" not in options[0]
        warning.assert_not_called()

    def test_real_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PyMongo takes the options."""
        for var in MONGO_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        client = create_mongo_client(
            host="localhost", connect=False, options={"maxPoolSize": 3}, appname="x"
        )
        try:
            assert client.options.pool_options.max_pool_size == 3
            assert client.options.server_selection_timeout == 5
            assert client.options.pool_options.connect_timeout == 5
            assert client.options.pool_options.metadata["application"] == {"name": "x"}
        finally:
            client.close()

    @pytest.mark.parametrize(
        "query",
        [
            "maxPoolSize=7;serverSelectionTimeoutMS=1000",
            "maxPoolSize=7&serverSelectionTimeoutMS=1000",
        ],
    )
    def test_real_client_uri_wins(
        self, monkeypatch: pytest.MonkeyPatch, query: str
    ) -> None:
        """PyMongo uses the options of the connection string."""
        monkeypatch.setenv("MONGO_URI", f"mongodb://localhost/db?{query}")
        client = create_mongo_client(connect=False, options={"maxPoolSize": 9})
        try:
            assert client.options.pool_options.max_pool_size == 7
            assert client.options.server_selection_timeout == 1
            assert client.options.pool_options.connect_timeout == 5
        finally:
            client.close()

    def test_aliases_not_resolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PyMongo refuses aliases with different values in both places."""
        monkeypatch.setenv("MONGO_URI", "mongodb://localhost/db?ssl=true")
        with pytest.raises(PyMongoError):
            create_mongo_client(connect=False, options={"tls": False})

    @pytest.mark.parametrize(
        ("name", "value", "error"),
        [
            ("maxPoolSize", "abc", ValueError),
            ("serverSelectionTimeoutMS", -1, ValueError),
            ("document_class", "x", TypeError),
        ],
    )
    def test_invalid_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        value: object,
        error: type[Exception],
    ) -> None:
        """Invalid values fail on creating the client."""
        for var in MONGO_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(error):
            create_mongo_client(connect=False, options={name: value})

    def test_unknown_option(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unknown options fail on creating the client."""
        for var in MONGO_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ConfigurationError, match="nonsense"):
            create_mongo_client(connect=False, options={"nonsense": 1})

    @pytest.mark.parametrize(
        "name", ["host", "PORT", "connect", "username", "Password"]
    )
    def test_reserved_option(self, name: str) -> None:
        """Options that FOCA sets are rejected."""
        with pytest.raises(ValidationError, match=name):
            MongoConfig(client_options={name: "x"})

    def test_option_set_twice(self) -> None:
        """The same option in different cases is rejected, not overwritten."""
        with pytest.raises(ValidationError, match="twice"):
            MongoConfig(client_options={"maxPoolSize": 1, "maxpoolsize": 2})

    @pytest.mark.parametrize("value", [None, ["x"], {"a": 1}])
    def test_only_scalars(self, value: object) -> None:
        """Options are scalars."""
        with pytest.raises(ValidationError):
            MongoConfig(client_options={"maxPoolSize": value})  # type: ignore[dict-item]

    def test_reopen_keeps_options(self, options: list[dict[str, Any]]) -> None:
        """Replaced clients have the same options."""
        conf = MongoConfig(client_options={"maxPoolSize": 20})
        get_mongo_client(conf)
        reopen_mongodb(conf)
        assert options[1] == {**options[0], "connect": False}


@pytest.mark.parametrize("code", [68, 85, 86, 11000])
def test_index_conflict_codes(code: int) -> None:
    """Errors of conflicts are told apart."""
    assert _create_failing_index(code) is IndexConflictError


@pytest.mark.parametrize("code", [13, None])
def test_other_index_errors(code: int | None) -> None:
    """Other errors stay what they are."""
    assert _create_failing_index(code) is OperationFailure


def _create_failing_index(code: int | None) -> type[Exception]:
    """Register a database whose indexes can't be created.

    Args:
        code: Code of the error.

    Returns:
        Type of the error raised.
    """
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value.create_index = MagicMock(
        side_effect=OperationFailure("Failed", code=code)
    )
    conf = DBConfig.model_validate(
        {"collections": {"c": {"indexes": [{"keys": {"a": 1}}]}}}
    )
    with pytest.raises(Exception) as info:
        _register_database(client, "db", conf)
    return info.type


class TestConnectionStringErrors:
    """Tests for errors of connection strings that PyMongo rejects."""

    @pytest.mark.parametrize(
        "rest",
        [
            f"u:a/{SECRET}@h/db",
            f"u:a%2F/{SECRET}@h/db",
            f"u:{SECRET}%@h",
            f"u:a%{SECRET}",
            f"u:a/URI option {SECRET}@h/db",
        ],
    )
    def test_message_replaced(self, clean_env: pytest.MonkeyPatch, rest: str) -> None:
        """Messages that may quote credentials are replaced, chain included."""
        clean_env.setenv("MONGO_URI", f"{SCHEME}{rest}")
        with pytest.raises(InvalidURI, match="MONGO_URI") as excinfo:
            create_mongo_client(connect=False)
        assert SECRET not in "".join(
            format_exception(excinfo.type, excinfo.value, excinfo.tb)
        )

    def test_netloc_not_quoted(self, clean_env: pytest.MonkeyPatch) -> None:
        """The check of the scheme doesn't fail with an error quoting credentials."""
        clean_env.setenv("MONGO_URI", f"{SCHEME}u:{SECRET}℀@h/db")
        assert get_mongo_uri() == f"{SCHEME}u:{SECRET}\u2100@h/db"

    @pytest.mark.parametrize(
        ("query", "match"),
        [
            ("tlsInsecure=true&tlsAllowInvalidCertificates=true", "tlsInsecure"),
            ("tls=true&ssl=false", "ssl and tls"),
            ("a=b&c", "key=value"),
        ],
    )
    def test_uri_option_message_kept(
        self, clean_env: pytest.MonkeyPatch, query: str, match: str
    ) -> None:
        """Conflicts of options in the connection string are told as they are."""
        clean_env.setenv("MONGO_URI", f"{SCHEME}{AUTH}h/?{query}")
        with pytest.raises(InvalidURI, match=match):
            create_mongo_client(connect=False)

    def test_conflicting_client_options(self, clean_env: pytest.MonkeyPatch) -> None:
        """Conflicts of `client_options`, which PyMongo reports alike, stay useful."""
        with pytest.raises(InvalidURI, match=r"(?i)tlsInsecure and tlsAllow"):
            create_mongo_client(
                connect=False,
                options={"tlsInsecure": True, "tlsAllowInvalidCertificates": True},
            )

    @pytest.mark.parametrize(
        ("error", "options", "match"),
        [
            (ValueError, {"maxPoolSize": "x"}, "maxpoolsize"),
            (ConfigurationError, {"nonsense": 1}, "nonsense"),
        ],
    )
    def test_other_errors_kept(
        self,
        clean_env: pytest.MonkeyPatch,
        error: type[Exception],
        options: dict[str, Any],
        match: str,
    ) -> None:
        """Other errors of options keep their messages."""
        with pytest.raises(error, match=match):
            create_mongo_client(connect=False, options=options)

    def test_port_error_kept(self, clean_env: pytest.MonkeyPatch) -> None:
        """Errors of hosts that quote nothing keep their hint on escaping."""
        clean_env.setenv("MONGO_URI", f"{SCHEME}u:{SECRET}")
        with pytest.raises(ValueError, match="must be escaped") as excinfo:
            create_mongo_client(connect=False)
        assert SECRET not in str(excinfo.value)
