"""Tests of `foca.testing.pytest_plugin`, which `tests/conftest.py` enables."""

import os
import socket
import sys
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from pymongo import MongoClient

from foca.database.register_mongodb import create_mongo_client
from foca.testing import pytest_plugin

REAL_MONGODB = "mongodb://localhost:12345/?serverSelectionTimeoutMS=5000"
# Reserved for documentation (RFC 5737): nothing is reachable there
OUTSIDE = ("192.0.2.1", 80)


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    """Socket listening on the local host."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield sock


class TestNetworkGuard:
    """Tests of the network guard."""

    def test_blocks_other_hosts(self) -> None:
        """Connections to other hosts fail."""
        with socket.socket() as sock:
            with pytest.raises(OSError, match="blocked"):
                sock.connect(OUTSIDE)
            with pytest.raises(OSError, match="blocked"):
                sock.connect_ex(OUTSIDE)

    def test_allows_local_host(self, listener: socket.socket) -> None:
        """Connections to the local host work."""
        with socket.socket() as sock:
            sock.connect(listener.getsockname())
        with socket.socket() as sock:
            assert sock.connect_ex(listener.getsockname()) == 0

    @pytest.mark.parametrize(
        ("host", "local"),
        [
            ("127.0.0.1", True),
            ("127.1.2.3", True),
            ("::1", True),
            ("::1%lo", True),
            ("localhost", True),
            ("LOCALHOST", True),
            ("localhost.", True),
            ("0.0.0.0", True),  # noqa: S104 (not a binding)
            ("", True),
            ("example.org", False),
            ("192.0.2.1", False),
        ],
    )
    def test_is_local(self, host: str, local: bool) -> None:
        """Only the local host counts as local."""
        assert pytest_plugin._is_local(host) is local

    def test_unix_socket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sockets of other families aren't checked."""
        connect = MagicMock()
        monkeypatch.setattr(socket.socket, "connect", connect)
        pytest_plugin.install_network_guard(monkeypatch)
        sock = MagicMock(family=socket.AF_UNIX)
        socket.socket.connect(sock, "/run/app.sock")
        connect.assert_called_once_with(sock, "/run/app.sock")

    def test_unsets_proxies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Settings of proxies are unset."""
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        pytest_plugin.install_network_guard(monkeypatch)
        assert "HTTPS_PROXY" not in os.environ

    def test_allows_test_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Connections to the test database work."""
        monkeypatch.setenv(pytest_plugin.MONGODB_TEST_URI, "mongodb://192.0.2.1:80")
        connect = MagicMock()
        monkeypatch.setattr(socket.socket, "connect", connect)
        pytest_plugin.install_network_guard(monkeypatch)
        with socket.socket() as sock:
            sock.connect(OUTSIDE)
            connect.assert_called_once()
            with pytest.raises(OSError, match="blocked"):
                sock.connect(("192.0.2.2", 80))

    def test_resolves_test_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The hosts of the test database count as named and as resolved."""
        monkeypatch.setenv(pytest_plugin.MONGODB_TEST_URI, "mongodb://resolved:1")
        info = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 1))
        monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [info])
        assert pytest_plugin._get_test_database_addresses() == {
            ("resolved", 1),
            ("192.0.2.1", 1),
        }

    def test_resolves_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hosts are only resolved once."""
        monkeypatch.setenv(pytest_plugin.MONGODB_TEST_URI, "mongodb://once:1")
        lookup = MagicMock(return_value=[])
        monkeypatch.setattr(socket, "getaddrinfo", lookup)
        pytest_plugin._get_test_database_addresses()
        pytest_plugin._get_test_database_addresses()
        lookup.assert_called_once()

    def test_unresolved_test_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hosts that can't be resolved aren't an error of the guard."""
        monkeypatch.setenv(pytest_plugin.MONGODB_TEST_URI, "mongodb://unresolved:1")
        monkeypatch.setattr(socket, "getaddrinfo", MagicMock(side_effect=OSError))
        assert pytest_plugin._get_test_database_addresses() == {("unresolved", 1)}

    def test_invalid_test_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An invalid connection string isn't an error of tests without MongoDB."""
        monkeypatch.setenv(pytest_plugin.MONGODB_TEST_URI, "nonsense")
        assert pytest_plugin._get_test_database_addresses() == frozenset()

    def test_no_test_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without the test database, there is no address to allow."""
        monkeypatch.delenv(pytest_plugin.MONGODB_TEST_URI, raising=False)
        assert pytest_plugin._get_test_database_addresses() == frozenset()

    def test_guard_on(self) -> None:
        """Tests are guarded."""
        assert socket.socket.connect.__name__ == "_guarded_connect"

    @pytest.mark.allow_network
    def test_marker_lifts_guard(self) -> None:
        """The marker lifts the guard."""
        assert socket.socket.connect.__name__ == "connect"


class TestMongodb:
    """Tests of the fixture `mongodb`, in memory."""

    @pytest.fixture(autouse=True)
    def _no_test_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run without a real database."""
        monkeypatch.delenv(pytest_plugin.MONGODB_TEST_URI, raising=False)

    def test_used_by_foca(self, mongodb: MongoClient[dict[str, Any]]) -> None:
        """The clients of FOCA see the same data, also after closing one."""
        client = create_mongo_client(host="ignored")
        client["db"]["coll"].insert_one({"a": 1})
        client.close()
        assert mongodb["db"]["coll"].count_documents({}) == 1

    def test_empty_first(self, mongodb: MongoClient[dict[str, Any]]) -> None:
        """Writes..."""
        assert mongodb["db"]["coll"].count_documents({}) == 0
        mongodb["db"]["coll"].insert_one({"a": 1})

    def test_empty_second(self, mongodb: MongoClient[dict[str, Any]]) -> None:
        """... don't reach the next test."""
        assert mongodb["db"]["coll"].count_documents({}) == 0

    def test_settings_of_deployments_unset(
        self, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
    ) -> None:
        """The settings of deployments don't reach the tests."""
        monkeypatch.setenv("MONGO_URI", "mongodb://deployment/db")
        request.getfixturevalue("mongodb")
        assert "MONGO_URI" not in os.environ


