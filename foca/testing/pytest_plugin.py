"""Pytest plugin for the tests of FOCA apps (needs pytest).

Not registered automatically; enable it in `conftest.py`:

```python
pytest_plugins = ["foca.testing.pytest_plugin"]
```

- Network guard, for all tests: `connect()` to other hosts than the local one and
  the test database fails. It catches accidental connections, but isn't a sandbox.
  Mark tests with `allow_network` to lift it.
- `mongodb`: MongoDB that FOCA uses in a test; needs mongomock, or the environment
  variable `MONGODB_TEST_URI` for a real MongoDB.
- `issuer`: local identity provider (cf. :py:mod:`foca.testing.issuer`).
"""

import ipaddress
import os
import socket
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

import pytest
from pymongo import MongoClient, monitoring
from pymongo.errors import PyMongoError
from pymongo.uri_parser import parse_uri

from foca.database import register_mongodb
from foca.testing.issuer import Issuer
from foca.testing.issuer import issuer as run_issuer

MONGODB_TEST_URI = "MONGODB_TEST_URI"
"""Environment variable with the URI of a MongoDB to test against."""

# Settings of deployments, which must not reach the tests
_MONGO_ENV_VARS = (
    "MONGO_URI",
    "MONGO_HOST",
    "MONGO_PORT",
    "MONGO_DBNAME",
    "MONGO_USERNAME",
    "MONGO_PASSWORD",
    "MONGO_URI_FILE",
    "MONGO_USERNAME_FILE",
    "MONGO_PASSWORD_FILE",
)
# Would send requests to the local issuer elsewhere, or others past the guard
_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
# Lowercase names of the usual commands by which a database comes into being
_WRITE_COMMANDS = frozenset(
    {
        "create",
        "createindexes",
        "findandmodify",
        "insert",
        "update",
    }
)


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker of the plugin.

    Args:
        config: Configuration of pytest.
    """
    config.addinivalue_line(
        "markers", "allow_network: lift the network guard of FOCA's pytest plugin."
    )


def _is_local(host: str) -> bool:
    """Check whether a host in a socket address is the local one.

    Args:
        host: Host name or IP address.

    Returns:
        Whether it is `localhost`, a loopback address, or an unspecified one, for
        which connections go to the local host (e.g., `0.0.0.0` or an empty one).
    """
    if not host or host.lower().rstrip(".") == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.partition("%")[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


@lru_cache
def _get_addresses(uri: str) -> frozenset[tuple[str, int]]:
    """Get the socket addresses of the hosts of a connection string.

    Only once for each connection string, as this may involve DNS lookups.

    Args:
        uri: Connection string.

    Returns:
        Hosts in the connection string, as named there and as resolved, as far as
        they are. Invalid strings have none, and fail in the fixture `mongodb`.
    """
    try:
        nodes = parse_uri(uri)["nodelist"]
    except (PyMongoError, ValueError):
        return frozenset()
    addresses: set[tuple[str, int]] = set()
    for host, port in nodes:
        addresses.add((host, port))
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:  # Connections fail with a clearer message
            continue
        addresses.update((str(info[4][0]), port) for info in infos)
    return frozenset(addresses)


def _get_test_database_addresses() -> frozenset[tuple[str, int]]:
    """Get the socket addresses of the test database.

    Returns:
        Addresses of the hosts in its connection string, if there is one.
    """
    uri = os.environ.get(MONGODB_TEST_URI)
    return _get_addresses(uri) if uri else frozenset()


def install_network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail connections to other hosts than the local one and the test database.

    Also unsets the settings of proxies, which would send requests past the guard
    and those to a local issuer elsewhere.

    Args:
        monkeypatch: Monkeypatch, which restores the methods of `socket.socket` and
            the environment variables.
    """
    for name in _PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    allowed = _get_test_database_addresses()

    def _check(sock: socket.socket, address: Any) -> None:
        """Raise an error for addresses that are not allowed."""
        if (
            sock.family in (socket.AF_INET, socket.AF_INET6)
            and not _is_local(str(address[0]))
            and tuple(address[:2]) not in allowed
        ):
            raise OSError(f"Network access blocked in tests: {address}")

    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def _guarded_connect(sock: socket.socket, address: Any) -> None:
        """Connect only to addresses that are allowed."""
        _check(sock, address)
        connect(sock, address)

    def _guarded_connect_ex(sock: socket.socket, address: Any) -> int:
        """Connect only to addresses that are allowed."""
        _check(sock, address)
        return connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)


