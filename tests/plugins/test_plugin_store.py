"""Tests of the key-value store of plugins (`foca.plugins.store`)."""

import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import mongomock
import pymongo
import pytest
from pydantic import ValidationError
from pymongo import MongoClient, _csot
from pymongo.errors import (
    DuplicateKeyError,
    ExecutionTimeout,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)
from pymongo.read_preferences import ReadPreference

from foca import Foca
from foca import plugins as plugins_api
from foca.models.config import Config, DBConfig, MongoConfig
from foca.plugins import (
    Context,
    InboundRequest,
    Plugin,
    Store,
    StoreError,
    engine,
    runtime,
)
from foca.plugins.base import EmptyModel
from foca.plugins.config import (
    STORE_KEY_SECRET_ENV,
    PluginsConfig,
    StoreConfig,
    StoreOverrideConfig,
)
from foca.plugins.engine import PluginHost, _mongo_store_backend, _store_namespace
from foca.plugins.loader import LoadedPlugin
from foca.plugins.pytest_plugin import RegisterPlugins
from foca.plugins.runtime import GatewayApp
from foca.plugins.store import (
    MAX_KEY_LENGTH,
    STORE_COLLECTION,
    STORE_ENTRY_OVERHEAD,
    STORE_SIZES_COLLECTION,
    MemoryStoreBackend,
    MongoStoreBackend,
    StoreBackend,
    StoreFullError,
    StoreSettings,
    _aware,
    _document_size,
    _retry_upsert,
    entry_size,
)
from foca.plugins.testing import PluginHarness, make_inbound
from tests.plugins.test_plugin_runtime import write_config

MONGO_URI = "mongodb://localhost:12345"
DB_NAME = "foca_test_plugin_store"
# Arbitrary time
START = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


class Clock:
    """Clock for tests, advanced by hand.

    Attributes:
        now: Current time.
    """

    def __init__(self) -> None:
        """Start now, as mongomock removes entries by its TTL index in real time."""
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        """Get the current time.

        Returns:
            Time.
        """
        return self.now

    def advance(self, seconds: float) -> None:
        """Advance the clock.

        Args:
            seconds: Seconds.
        """
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def mongo_backend() -> Iterator[MongoStoreBackend]:
    """Backend in a MongoDB server on `localhost:12345`, with a fresh database."""
    client: MongoClient[dict[str, Any]] = MongoClient(
        MONGO_URI, serverSelectionTimeoutMS=5000
    )
    client.drop_database(DB_NAME)
    mongo = MongoStoreBackend(client[DB_NAME][STORE_COLLECTION])
    mongo.setup()
    yield mongo
    client.drop_database(DB_NAME)
    client.close()


@pytest.fixture(
    params=[
        "memory",
        "mongomock",
        pytest.param("mongodb", marks=pytest.mark.mongodb),
    ]
)
def backend(request: pytest.FixtureRequest) -> StoreBackend:
    """Backend in memory, in mongomock (as in apps' tests) or in a MongoDB server.

    Cf. `mongo_backend` for the server.
    """
    if request.param == "memory":
        return MemoryStoreBackend()
    if request.param == "mongomock":
        collection: Any = mongomock.MongoClient()["db"][STORE_COLLECTION]
        return MongoStoreBackend(collection)
    backend: MongoStoreBackend = request.getfixturevalue("mongo_backend")
    return backend


def make_store(
    backend: StoreBackend,
    clock: Clock | None = None,
    namespace: str = "ns",
    max_entry_size: int = 4096,
    max_ttl: float = 3600,
    deadline: float | None = None,
    key_secret: bytes | None = None,
    max_namespace_size: int | None = None,
) -> Store:
    """Create a store.

    Args:
        backend: Backend.
        clock: Clock; real time if not given.
        namespace: Namespace.
        max_entry_size: Maximum size of values.
        max_ttl: Maximum time to live.
        deadline: Deadline, if any.
        key_secret: Secret for `key_for()`, if any.
        max_namespace_size: Maximum size of the namespace, if any.

    Returns:
        Store.
    """
    settings = StoreSettings(
        namespace=namespace,
        mode="shared" if backend.shared else "local",
        backend=backend,
        max_entry_size=max_entry_size,
        max_ttl=max_ttl,
        key_secret=key_secret,
        max_namespace_size=max_namespace_size,
    )
    if clock is None:
        return Store(settings, deadline=deadline)
    return Store(settings, deadline=deadline, clock=clock)