@pytest.mark.mongodb
def test_mongodb_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a real MongoDB, only databases that the test wrote to are dropped."""
    monkeypatch.setenv(pytest_plugin.MONGODB_TEST_URI, REAL_MONGODB)
    kept, new, other = "foca_plugin_kept", "foca_plugin_new", "foca_plugin_other"
    outside: MongoClient[dict[str, Any]] = MongoClient(REAL_MONGODB)
    try:
        outside[kept]["coll"].insert_one({"a": 1})
        fixture = pytest_plugin.use_mongodb(monkeypatch)
        client = next(fixture)
        client[kept]["coll"].insert_one({"a": 2})
        # Clients of FOCA, one of which is closed, as on reopening
        foca_client = create_mongo_client(host="ignored")
        foca_client[new]["coll"].insert_one({"a": 1})
        foca_client.close()
        client[new]["coll"].find_one({})
        # Not of this test
        outside[other]["coll"].insert_one({"a": 1})
        with pytest.raises(StopIteration):
            next(fixture)
        names = outside.list_database_names()
        assert kept in names
        assert other in names
        assert new not in names
    finally:
        for name in (kept, new, other):
            outside.drop_database(name)
        outside.close()


@pytest.mark.mongodb
@pytest.mark.parametrize(
    "write",
    [
        lambda db: db.create_collection("coll"),
        lambda db: db["coll"].create_index("a"),
        lambda db: db["coll"].find_one_and_update(
            {"a": 1}, {"$set": {"a": 2}}, upsert=True
        ),
        lambda db: db["coll"].update_one({"a": 1}, {"$set": {"a": 2}}, upsert=True),
        lambda db: db.command(
            "findandmodify", "coll", query={}, update={"a": 1}, upsert=True
        ),
    ],
)
def test_mongodb_real_writes(
    monkeypatch: pytest.MonkeyPatch, write: Callable[[Any], Any]
) -> None:
    """Databases that come into being by any of the usual commands are dropped."""
    monkeypatch.setenv(pytest_plugin.MONGODB_TEST_URI, REAL_MONGODB)
    name = "foca_plugin_writes"
    fixture = pytest_plugin.use_mongodb(monkeypatch)
    write(next(fixture)[name])
    with pytest.raises(StopIteration):
        next(fixture)
    outside: MongoClient[dict[str, Any]] = MongoClient(REAL_MONGODB)
    try:
        assert name not in outside.list_database_names()
    finally:
        outside.drop_database(name)
        outside.close()


def test_mongodb_needs_mongomock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without mongomock, tests are skipped, saying why."""
    monkeypatch.delenv(pytest_plugin.MONGODB_TEST_URI, raising=False)
    monkeypatch.setitem(sys.modules, "mongomock", None)
    with pytest.raises(pytest.skip.Exception, match="needs mongomock"):
        next(pytest_plugin.use_mongodb(monkeypatch))