@pytest.fixture(autouse=True)
def foca_network_guard(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail connections to other hosts than the local one and the test database.

    Tests marked with `allow_network` are exempt.

    Args:
        request: Request of the test, for its markers.
        monkeypatch: Monkeypatch, which restores the methods of `socket.socket`.
    """
    if not request.node.get_closest_marker("allow_network"):
        install_network_guard(monkeypatch)


class _WrittenDatabases(monitoring.CommandListener):
    """Listener recording the databases that a client writes to.

    Attributes:
        names: Names of the databases.
    """

    def __init__(self) -> None:
        """Start with no databases."""
        self.names: set[str] = set()

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        """Record the database of a write.

        Args:
            event: Event of the started command.
        """
        if event.command_name.lower() in _WRITE_COMMANDS:
            self.names.add(event.database_name)

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        """Ignore finished commands.

        Args:
            event: Event of the finished command.
        """

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        """Ignore failed commands.

        Args:
            event: Event of the failed command.
        """


def use_mongodb(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MongoClient[dict[str, Any]]]:
    """Make FOCA use a MongoDB for tests (cf. the fixture `mongodb`).

    Args:
        monkeypatch: Monkeypatch, which restores FOCA's MongoDB client and the
            environment variables with the settings of deployments.

    Yields:
        A client of that MongoDB. Clients that FOCA creates are others, as FOCA
        also closes them, for the same data.
    """
    for name in _MONGO_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    uri = os.environ.get(MONGODB_TEST_URI)
    if uri:
        yield from _use_real_mongodb(monkeypatch, uri)
        return
    # Not a requirement of FOCA, so only if used
    mongomock = pytest.importorskip(
        "mongomock",
        reason=f"The fixture 'mongodb' needs mongomock or {MONGODB_TEST_URI}",
    )
    # Otherwise, mongomock shares data between clients for the same host
    store = mongomock.store.ServerStore()

    def _create_client(*_args: Any, **_kwargs: Any) -> Any:
        """Create a client of the in-memory MongoDB."""
        return mongomock.MongoClient(_store=store)

    monkeypatch.setattr(register_mongodb, "MongoClient", _create_client)
    yield _create_client()


def _use_real_mongodb(
    monkeypatch: pytest.MonkeyPatch, uri: str
) -> Iterator[MongoClient[dict[str, Any]]]:
    """Make FOCA use a real MongoDB for tests, and clean up after them.

    Args:
        monkeypatch: Monkeypatch, which restores FOCA's MongoDB client.
        uri: Connection string of the MongoDB.

    Yields:
        A client of that MongoDB.
    """
    written = _WrittenDatabases()
    clients: list[MongoClient[dict[str, Any]]] = []

    def _create_client(
        *_args: Any, connect: bool = True, **_kwargs: Any
    ) -> MongoClient[dict[str, Any]]:
        """Create a client that records the databases that it writes to."""
        client: MongoClient[dict[str, Any]] = MongoClient(
            uri, connect=connect, event_listeners=[written]
        )
        clients.append(client)
        return client

    client = _create_client()
    kept = set(client.list_database_names())
    monkeypatch.setattr(register_mongodb, "MongoClient", _create_client)
    try:
        yield client
    finally:
        cleaner: MongoClient[dict[str, Any]] = MongoClient(uri)
        try:
            for name in written.names - kept:
                cleaner.drop_database(name)
        finally:
            cleaner.close()
            for created in clients:
                created.close()


@pytest.fixture
def mongodb(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MongoClient[dict[str, Any]]]:
    """Make FOCA use a MongoDB for tests.

    With the environment variable `MONGODB_TEST_URI`, that MongoDB: the
    databases that the test creates through this fixture or FOCA are dropped
    afterwards, for which its user needs the right to, and others are kept (but
    not protected from the test, so use a MongoDB without other data). Otherwise,
    an empty in-memory MongoDB of mongomock, for each test.

    Args:
        monkeypatch: Monkeypatch, which restores FOCA's MongoDB client and the
            environment variables with the settings of deployments.

    Yields:
        A client of that MongoDB.
    """
    yield from use_mongodb(monkeypatch)


@pytest.fixture
def issuer() -> Iterator[Issuer]:
    """Run a local identity provider for a test.

    Yields:
        The identity provider (cf. :py:class:`foca.testing.issuer.Issuer`).
    """
    with run_issuer() as instance:
        yield instance