class TestStore:
    """Semantics of stores, with both backends."""

    def test_set_get_delete(self, backend: StoreBackend) -> None:
        """Values round-trip as JSON; deleted keys are absent."""
        store = make_store(backend)
        assert store.get("k") is None
        assert store.get("k", default=0) == 0
        value = {"a": [1, 2.5, "ü", None, True], "b": {"c": "d"}}
        store.set("k", value)
        assert store.get("k") == value
        store.set("k", ("x",))  # replaced; tuples become lists
        assert store.get("k") == ["x"]
        assert store.delete("k") is True
        assert store.delete("k") is False
        assert store.get("k") is None

    def test_namespaces(self, backend: StoreBackend) -> None:
        """Keys of one namespace are invisible in others."""
        first = make_store(backend, namespace="a")
        second = make_store(backend, namespace="b")
        first.set("k", 1)
        assert second.get("k") is None
        assert second.increment("k") == 1
        assert first.get("k") == 1

    def test_ttl(self, backend: StoreBackend) -> None:
        """Entries expire after their TTL, capped at `max_ttl`."""
        clock = Clock()
        store = make_store(backend, clock, max_ttl=100)
        store.set("short", 1, ttl=10)
        store.set("default", 2)
        store.set("capped", 3, ttl=1000)
        store.set("float", 4, ttl=10.5)
        clock.advance(10)
        assert store.get("short") is None
        assert store.get("float") == 4
        assert store.get("default") == 2
        clock.advance(90)
        assert store.get("default") is None
        assert store.get("capped") is None
        # expired entries count as absent
        assert store.delete("default") is False
        store.set("short", 5, ttl=10)
        assert store.get("short") == 5

    def test_cap_expiry(self, backend: StoreBackend) -> None:
        """Capping cuts the expiry of a namespace's longer-lived entries."""
        clock = Clock()
        store = make_store(backend, clock, max_ttl=3600)
        other = make_store(backend, clock, namespace="other", max_ttl=3600)
        store.set("value", 1)
        store.increment("counter", by=5)
        store.set("short", 2, ttl=10)
        other.set("value", 3)
        backend.cap_expiry("ns", clock.now + timedelta(seconds=60), timeout=10)
        clock.advance(59)
        assert (store.get("value"), store.get("counter")) == (1, 5)
        assert store.get("short") is None
        clock.advance(1)
        assert (store.get("value"), store.get("counter")) == (None, None)
        assert other.get("value") == 3

    @pytest.mark.parametrize(
        "ttl", [0, 0.0004, -1, float("inf"), float("nan"), True, "1"]
    )
    def test_invalid_ttl(self, backend: StoreBackend, ttl: Any) -> None:
        """TTLs are finite numbers of at least a millisecond."""
        store = make_store(backend)
        with pytest.raises(ValueError, match="ttl"):
            store.set("k", 1, ttl=ttl)
        with pytest.raises(ValueError, match="ttl"):
            store.increment("k", ttl=ttl)

    def test_increment(self, backend: StoreBackend) -> None:
        """Counters start at 0, step by `by` and keep a fixed window."""
        clock = Clock()
        store = make_store(backend, clock)
        assert store.increment("c", ttl=60) == 1
        clock.advance(30)
        assert store.increment("c", by=5, ttl=60) == 6
        assert store.increment("c", by=-2) == 4
        assert store.get("c") == 4
        clock.advance(30)  # the window of the first increment ended
        assert store.get("c") is None
        assert store.increment("c", ttl=60) == 1

    def test_limit(self, backend: StoreBackend) -> None:
        """Increments beyond the limit leave the counter unchanged."""
        clock = Clock()
        store = make_store(backend, clock)
        assert store.increment("c", limit=2, ttl=60) == 1
        assert store.increment("c", limit=2) == 2
        assert store.increment("c", limit=2) is None
        assert store.get("c") == 2
        assert store.increment("c", by=-1, limit=2) == 1
        # a new counter that would exceed the limit is not created
        assert store.increment("new", by=3, limit=2) is None
        assert store.get("new") is None
        assert store.increment("new", limit=2) == 1
        clock.advance(60)
        assert store.increment("c", limit=2) == 1

    def test_increment_value(self, backend: StoreBackend) -> None:
        """Values are no counters, unless they expired; `set` replaces counters."""
        clock = Clock()
        store = make_store(backend, clock)
        store.set("v", 5, ttl=10)
        with pytest.raises(StoreError, match="no counter"):
            store.increment("v")
        assert store.get("v") == 5
        clock.advance(10)
        assert store.increment("v") == 1
        store.set("v", "x")
        assert store.get("v") == "x"

    def test_concurrent_increments(self, backend: StoreBackend) -> None:
        """Increments are atomic: a limit holds across threads."""
        if isinstance(getattr(backend, "collection", None), mongomock.Collection):
            pytest.skip("mongomock is not thread-safe")
        store = make_store(backend)
        results: list[int | None] = []
        lock = threading.Lock()

        def spend() -> None:
            for _ in range(5):
                result = store.increment("budget", limit=30)
                with lock:
                    results.append(result)

        threads = [threading.Thread(target=spend) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        granted = sorted(result for result in results if result is not None)
        assert granted == list(range(1, 31))
        assert results.count(None) == 20

    @pytest.mark.parametrize("key", ["", "x" * (MAX_KEY_LENGTH + 1), 1, None, "\ud800"])
    def test_invalid_keys(self, backend: StoreBackend, key: Any) -> None:
        """Keys are strings of 1 to 256 characters, in valid Unicode."""
        store = make_store(backend)
        for call in (
            lambda: store.get(key),
            lambda: store.set(key, 1),
            lambda: store.delete(key),
            lambda: store.increment(key),
        ):
            with pytest.raises(ValueError, match="Keys"):
                call()

    def test_long_key(self, backend: StoreBackend) -> None:
        """Keys of 256 characters are valid."""
        store = make_store(backend)
        store.set("x" * MAX_KEY_LENGTH, 1)
        assert store.get("x" * MAX_KEY_LENGTH) == 1

    @pytest.mark.parametrize(
        "value",
        [
            float("nan"),
            2**53,
            {1: "x"},
            {"a": {None: 1}},
            object(),
            "\ud800",
            {"x": {1, 2}},
        ],
    )
    def test_invalid_values(self, backend: StoreBackend, value: Any) -> None:
        """Values are I-JSON data with string keys."""
        store = make_store(backend)
        with pytest.raises(ValueError, match="JSON data"):
            store.set("k", value)
        assert store.get("k") is None

    def test_deep_values(self, backend: StoreBackend) -> None:
        """Values nested too deeply are refused."""
        store = make_store(backend, max_entry_size=10**6)
        value: list[Any] = []
        for _ in range(10000):
            value = [value]
        with pytest.raises(ValueError, match="JSON data"):
            store.set("k", value)

    def test_size(self, backend: StoreBackend) -> None:
        """Values take at most `max_entry_size` bytes as compact JSON."""
        store = make_store(backend, max_entry_size=10)
        store.set("k", "12345678")  # 10 bytes with quotes
        with pytest.raises(ValueError, match="exceeds 10 bytes"):
            store.set("k", "123456789")
        with pytest.raises(ValueError, match="exceeds 10 bytes"):
            store.set("k", "üüüüü")  # 12 bytes in UTF-8
        assert store.get("k") == "12345678"

    def test_size_first(
        self, backend: StoreBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Values that are too large are refused before the other checks."""
        checks = MagicMock()
        monkeypatch.setattr("foca.plugins.store.check_i_json", checks)
        store = make_store(backend, max_entry_size=10)
        with pytest.raises(ValueError, match="exceeds 10 bytes"):
            store.set("k", ["x" * 100])
        checks.assert_not_called()

    @pytest.mark.parametrize(
        ("by", "limit"),
        [(True, None), (1.0, None), (2**53, None), (1, 2**53), (1, "1")],
    )
    def test_invalid_increments(
        self, backend: StoreBackend, by: Any, limit: Any
    ) -> None:
        """Increments and limits are integers up to 2^53 - 1."""
        store = make_store(backend)
        with pytest.raises(ValueError, match="integer"):
            store.increment("k", by=by, limit=limit)

    def test_default_limit(self, backend: StoreBackend) -> None:
        """Counters stay within ±(2^53 - 1), also without limit."""
        store = make_store(backend)
        assert store.increment("c", by=2**53 - 1) == 2**53 - 1
        assert store.increment("c") is None
        assert store.increment("d", by=-(2**53 - 1)) == -(2**53 - 1)
        assert store.increment("d", by=-1) is None
        assert store.increment("e", by=-1, limit=-2) is None
        assert store.get("d") == -(2**53 - 1)

    def test_deadline(self, backend: StoreBackend) -> None:
        """Calls after the deadline fail."""
        store = make_store(backend, deadline=time.monotonic() - 1)
        for call in (
            lambda: store.get("k"),
            lambda: store.set("k", 1),
            lambda: store.delete("k"),
            lambda: store.increment("k"),
        ):
            with pytest.raises(StoreError, match="deadline"):
                call()
        assert make_store(backend, deadline=time.monotonic() + 60).get("k") is None

    def test_key_for(self, backend: StoreBackend) -> None:
        """Derived keys are hashes, per namespace and parts."""
        store = make_store(backend)
        key = store.key_for("budget", "user", "alice")
        assert key.startswith("h:")
        assert len(key) == 66
        assert "alice" not in key
        assert key == store.key_for("budget", "user", "alice")
        assert key != store.key_for("budget", "client", "alice")
        assert key != store.key_for("budget", "user", "alic", "e")
        assert key != make_store(backend, namespace="other").key_for(
            "budget", "user", "alice"
        )
        for parts in ((), ("x", 1)):
            with pytest.raises(ValueError, match="strings"):
                store.key_for(*parts)

    def test_key_for_secret(self, backend: StoreBackend) -> None:
        """With a secret, keys are HMACs with their own prefix."""
        plain = make_store(backend).key_for("budget", "user", "alice")
        store = make_store(backend, key_secret=b"s1")
        key = store.key_for("budget", "user", "alice")
        message = json.dumps(["ns", "budget", "user", "alice"]).encode()
        expected = hmac.new(b"s1", message, hashlib.sha256).hexdigest()
        assert key == f"hk:{expected}"
        assert plain.startswith("h:")
        assert not key.startswith("h:")
        assert key.removeprefix("hk:") != plain.removeprefix("h:")
        assert key == store.key_for("budget", "user", "alice")
        assert key != store.key_for("budget", "client", "alice")
        assert key != make_store(backend, key_secret=b"s2").key_for(
            "budget", "user", "alice"
        )

    def test_key_secret_rotation(self, backend: StoreBackend) -> None:
        """Adding or changing the secret makes earlier entries unreachable."""
        before = make_store(backend)
        before.set(before.key_for("budget", "alice"), 1)
        for secret in (b"s1", b"s2"):
            store = make_store(backend, key_secret=secret)
            assert store.get(store.key_for("budget", "alice")) is None
            store.set(store.key_for("budget", "alice"), 2)
        again = make_store(backend, key_secret=b"s1")
        assert again.get(again.key_for("budget", "alice")) == 2
        assert before.get(before.key_for("budget", "alice")) == 1

    def test_settings_repr_hides_secret(self) -> None:
        """The secret is not shown in the representation of the settings."""
        settings = StoreSettings(
            namespace="ns",
            mode="local",
            backend=MemoryStoreBackend(),
            max_entry_size=1,
            max_ttl=1,
            key_secret=b"topsecret",
        )
        assert "topsecret" not in repr(settings)


# bytes of an entry with a one-character key and value (e.g., `1`)
SMALL = STORE_ENTRY_OVERHEAD + 2


class TestNamespaceSize:
    """Limit of the size of namespaces (`max_namespace_size`), with all backends."""

    def test_full(self, backend: StoreBackend) -> None:
        """Writes that don't fit fail clearly; other namespaces are unaffected."""
        store = make_store(backend, max_namespace_size=3 * SMALL)
        for key in "abc":
            store.set(key, 1)
        with pytest.raises(StoreFullError, match="namespace full") as info:
            store.set("d", 1)
        assert "'d'" not in str(info.value)
        assert isinstance(info.value, StoreError)
        assert plugins_api.StoreFullError is StoreFullError
        assert store.get("d") is None
        other = make_store(backend, namespace="other", max_namespace_size=SMALL)
        other.set("d", 1)
        assert other.get("d") == 1

    def test_replace(self, backend: StoreBackend) -> None:
        """Entries may be replaced by ones that are not larger, also when full."""
        store = make_store(backend, max_namespace_size=2 * SMALL)
        store.set("a", 1)
        store.set("b", 1)
        store.set("a", 2)
        with pytest.raises(StoreFullError):
            store.set("a", 22)
        assert store.get("a") == 2

    def test_delete(self, backend: StoreBackend) -> None:
        """Deleted entries free their space."""
        store = make_store(backend, max_namespace_size=SMALL)
        store.set("a", 1)
        assert store.delete("a") is True
        store.set("b", 1)
        assert store.get("b") == 1

    def test_counters(self, backend: StoreBackend) -> None:
        """New counters need room; increments of existing ones don't."""
        counter = STORE_ENTRY_OVERHEAD + 1 + 8
        store = make_store(backend, max_namespace_size=counter + SMALL)
        assert store.increment("c") == 1
        store.set("a", 1)
        assert store.increment("c", by=5) == 6
        with pytest.raises(StoreFullError):
            store.increment("d")
        assert store.get("d") is None

    def test_too_large(self, backend: StoreBackend) -> None:
        """Entries larger than the namespace never fit."""
        store = make_store(backend, max_namespace_size=SMALL - 1)
        with pytest.raises(StoreFullError, match=f"entry of {SMALL} bytes"):
            store.set("a", 1)
        assert store.get("a") is None  # not written

    def test_unlimited(self, backend: StoreBackend) -> None:
        """Without maximum (hand-built settings), namespaces are unbounded."""
        store = make_store(backend)
        for key in range(50):
            store.set(str(key), "x" * 100)
        assert store.get("49") == "x" * 100

    def test_sizes(self) -> None:
        """Entries count the overhead and their key and value in UTF-8."""
        assert entry_size("ü", '"a"') == STORE_ENTRY_OVERHEAD + 2 + 3
        assert entry_size("k", None) == STORE_ENTRY_OVERHEAD + 1 + 8


class TestMemoryNamespaceSize:
    """Namespace sizes in the memory backend."""

    def test_expired_entries(self) -> None:
        """Expired entries are removed when the namespace is full."""
        clock = Clock()
        backend = MemoryStoreBackend()
        store = make_store(backend, clock, max_namespace_size=2 * SMALL)
        other = make_store(backend, clock, namespace="o")
        store.set("a", 1, ttl=1)
        store.set("b", 1, ttl=1)
        other.set("a", 1, ttl=1)
        clock.advance(1)
        store.set("a", 1)  # expired itself
        store.set("c", 1)
        assert backend._sizes == {"ns": 2 * SMALL, "o": SMALL}
        assert ("o", "a") in backend._entries  # other namespaces stay

    def test_full_sweeps_limited(self) -> None:
        """Full namespaces are swept at most every few seconds."""
        clock = Clock()
        backend = MemoryStoreBackend()
        store = make_store(backend, clock, max_namespace_size=SMALL)
        store.set("a", 1, ttl=1)
        with pytest.raises(StoreFullError):
            store.set("b", 1)  # swept, nothing expired
        clock.advance(1)
        with pytest.raises(StoreFullError):
            store.set("b", 1)  # not swept again yet
        backend._FULL_SWEEP_INTERVAL = 0.0
        store.set("b", 1)
        assert list(backend._entries) == [("ns", "b")]

    def test_sweep_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sweeps and reads of expired entries update the sizes."""
        monkeypatch.setattr(MemoryStoreBackend, "_SWEEP_INTERVAL", 3)
        clock = Clock()
        backend = MemoryStoreBackend()
        store = make_store(backend, clock)
        store.set("a", 1, ttl=1)
        store.set("b", 1, ttl=1)
        clock.advance(1)
        store.set("c", 1)  # third write: sweeps
        assert backend._sizes == {"ns": SMALL}
        store.set("d", 1, ttl=1)
        clock.advance(1)
        assert store.get("d") is None
        assert backend._sizes == {"ns": SMALL}


def mongomock_backend() -> MongoStoreBackend:
    """Create a backend in mongomock, in an empty database.

    Clients of mongomock share their databases, so it is emptied first.

    Returns:
        Backend.
    """
    client: Any = mongomock.MongoClient()
    client.drop_database("db")
    collection: Any = client["db"][STORE_COLLECTION]
    return MongoStoreBackend(collection)


def ns_size(backend: MongoStoreBackend) -> int:
    """Get the size of the namespace `ns` as kept by a backend.

    Args:
        backend: Backend.

    Returns:
        Size, in bytes.
    """
    document = backend.sizes.find_one({"_id": "ns"})
    assert document is not None
    return int(document["size"])


@pytest.fixture(
    params=["mongomock", pytest.param("mongodb", marks=pytest.mark.mongodb)]
)
def mongo(request: pytest.FixtureRequest) -> MongoStoreBackend:
    """MongoDB backend in mongomock or in a MongoDB server (`mongo_backend`)."""
    if request.param == "mongomock":
        return mongomock_backend()
    backend: MongoStoreBackend = request.getfixturevalue("mongo_backend")
    return backend


class TestMongoNamespaceSize:
    """Namespace sizes in the MongoDB backend."""

    def test_size_documents(self, mongo: MongoStoreBackend) -> None:
        """Sizes are kept per namespace, in their own collection."""
        backend = mongo
        store = make_store(backend, max_namespace_size=10 * SMALL)
        store.set("a", 1)
        store.increment("c")
        assert backend.sizes.name == STORE_SIZES_COLLECTION
        size = SMALL + STORE_ENTRY_OVERHEAD + 9
        # counted once, at the first write (`gen`)
        assert list(backend.sizes.find()) == [{"_id": "ns", "size": size, "gen": 1}]
        store.delete("a")
        assert ns_size(backend) == size - SMALL

    def test_recount_when_full(self, mongo: MongoStoreBackend) -> None:
        """Full namespaces are counted again, at most every few seconds."""
        backend = mongo
        store = make_store(backend, max_namespace_size=SMALL)
        store.set("a", 1)
        # as if removed by the TTL index
        backend.collection.delete_many({})
        with pytest.raises(StoreFullError):
            store.set("b", 1)  # counted at the first write, just now
        backend._RECOUNT_RETRY_INTERVAL = 0.0
        store.set("b", 1)
        assert ns_size(backend) == SMALL
        with pytest.raises(StoreFullError):
            store.set("c", 1)  # counted again, still full

    def test_periodic_recount(self, mongo: MongoStoreBackend) -> None:
        """Namespaces are counted again before the first write and periodically."""
        backend = mongo
        store = make_store(backend, max_namespace_size=10 * SMALL)
        backend.sizes.insert_one({"_id": "ns", "size": 10 * SMALL})
        store.set("a", 1)  # first write: counted
        assert ns_size(backend) == SMALL
        backend.sizes.update_one({"_id": "ns"}, {"$set": {"size": 0}})
        store.set("b", 1)
        assert ns_size(backend) == SMALL
        backend._RECOUNT_INTERVAL = 0.0
        store.set("c", 1)
        assert ns_size(backend) == 3 * SMALL

    def test_legacy_documents(self, mongo: MongoStoreBackend) -> None:
        """Entries without size (earlier versions) count the overhead only."""
        backend = mongo
        backend.collection.insert_many(
            [
                {"_id": "ns\x1fv", "ns": "ns", "key": "v", "value": "1"},
                {"_id": "ns\x1fc", "ns": "ns", "key": "c", "n": 1},
                {"_id": "ns\x1fs", "ns": "ns", "key": "s", "value": "1", "size": 70},
                {"_id": "o\x1fv", "ns": "o", "key": "v", "value": "1"},
            ]
        )
        assert backend.recount("ns") == 2 * STORE_ENTRY_OVERHEAD + 70
        assert ns_size(backend) == 2 * STORE_ENTRY_OVERHEAD + 70
        assert backend.recount("empty") == 0
        store = make_store(backend)
        backend._counted["ns"] = time.monotonic()
        store.delete("v")  # subtracts what it counted
        assert ns_size(backend) == STORE_ENTRY_OVERHEAD + 70

    def test_document_size(self) -> None:
        """Sizes of documents: stored, or the overhead for earlier versions."""
        assert _document_size(None) == 0
        assert _document_size({"size": 5}) == 5
        assert _document_size({"size": True}) == STORE_ENTRY_OVERHEAD
        assert _document_size({}) == STORE_ENTRY_OVERHEAD

    def test_failed_recount(
        self, mongo: MongoStoreBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failed counts are logged, don't fail writes, and wait for the interval.

        The namespace is then counted in the background, which may fail too.
        """
        backend = mongo
        store = make_store(backend, max_namespace_size=10 * SMALL)
        backend.sizes.insert_one({"_id": "ns", "size": 0})  # counted before
        recount = MagicMock(side_effect=ServerSelectionTimeoutError("db.internal"))
        backend.recount = recount  # type: ignore[method-assign]
        store.set("a", 1)
        background = backend._background["ns"]
        background.join(5)
        assert not background.is_alive()
        store.set("b", 1)
        assert recount.call_count == 2  # in the call, then in the background
        assert "namespace ns not counted again (ServerSelectionTimeoutError)" in (
            caplog.text
        )
        assert (
            "namespace ns not counted again in the background"
            " (ServerSelectionTimeoutError)"
        ) in caplog.text
        assert "db.internal" not in caplog.text
        assert ns_size(backend) == 2 * SMALL

    def test_recount_in_background(self, mongo: MongoStoreBackend) -> None:
        """Counts that fail in the call (e.g., too slow) succeed in the background.

        So that entries removed by the TTL index don't keep counting, and a
        namespace isn't counted by two threads at a time.
        """
        backend = mongo
        store = make_store(backend, max_namespace_size=10 * SMALL)
        store.set("a", 1)
        store.set("b", 1)
        backend.collection.delete_one({"key": "a"})  # as if expired
        original = backend.recount
        release = threading.Event()
        calls: list[str] = []

        def slow(ns: str) -> int:
            calls.append(ns)
            if len(calls) == 1:
                raise ExecutionTimeout("too slow")
            release.wait(5)
            return original(ns)

        backend.recount = slow  # type: ignore[method-assign]
        backend._RECOUNT_INTERVAL = 0.0
        store.set("c", 1)  # fails in the call, starts the background count
        thread = backend._background["ns"]
        backend._recount_in_background("ns")  # one at a time
        assert backend._background["ns"] is thread
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert calls == ["ns", "ns"]
        assert ns_size(backend) == 2 * SMALL

    def test_background_count_ignores_deadline(self, mongo: MongoStoreBackend) -> None:
        """Background counts don't inherit the deadline of the call."""
        backend = mongo
        deadlines: list[float | None] = []

        def recount(ns: str) -> int:
            deadlines.append(_csot.remaining())
            return 0

        backend.recount = recount  # type: ignore[method-assign]
        with pymongo.timeout(0.001):
            backend._recount_in_background("ns")
        thread = backend._background["ns"]
        thread.join(5)
        assert not thread.is_alive()
        remaining = deadlines[0]
        assert remaining is not None
        assert remaining > 1

    def test_thread_not_started(
        self, mongo: MongoStoreBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If no thread can be started, the failure is logged, not raised."""
        backend = mongo

        def fail(self: threading.Thread) -> None:
            raise RuntimeError("can't start new thread")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(threading.Thread, "start", fail)
            backend._recount_in_background("ns")
        assert "ns" not in backend._background
        assert (
            "namespace ns not counted in the background (can't start new thread)"
        ) in caplog.text

    def test_concurrent_writes_while_counting(self, mongo: MongoStoreBackend) -> None:
        """Sizes added while a count runs are kept (also of this process)."""
        backend = mongo
        store = make_store(backend, max_namespace_size=10 * SMALL)
        store.set("a", 1)
        original = backend.collection.aggregate

        def concurrent(*args: Any, **kwargs: Any) -> Any:
            result = list(original(*args, **kwargs))
            # writes after the entries were read
            store.set("b", 1)
            store.set("c", 1)
            return iter(result)

        backend.collection.aggregate = concurrent  # type: ignore[method-assign]
        assert backend.recount("ns") == SMALL
        assert ns_size(backend) == 3 * SMALL

    def test_concurrent_counts(self, mongo: MongoStoreBackend) -> None:
        """Concurrent counts correct the size once."""
        backend = mongo
        store = make_store(backend, max_namespace_size=10 * SMALL)
        store.set("a", 1)
        backend.sizes.update_one({"_id": "ns"}, {"$set": {"size": 5 * SMALL}})
        original = backend.collection.aggregate
        nested: list[int] = []

        def concurrent(*args: Any, **kwargs: Any) -> Any:
            result = list(original(*args, **kwargs))
            if not nested:  # another count, after this one read the size
                nested.append(0)
                nested[0] = backend.recount("ns")
            return iter(result)

        backend.collection.aggregate = concurrent  # type: ignore[method-assign]
        assert backend.recount("ns") == SMALL
        assert nested == [SMALL]
        assert ns_size(backend) == SMALL

    def test_concurrent_first_counts(self, mongo: MongoStoreBackend) -> None:
        """A first count that loses to a concurrent one leaves its size."""
        backend = mongo
        backend.collection.insert_one(
            {"_id": "ns\x1fa", "ns": "ns", "key": "a", "value": "1", "size": SMALL}
        )
        original = backend.collection.aggregate
        nested: list[int] = []

        def concurrent(*args: Any, **kwargs: Any) -> Any:
            result = list(original(*args, **kwargs))
            if not nested:
                nested.append(0)
                nested[0] = backend.recount("ns")
            return iter(result)

        backend.collection.aggregate = concurrent  # type: ignore[method-assign]
        assert backend.recount("ns") == SMALL
        assert ns_size(backend) == SMALL

    def test_deleted_size(self, mongo: MongoStoreBackend) -> None:
        """A deleted size is counted again at the next write."""
        backend = mongo
        store = make_store(backend, max_namespace_size=10 * SMALL)
        store.set("a", 1)
        store.set("b", 1)
        backend.sizes.delete_one({"_id": "ns"})
        store.set("c", 1)  # inserts the size of its entry only
        assert ns_size(backend) == SMALL
        store.set("d", 1)  # counts the namespace again first
        assert ns_size(backend) == 4 * SMALL

    def test_failed_shrinking_write(self, mongo: MongoStoreBackend) -> None:
        """Writes that would shrink the namespace but fail don't change its size."""
        backend = mongo
        store = make_store(backend, max_namespace_size=10 * SMALL)
        store.set("a", 123)
        size = ns_size(backend)

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise ExecutionTimeout("too slow")

        backend.collection.find_one_and_replace = fail  # type: ignore[method-assign]
        with pytest.raises(StoreError):
            store.set("a", 1)
        assert ns_size(backend) == size

    def test_grow_at_the_limit(self, mongo: MongoStoreBackend) -> None:
        """Sizes grow up to the limit exactly, and not beyond."""
        backend = mongo
        assert backend._grow("ns", 6, 10) is True  # inserts the size
        assert backend._grow("ns", 4, 10) is True
        assert backend._grow("ns", 1, 10) is False
        assert ns_size(backend) == 10

    def test_changed_in_between(self, mongo: MongoStoreBackend) -> None:
        """Entries written between read and replacement are accounted for."""
        backend = mongo
        store = make_store(backend, max_namespace_size=10 * SMALL)
        store.set("x", 1)
        original = backend.collection.find_one_and_replace

        def concurrent(*args: Any, **kwargs: Any) -> Any:
            # another process writes a larger value first
            original(
                {"_id": "ns\x1fa"},
                {"ns": "ns", "key": "a", "value": "123", "size": SMALL + 2},
                upsert=True,
            )
            backend.sizes.update_one({"_id": "ns"}, {"$inc": {"size": SMALL + 2}})
            return original(*args, **kwargs)

        backend.collection.find_one_and_replace = concurrent  # type: ignore[method-assign]
        store.set("a", 1)
        assert ns_size(backend) == 2 * SMALL

    def test_concurrent_size_insert(self, mongo: MongoStoreBackend) -> None:
        """A size inserted concurrently is grown, if it stays within bounds."""
        backend = mongo
        sizes = MagicMock()
        sizes.update_one.side_effect = [
            DuplicateKeyError("E11000"),
            MagicMock(matched_count=1),
            DuplicateKeyError("E11000"),
            MagicMock(matched_count=0),
        ]
        backend.sizes = sizes
        backend._counted["ns"] = time.monotonic()
        assert backend._grow("ns", 1, 10) is True
        assert backend._grow("ns", 1, 10) is False
        # the retries keep the bound of the size
        bounded = {"_id": "ns", "size": {"$lte": 9}}
        assert [call.args[0] for call in sizes.update_one.call_args_list] == [
            bounded
        ] * 4

    def test_new_counter_races(self, mongo: MongoStoreBackend) -> None:
        """Counters that replace expired entries or lose a race keep sizes right."""
        clock = Clock()
        backend = mongo
        store = make_store(backend, clock, max_namespace_size=10 * SMALL)
        counter = STORE_ENTRY_OVERHEAD + 1 + 8
        store.set("c", 1, ttl=1)
        clock.advance(1)
        assert store.increment("c") == 1  # replaces the expired value
        assert ns_size(backend) == counter
        store.delete("c")
        original = backend.collection.insert_one
        calls: list[Any] = []

        def racing(document: Any, *args: Any, **kwargs: Any) -> Any:
            # another process creates the counter first, once
            if not calls:
                calls.append(document)
                original({**document, "n": 5}, *args, **kwargs)
                backend.sizes.update_one({"_id": "ns"}, {"$inc": {"size": counter}})
            return original(document, *args, **kwargs)

        backend.collection.insert_one = racing  # type: ignore[method-assign]
        assert store.increment("c") == 6
        assert ns_size(backend) == counter


class TestMemoryBackend:
    """Specifics of the memory backend."""

    def test_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Writes remove expired entries from time to time."""
        monkeypatch.setattr(MemoryStoreBackend, "_SWEEP_INTERVAL", 3)
        backend = MemoryStoreBackend()
        clock = Clock()
        store = make_store(backend, clock)
        store.set("a", 1, ttl=1)
        store.increment("b", ttl=1)
        clock.advance(1)
        store.set("c", 1)  # third write
        assert list(backend._entries) == [("ns", "c")]

    def test_lock_timeout(self) -> None:
        """Calls wait for the lock at most until the deadline."""
        backend = MemoryStoreBackend()
        calls: list[Callable[[Store], Any]] = [
            lambda store: store.get("k"),
            lambda store: store.set("k", 1),
            lambda store: store.delete("k"),
            lambda store: store.increment("k"),
        ]
        with backend._lock:
            for call in calls:
                store = make_store(backend, deadline=time.monotonic() + 0.02)
                with pytest.raises(StoreError, match="lock not acquired"):
                    call(store)


class TestMongoBackend:
    """Specifics of the MongoDB backend."""

    def test_setup(self) -> None:
        """Setup creates the TTL and namespace indexes, idempotently."""
        collection: Any = mongomock.MongoClient()["db"][STORE_COLLECTION]
        backend = MongoStoreBackend(collection)
        backend.setup()
        backend.setup()
        indexes = collection.index_information()
        assert indexes["expires_at_ttl"]["expireAfterSeconds"] == 0
        assert indexes["ns"]["key"] == [("ns", 1)]

    @pytest.mark.mongodb
    def test_documents(self, mongo_backend: MongoStoreBackend) -> None:
        """Documents hold namespace, key, JSON or count, and expiry."""
        backend = mongo_backend
        clock = Clock()
        store = make_store(backend, clock)
        store.set("v", {"a": 1}, ttl=60)
        store.increment("c", ttl=60)
        documents = {
            document["_id"]: document for document in backend.collection.find()
        }
        assert documents["ns\x1fv"]["value"] == '{"a":1}'
        assert documents["ns\x1fv"]["ns"] == "ns"
        assert documents["ns\x1fv"]["key"] == "v"
        assert "n" not in documents["ns\x1fv"]
        assert documents["ns\x1fc"]["n"] == 1
        assert "value" not in documents["ns\x1fc"]
        assert _aware(documents["ns\x1fc"]["expires_at"]) == clock.now + timedelta(
            seconds=60
        )
        assert set(documents["ns\x1fc"]) == {
            "_id",
            "ns",
            "key",
            "n",
            "expires_at",
            "size",
        }
        # sizes: overhead, key and value (counters: 8 bytes)
        assert documents["ns\x1fv"]["size"] == STORE_ENTRY_OVERHEAD + 1 + 7
        assert documents["ns\x1fc"]["size"] == STORE_ENTRY_OVERHEAD + 1 + 8

    @pytest.mark.mongodb
    def test_rejected_new_counter(self, mongo_backend: MongoStoreBackend) -> None:
        """New counters beyond the limit leave no document."""
        store = make_store(mongo_backend, Clock())
        assert store.increment("c", by=5, limit=1) is None
        assert mongo_backend.collection.find_one({"_id": "ns\x1fc"}) is None

    @pytest.mark.mongodb
    def test_datetime_options(self) -> None:
        """Results don't depend on the client's datetime options."""
        client: MongoClient[dict[str, Any]] = MongoClient(
            MONGO_URI, serverSelectionTimeoutMS=5000, datetime_conversion="DATETIME_MS"
        )
        try:
            client.drop_database(DB_NAME)
            backend = MongoStoreBackend(client[DB_NAME][STORE_COLLECTION])
            store = make_store(backend, Clock())
            assert [store.increment("c", limit=2) for _ in range(3)] == [1, 2, None]
            store.set("v", 1)
            with pytest.raises(StoreError, match="no counter"):
                store.increment("v")
            assert store.delete("v") is True
        finally:
            client.drop_database(DB_NAME)
            client.close()

    def test_contention(self) -> None:
        """Increments retry if entries change in between, up to a bound.

        The sequence of calls is deliberate: contention is hard to force on a
        server.
        """
        collection = MagicMock()
        collection.with_options.return_value = collection
        collection.find_one_and_update.return_value = None
        # a live counter within the bounds (changed after the update), then
        # none (also no expired entry), as created and removed by others
        collection.find_one.side_effect = [{"n": 0}, *[None] * 8]
        collection.insert_one.side_effect = DuplicateKeyError("dup")
        collection.find_one_and_replace.return_value = None
        store = make_store(MongoStoreBackend(collection))
        with pytest.raises(StoreError, match="busy"):
            store.increment("k")
        assert collection.find_one_and_update.call_count == 5

    def test_lazy_indexes(self) -> None:
        """Writes create the indexes until that succeeded once (call counts)."""
        collection = MagicMock()
        collection.with_options.return_value = collection
        collection.create_index.side_effect = [
            ServerSelectionTimeoutError("down"),
            None,
            None,
        ]
        collection.find_one_and_update.return_value = {"n": 1}
        backend = MongoStoreBackend(collection)
        with pytest.raises(ServerSelectionTimeoutError):
            backend.setup()
        store = make_store(backend)
        store.set("k", 1)
        store.increment("c")
        assert collection.create_index.call_count == 3

    @pytest.mark.mongodb
    def test_timeout(self, mongo_backend: MongoStoreBackend) -> None:
        """Calls that take too long raise `StoreError`."""
        with pytest.raises(StoreError, match="failed") as info:
            mongo_backend.get("ns", "k", START, 1e-6)
        assert info.value.__cause__ is not None

    def test_errors(self) -> None:
        """Database errors become `StoreError`s, without their messages."""
        collection = MagicMock()
        collection.with_options.return_value = collection
        error = ServerSelectionTimeoutError("secret detail")
        collection.find_one.side_effect = error
        collection.replace_one.side_effect = error
        collection.find_one_and_delete.side_effect = error
        collection.find_one_and_update.side_effect = error
        store = make_store(MongoStoreBackend(collection))
        for call in (
            lambda: store.get("k"),
            lambda: store.set("k", 1),
            lambda: store.delete("k"),
            lambda: store.increment("k"),
        ):
            with pytest.raises(
                StoreError, match=r"^Plugin store failed: ServerSelectionTimeoutError$"
            ):
                call()

    def test_unchecked_documents(self) -> None:
        """`get` trusts the query's filter; `delete` checks the deleted expiry."""
        collection = MagicMock()
        collection.with_options.return_value = collection
        collection.find_one.return_value = {"_id": "x", "value": "1"}
        collection.find_one_and_delete.return_value = {"_id": "x"}
        clock = Clock()
        store = make_store(MongoStoreBackend(collection), clock)
        assert store.get("k") == 1
        query = collection.find_one.call_args.args[0]
        assert query == {"_id": "ns\x1fk", "expires_at": {"$gt": clock.now}}
        assert store.delete("k") is False

    def test_primary_reads(self) -> None:
        """Entries are read from the primary, whatever the client's options."""
        client: MongoClient[dict[str, Any]] = MongoClient(
            MONGO_URI, readPreference="secondaryPreferred", connect=False
        )
        try:
            backend = MongoStoreBackend(client["db"][STORE_COLLECTION])
            assert backend.collection.read_preference == ReadPreference.PRIMARY
        finally:
            client.close()

    def test_setup_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Setup gives up after `_SETUP_TIMEOUT`, e.g., at startup."""
        monkeypatch.setattr(MongoStoreBackend, "_SETUP_TIMEOUT", 0.2)
        client: MongoClient[dict[str, Any]] = MongoClient(
            "mongodb://127.0.0.1:1", serverSelectionTimeoutMS=60000, connect=False
        )
        try:
            backend = MongoStoreBackend(client["db"][STORE_COLLECTION])
            start = time.monotonic()
            with pytest.raises(PyMongoError):
                backend.setup()
            assert time.monotonic() - start < 10
        finally:
            client.close()

    @pytest.mark.parametrize(
        "kind", ["mongomock", pytest.param("mongodb", marks=pytest.mark.mongodb)]
    )
    def test_without_expiry(self, request: pytest.FixtureRequest, kind: str) -> None:
        """Entries without valid expiry count as absent, also for counters."""
        backend: MongoStoreBackend
        if kind == "mongomock":
            collection: Any = mongomock.MongoClient()["db"][STORE_COLLECTION]
            backend = MongoStoreBackend(collection)
        else:
            backend = request.getfixturevalue("mongo_backend")
        for expiry in ({}, {"expires_at": "never"}):
            backend.collection.replace_one(
                {"_id": "ns\x1fc"},
                {"ns": "ns", "key": "c", "n": 5, **expiry},
                upsert=True,
            )
            store = make_store(backend)
            assert store.get("c") is None
            assert store.increment("c") == 1
            assert store.get("c") == 1

    def test_naive_times(self) -> None:
        """Naive times from MongoDB are UTC."""
        naive = datetime(2026, 1, 1, 12)
        assert _aware(naive) == datetime(2026, 1, 1, 12, tzinfo=UTC)
        assert _aware(START) is START
        assert _aware("2026") is None

    def test_retry_upsert(self) -> None:
        """Upserts are retried once after concurrent inserts of the key."""
        operation = MagicMock(side_effect=[DuplicateKeyError("dup"), "ok"])
        assert _retry_upsert(operation) == "ok"
        operation = MagicMock(side_effect=DuplicateKeyError("dup"))
        with pytest.raises(DuplicateKeyError):
            _retry_upsert(operation)
        assert operation.call_count == 2


class CountingPlugin(Plugin[EmptyModel, EmptyModel]):
    """Count requests in the store."""

    data_stored = ("request counts",)

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Count the request and remember the store.

        Args:
            request: Inbound request.
            ctx: Context.
        """
        assert ctx.store is not None
        count = ctx.store.increment("requests")
        ctx.record("count", count)
        ctx.record("mode", ctx.store.mode)
        ctx.record("namespace", ctx.store.namespace)


class QuietPlugin(Plugin[EmptyModel, EmptyModel]):
    """Declare no stored data."""


def loaded(plugin_id: str, distribution: str | None) -> LoadedPlugin:
    """Build a loaded plugin.

    Args:
        plugin_id: Identifier.
        distribution: Distribution, if any.

    Returns:
        Plugin.
    """
    conf = PluginsConfig.model_validate({"available": {plugin_id: {}}})
    return LoadedPlugin(
        id=plugin_id,
        cls=CountingPlugin,
        instance=CountingPlugin(plugin_id, EmptyModel()),
        distribution=distribution,
        version="1.0",
        entry_point="x:CountingPlugin",
        record_digest=None,
        available=conf.available[plugin_id],
    )


class FalsyBackend(MemoryStoreBackend):
    """Memory backend that is falsy."""

    def __bool__(self) -> bool:
        """Be falsy.

        Returns:
            `False`.
        """
        return False


class TestEngine:
    """Stores of plugin calls."""

    def test_harness(self) -> None:
        """Hooks get a store; the harness shares its entries with tests."""
        harness = PluginHarness(CountingPlugin)
        harness.run_request(make_inbound({}))
        assert harness.executions[0].annotations == {
            "count": 1,
            "mode": "local",
            "namespace": "plugin:test.plugin",
        }
        harness.run_request(make_inbound({}))
        assert harness.store.get("requests") == 2

    def test_shared(self) -> None:
        """With a shared backend, hosts share the entries."""
        backend = MongoStoreBackend(mongomock.MongoClient()["db"][STORE_COLLECTION])
        harness = PluginHarness(CountingPlugin, store_backend=backend)
        harness.run_request(make_inbound({}))
        assert harness.executions[0].annotations["mode"] == "shared"
        assert harness.executions[0].annotations["count"] == 1
        other = PluginHarness(CountingPlugin, store_backend=backend)
        other.run_request(make_inbound({}))
        assert other.executions[0].annotations["count"] == 2
        assert harness.store.get("requests") == 2

    def test_falsy_backend(self) -> None:
        """Backends are used even if they are falsy (e.g., define `__len__`)."""
        backend = FalsyBackend()
        harness = PluginHarness(CountingPlugin, store_backend=backend)
        assert harness.host.store_settings("test.plugin").backend is backend

    def test_off(self, caplog: pytest.LogCaptureFixture) -> None:
        """Plugins whose store is off keep entries in memory; splits are logged."""
        backend = MongoStoreBackend(MagicMock())
        conf = PluginsConfig.model_validate(
            {
                "available": {
                    "test.off": {"store": {"enabled": False, "max_ttl": 5}},
                    "test.on": {"store": {"max_namespace_size": 1000}},
                },
                "defaults": {"store": {"max_entry_size": 10}},
                "pipeline": [],
            }
        )
        plugins = {
            plugin_id: LoadedPlugin(
                **{**loaded(plugin_id, "dist").__dict__, "available": entry}
            )
            for plugin_id, entry in conf.available.items()
            if plugin_id.startswith("test.")
        }
        host = PluginHost(conf, api="tes", plugins=plugins, store_backend=backend)
        try:
            off = host.store_settings("test.off")
            on = host.store_settings("test.on")
            assert (off.mode, off.max_entry_size, off.max_ttl) == ("off", 10, 5)
            assert isinstance(off.backend, MemoryStoreBackend)
            assert (on.mode, on.max_entry_size, on.max_ttl) == ("shared", 10, 2592000)
            assert on.backend is backend
            assert (off.max_namespace_size, on.max_namespace_size) == (16777216, 1000)
            assert "namespace dist keep their store entries apart (off, shared)" in (
                caplog.text
            )
            store = host.store("test.off")
            store.set("k", 1)
            assert store.get("k") == 1
            with pytest.raises(KeyError):
                host.store("test.unknown")
        finally:
            host.close()

    def test_local_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Setup warns once about plugins that store data without database."""
        message = "keep their store entries in the memory of each process"
        plugins = {"test.count": loaded("test.count", "dist")}
        conf = PluginsConfig.model_validate(
            {"available": {"test.count": {}}, "pipeline": []}
        )
        for backend, warned in (
            (MemoryStoreBackend(), True),
            (MongoStoreBackend(mongomock.MongoClient()["db"][STORE_COLLECTION]), False),
        ):
            caplog.clear()
            host = PluginHost(conf, api="tes", plugins=plugins, store_backend=backend)
            try:
                host.setup_store()
            finally:
                host.close()
            assert caplog.text.count(message) == (1 if warned else 0)
        # plugins that declare no stored data
        caplog.clear()
        quiet = {"test.quiet": replace(loaded("test.quiet", "dist"), cls=QuietPlugin)}
        host = PluginHost(conf, api="tes", plugins=quiet)
        try:
            host.setup_store()
        finally:
            host.close()
        assert message not in caplog.text

    @pytest.mark.parametrize(
        ("conf_secret", "env_secret", "expected"),
        [
            (None, None, None),
            (None, "", None),
            ("", None, None),
            ("", "from-env", b"from-env"),
            (None, "from-env", b"from-env"),
            ("from-conf", "from-env", b"from-conf"),
        ],
    )
    def test_key_secret(
        self,
        monkeypatch: pytest.MonkeyPatch,
        conf_secret: str | None,
        env_secret: str | None,
        expected: bytes | None,
    ) -> None:
        """The key secret is from the configuration, else the environment."""
        if env_secret is not None:
            monkeypatch.setenv(STORE_KEY_SECRET_ENV, env_secret)
        else:
            monkeypatch.delenv(STORE_KEY_SECRET_ENV, raising=False)
        store: dict[str, Any] = {}
        if conf_secret is not None:
            store["key_secret"] = conf_secret
        conf = PluginsConfig.model_validate(
            {
                "available": {"test.count": {}},
                "defaults": {"store": store},
                "pipeline": [],
            }
        )
        plugins = {"test.count": loaded("test.count", "dist")}
        host = PluginHost(conf, api="tes", plugins=plugins)
        try:
            settings = host.store_settings("test.count")
            assert settings.key_secret == expected
            key = host.store("test.count").key_for("a")
            assert key.startswith("hk:" if expected else "h:")
        finally:
            host.close()

    def test_key_secret_warning(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setup warns once if database-backed stores have no key secret.

        Only plugins that declare stored data (`data_stored`) count, as for
        the warning about stores in memory.
        """
        monkeypatch.delenv(STORE_KEY_SECRET_ENV, raising=False)
        message = "plugins.defaults.store.key_secret"
        counting = loaded("test.count", "dist")
        quiet = replace(counting, cls=QuietPlugin)
        mongo = MongoStoreBackend(mongomock.MongoClient()["db"][STORE_COLLECTION])
        cases: list[tuple[StoreBackend, dict[str, Any], LoadedPlugin, int]] = [
            (mongo, {}, counting, 1),
            (mongo, {"key_secret": "s3cr3t-value"}, counting, 0),
            (MemoryStoreBackend(), {}, counting, 0),  # no database
            (mongo, {"enabled": False}, counting, 0),  # entries in memory
            (mongo, {}, quiet, 0),  # no stored data declared
        ]
        for backend, store, plugin, count in cases:
            plugins = {"test.count": plugin}
            caplog.clear()
            conf = PluginsConfig.model_validate(
                {
                    "available": {"test.count": {}},
                    "defaults": {"store": store},
                    "pipeline": [],
                }
            )
            host = PluginHost(conf, api="tes", plugins=plugins, store_backend=backend)
            try:
                host.setup_store()
            finally:
                host.close()
            assert caplog.text.count(message) == count
            assert caplog.text.count(STORE_KEY_SECRET_ENV) == count
            assert "s3cr3t-value" not in caplog.text

    def test_key_secret_not_exposed(self) -> None:
        """The secret is masked in the configuration and in its errors."""
        conf = PluginsConfig.model_validate(
            {"defaults": {"store": {"key_secret": "s3cr3t-value"}}}
        )
        assert "s3cr3t-value" not in repr(conf)
        assert "s3cr3t-value" not in str(conf.defaults.store)
        assert "s3cr3t-value" not in conf.model_dump_json()
        numeric = PluginsConfig.model_validate(
            {"defaults": {"store": {"key_secret": 12345678}}}
        )
        assert numeric.defaults.store.key_secret is not None
        assert numeric.defaults.store.key_secret.get_secret_value() == "12345678"
        for bad in (True, ["x"]):
            with pytest.raises(ValidationError):
                PluginsConfig.model_validate(
                    {"defaults": {"store": {"key_secret": bad}}}
                )
        with pytest.raises(ValidationError):  # not per plugin
            PluginsConfig.model_validate(
                {"available": {"a.b": {"store": {"key_secret": "x"}}}}
            )

    @pytest.mark.parametrize(
        "plugins",
        [
            # a model-level check fails; the secret is last in the input
            {
                "pipeline": [{"plugin": "nope.notallowed"}],
                "defaults": {"store": {"key_secret": "Zq9xK2mP4vB7nR1t"}},
            },
            # the secret itself is invalid
            {"defaults": {"store": {"key_secret": ["Zq9xK2mP4vB7nR1t"]}}},
        ],
    )
    def test_key_secret_not_in_errors(self, plugins: dict[str, Any]) -> None:
        """Messages of validation errors don't show the secret."""
        with pytest.raises(ValidationError) as plugins_error:
            PluginsConfig.model_validate(plugins)
        with pytest.raises(ValidationError) as config_error:
            Config.model_validate({"plugins": plugins})
        for error in (plugins_error, config_error):
            assert "Zq9xK2mP4vB7nR1t" not in str(error.value)

    def test_setup_caps_expiry(self) -> None:
        """Setup cuts entries to the current `max_ttl` of their namespace."""
        backend = MongoStoreBackend(mongomock.MongoClient()["db"][STORE_COLLECTION])
        clock = Clock()
        make_store(backend, clock, namespace="dist", max_ttl=3600).set("k", 1)
        conf = PluginsConfig.model_validate(
            {
                "available": {
                    "test.a": {"store": {"max_ttl": 60}},
                    "test.b": {"store": {"max_ttl": 120}},
                    "test.off": {"store": {"enabled": False, "max_ttl": 600}},
                },
                "pipeline": [],
            }
        )
        plugins = {
            plugin_id: replace(loaded(plugin_id, "dist"), available=entry)
            for plugin_id, entry in conf.available.items()
            if plugin_id.startswith("test.")
        }
        host = PluginHost(conf, api="tes", plugins=plugins, store_backend=backend)
        try:
            host.setup_store()
        finally:
            host.close()
        document = backend.collection.find_one({"_id": "dist\x1fk"})
        assert document is not None
        # the longest `max_ttl` of the namespace's shared plugins
        expires_at = _aware(document["expires_at"])
        assert expires_at is not None
        remaining = expires_at - datetime.now(UTC)
        assert timedelta(seconds=110) < remaining <= timedelta(seconds=120)

    def test_cap_errors(self, log: pytest.LogCaptureFixture) -> None:
        """Errors of capping are logged per namespace; the others are capped."""
        failures: dict[str, PyMongoError | None] = {
            "a": OperationFailure("denied", code=13),
            "b": None,
            "c": ServerSelectionTimeoutError("db.internal:27017"),
        }
        calls: list[str] = []

        class Backend(MemoryStoreBackend):
            def cap_expiry(self, ns: str, expires_at: datetime, timeout: float) -> None:
                calls.append(ns)
                error = failures.get(ns)
                if error is not None:
                    raise error

        conf = PluginsConfig.model_validate(
            {"available": {f"test.{ns}": {} for ns in failures}, "pipeline": []}
        )
        plugins = {f"test.{ns}": loaded(f"test.{ns}", ns) for ns in failures}
        host = PluginHost(conf, api="tes", plugins=plugins, store_backend=Backend())
        try:
            host.setup_store()
        finally:
            host.close()
        assert calls == ["a", "b", "c"]
        records = [r for r in log.records if "not capped" in r.getMessage()]
        assert [r.levelname for r in records] == ["ERROR", "WARNING"]
        assert "namespace a" in records[0].getMessage()
        assert "code 13: denied" in records[0].getMessage()
        assert "namespace c" in records[1].getMessage()
        assert "db.internal" not in records[1].getMessage()

    def test_cap_without_indexes(
        self, log: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Capping is tried without indexes, within one deadline for all."""
        calls: list[str] = []

        class Backend(MemoryStoreBackend):
            def setup(self) -> None:
                raise OperationFailure("conflict", code=85)

            def cap_expiry(self, ns: str, expires_at: datetime, timeout: float) -> None:
                calls.append(ns)

        conf = PluginsConfig.model_validate(
            {"available": {"test.a": {}, "test.b": {}}, "pipeline": []}
        )
        plugins = {f"test.{ns}": loaded(f"test.{ns}", ns) for ns in "ab"}
        for timeout, expected in ((10.0, ["a", "b"]), (0.0, [])):
            calls.clear()
            log.clear()
            monkeypatch.setattr(engine, "_STORE_SETUP_TIMEOUT", timeout)
            host = PluginHost(conf, api="tes", plugins=plugins, store_backend=Backend())
            try:
                host.setup_store()
            finally:
                host.close()
            assert calls == expected
            assert "code 85: conflict" in log.text
            assert ("namespaces a, b not capped" in log.text) is (not expected)

    def test_namespaces(self) -> None:
        """Namespaces are normalized distributions; FOCA's are `foca`."""
        assert _store_namespace(loaded("test.x", "My_Plugins")) == "my-plugins"
        assert _store_namespace(loaded("test.x", None)) == "plugin:test.x"
        assert _store_namespace(loaded("foca.routing.ordered", None)) == "foca"

    def test_hand_built_context(self) -> None:
        """Contexts built by hand have no store."""
        ctx = Context(
            api="tes",
            plugin_id="x",
            principal=MagicMock(),
            params=MagicMock(),
            original={},
            deadline=0,
            state={},
        )
        assert ctx.store is None


class TestConfig:
    """Store settings."""

    def test_defaults(self) -> None:
        """Defaults: on, 4 KiB, 16 MiB, 30 days; overrides replace set keys only."""
        defaults = StoreConfig()
        assert (
            defaults.enabled,
            defaults.max_entry_size,
            defaults.max_namespace_size,
            defaults.max_ttl,
        ) == (True, 4096, 16777216, 2592000)
        assert StoreOverrideConfig().apply(defaults) == defaults
        assert StoreOverrideConfig(max_ttl=60).apply(defaults) == StoreConfig(
            max_ttl=60
        )

    @pytest.mark.parametrize(
        "store",
        [
            {"max_ttl": 0},
            {"max_ttl": "inf"},
            {"max_ttl": 1e12},
            {"max_entry_size": -1},
            {"max_namespace_size": 0},
            {"unknown": 1},
        ],
    )
    def test_invalid(self, store: dict[str, Any]) -> None:
        """Limits are positive; unknown keys are errors."""
        with pytest.raises(ValueError, match="store"):
            PluginsConfig.model_validate({"defaults": {"store": store}})
        with pytest.raises(ValueError, match="store"):
            PluginsConfig.model_validate({"available": {"x.y": {"store": store}}})


class TestFromConfig:
    """Backends from the FOCA configuration."""

    def test_no_database(self) -> None:
        """Without databases, stores are local."""
        assert _mongo_store_backend(None) is None
        assert _mongo_store_backend(MongoConfig()) is None
        assert _mongo_store_backend(MongoConfig(dbs={})) is None

    def test_skipped_databases(self) -> None:
        """Databases that aren't the app's (e.g., access control's) are skipped."""
        database: Any = mongomock.MongoClient()["mine"]
        conf = MongoConfig(dbs={"policies": DBConfig(), "mine": DBConfig()})
        conf.dbs["mine"].client = database  # type: ignore[index]
        backend = _mongo_store_backend(conf, skip={"policies"})
        assert backend is not None
        assert backend.collection.database is database
        assert _mongo_store_backend(conf, skip={"policies", "mine"}) is None

    def test_app_policy_database(
        self,
        tmp_path: Path,
        fake_plugin_entry_points: RegisterPlugins,
        mongo_clients: list[Any],
    ) -> None:
        """An app database that also holds the policies holds the entries."""
        fake_plugin_entry_points({"test.count": CountingPlugin})
        data: dict[str, Any] = {
            "db": {"dbs": {"access_control_db": {}}},
            "security": {
                "auth": {
                    "issuers": ["https://login.example.org/oidc/"],
                    "audience": ["my-app"],
                },
                "access_control": {"enabled": True},
            },
            "plugins": {"available": {"test.count": {}}, "pipeline": ["test.count"]},
        }
        foca = Foca(
            config_file=write_config(tmp_path, data), gateway=GatewayApp(api="tes")
        )
        try:
            foca.create_app()
            assert foca.conf.security.access_control.added_database is None
            _, client = mongo_clients[0]
            assert STORE_COLLECTION in (
                client["access_control_db"].list_collection_names()
            )
        finally:
            runtime.reset()

    @pytest.mark.parametrize("app_db", [False, True])
    def test_access_control_database(
        self,
        tmp_path: Path,
        fake_plugin_entry_points: RegisterPlugins,
        mongo_clients: list[Any],
        app_db: bool,
    ) -> None:
        """The policy database that access control adds holds no entries."""
        fake_plugin_entry_points({"test.count": CountingPlugin})
        data: dict[str, Any] = {
            "security": {
                "auth": {
                    "issuers": ["https://login.example.org/oidc/"],
                    "audience": ["my-app"],
                },
                "access_control": {"enabled": True},
            },
            "plugins": {"available": {"test.count": {}}, "pipeline": ["test.count"]},
        }
        if app_db:
            data["db"] = {"dbs": {"mine": {}}}
        foca = Foca(
            config_file=write_config(tmp_path, data), gateway=GatewayApp(api="tes")
        )
        try:
            foca.create_app()
            assert foca.conf.db is not None
            assert "access_control_db" in (foca.conf.db.dbs or {})
            host = PluginHost.from_config(foca.conf, api="tes")
            try:
                assert host.store_settings("test.count").mode == (
                    "shared" if app_db else "local"
                )
            finally:
                host.close()
            for _, client in mongo_clients:
                assert STORE_COLLECTION not in (
                    client["access_control_db"].list_collection_names()
                )
            if app_db:
                _, client = mongo_clients[0]
                assert STORE_COLLECTION in client["mine"].list_collection_names()
        finally:
            runtime.reset()

    def test_registered(self) -> None:
        """The collection is in the first registered database."""
        database: Any = mongomock.MongoClient()["first"]
        conf = MongoConfig(dbs={"first": DBConfig(), "second": DBConfig()})
        conf.dbs["first"].client = database  # type: ignore[index]
        backend = _mongo_store_backend(conf)
        assert backend is not None
        assert backend.collection.name == STORE_COLLECTION
        assert backend.collection.database is database

    def test_unregistered(self, mongo_clients: list[Any]) -> None:
        """Databases that are not registered yet use the app's client."""
        backend = _mongo_store_backend(MongoConfig(dbs={"mine": DBConfig()}))
        assert backend is not None
        assert backend.collection.database.name == "mine"
        assert len(mongo_clients) == 1

    def test_host(self, mongo_clients: list[Any]) -> None:
        """Hosts from configurations with databases share entries."""
        config = Config.model_validate({"db": {"dbs": {"mine": {}}}})
        host = PluginHost.from_config(config, api="tes")
        other = PluginHost.from_config(config, api="tes")
        try:
            assert host.store_settings("foca.routing.ordered").mode == "shared"
            host.store("foca.routing.ordered").set("k", 1)
            assert other.store("foca.routing.ordered").get("k") == 1
        finally:
            host.close()
            other.close()
        host = PluginHost.from_config(Config(), api="tes")
        try:
            assert host.store_settings("foca.routing.ordered").mode == "local"
        finally:
            host.close()

    def test_create_app(
        self,
        tmp_path: Path,
        fake_plugin_entry_points: RegisterPlugins,
        mongo_clients: list[Any],
    ) -> None:
        """Gateway apps create the indexes of the store at startup."""
        fake_plugin_entry_points({"test.count": CountingPlugin})
        config = write_config(
            tmp_path,
            {
                "db": {"dbs": {"mine": {}}},
                "plugins": {
                    "available": {"test.count": {}},
                    "pipeline": ["test.count"],
                },
            },
        )
        try:
            Foca(config_file=config, gateway=GatewayApp(api="tes")).create_app()
            _, client = mongo_clients[0]
            indexes = client["mine"][STORE_COLLECTION].index_information()
            assert "expires_at_ttl" in indexes
        finally:
            runtime.reset()

    def test_create_app_without_indexes(
        self,
        tmp_path: Path,
        fake_plugin_entry_points: RegisterPlugins,
        mongo_clients: list[Any],
        log: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Apps start if the indexes can't be created, with a warning."""

        def fail(self: MongoStoreBackend) -> None:
            raise ServerSelectionTimeoutError("down")

        monkeypatch.setattr(MongoStoreBackend, "setup", fail)
        fake_plugin_entry_points({"test.count": CountingPlugin})
        config = write_config(
            tmp_path,
            {
                "db": {"dbs": {"mine": {}}},
                "plugins": {
                    "available": {"test.count": {}},
                    "pipeline": ["test.count"],
                },
            },
        )
        try:
            Foca(config_file=config, gateway=GatewayApp(api="tes")).create_app()
            assert "Indexes of the plugin store not created at startup" in log.text
        finally:
            runtime.reset()

    @pytest.mark.parametrize(
        ("code", "level"),
        [
            (13, "ERROR"),
            (18, "ERROR"),
            (85, "ERROR"),
            (86, "ERROR"),
            (11600, "WARNING"),
        ],
    )
    def test_create_app_setup_error_levels(
        self,
        tmp_path: Path,
        fake_plugin_entry_points: RegisterPlugins,
        mongo_clients: list[Any],
        log: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        code: int,
        level: str,
    ) -> None:
        """Permanent setup errors are errors, others warnings, with their cause.

        E.g., `InterruptedAtShutdown` (11600) is no permanent error.
        """

        def fail(self: MongoStoreBackend) -> None:
            raise OperationFailure("the cause", code=code)

        monkeypatch.setattr(MongoStoreBackend, "setup", fail)
        fake_plugin_entry_points({"test.count": CountingPlugin})
        config = write_config(
            tmp_path,
            {
                "db": {"dbs": {"mine": {}}},
                "plugins": {
                    "available": {"test.count": {}},
                    "pipeline": ["test.count"],
                },
            },
        )
        try:
            Foca(config_file=config, gateway=GatewayApp(api="tes")).create_app()
            records = [
                record
                for record in log.records
                if "Indexes of the plugin store not created at startup"
                in record.getMessage()
            ]
            assert [record.levelname for record in records] == [level]
            message = records[0].getMessage()
            assert f"code {code}" in message
            # server messages only for permanent errors; others may name hosts
            assert ("the cause" in message) is (level == "ERROR")
        finally:
            runtime.reset()
