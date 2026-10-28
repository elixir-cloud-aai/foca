"""Tests of the key-value store of plugins (`foca.plugins.store`)."""

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import mongomock
import pytest
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError

from foca import Foca
from foca.models.config import Config, DBConfig, MongoConfig
from foca.plugins import Context, InboundRequest, Plugin, Store, StoreError, runtime
from foca.plugins.base import EmptyModel
from foca.plugins.config import PluginsConfig, StoreConfig, StoreOverrideConfig
from foca.plugins.engine import PluginHost, _mongo_store_backend, _store_namespace
from foca.plugins.loader import LoadedPlugin
from foca.plugins.pytest_plugin import RegisterPlugins
from foca.plugins.runtime import GatewayApp
from foca.plugins.store import (
    MAX_KEY_LENGTH,
    STORE_COLLECTION,
    MemoryStoreBackend,
    MongoStoreBackend,
    StoreBackend,
    StoreSettings,
    _aware,
    _retry_upsert,
)
from foca.plugins.testing import PluginHarness, make_inbound
from tests.plugins.test_plugin_runtime import write_config

MONGO_URI = "mongodb://localhost:12345"
DB_NAME = "foca_test_plugin_store"
START = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


class Clock:
    """Clock for tests, advanced by hand.

    Attributes:
        now: Current time.
    """

    def __init__(self) -> None:
        """Start at `START`."""
        self.now = START

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


@pytest.fixture(params=["memory", pytest.param("mongodb", marks=pytest.mark.mongodb)])
def backend(request: pytest.FixtureRequest) -> StoreBackend:
    """Backend in memory, or in a MongoDB server (cf. `mongo_backend`)."""
    if request.param == "memory":
        return MemoryStoreBackend()
    backend: MongoStoreBackend = request.getfixturevalue("mongo_backend")
    return backend


def make_store(
    backend: StoreBackend,
    clock: Clock | None = None,
    namespace: str = "ns",
    max_entry_size: int = 4096,
    max_ttl: float = 3600,
    deadline: float | None = None,
) -> Store:
    """Create a store.

    Args:
        backend: Backend.
        clock: Clock; real time if not given.
        namespace: Namespace.
        max_entry_size: Maximum size of values.
        max_ttl: Maximum time to live.
        deadline: Deadline, if any.

    Returns:
        Store.
    """
    settings = StoreSettings(
        namespace=namespace,
        mode="shared" if backend.shared else "local",
        backend=backend,
        max_entry_size=max_entry_size,
        max_ttl=max_ttl,
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

    @pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True, "1"])
    def test_invalid_ttl(self, backend: StoreBackend, ttl: Any) -> None:
        """TTLs are positive, finite numbers of seconds."""
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
        """Counters stay within 2^53 - 1 without limit."""
        store = make_store(backend)
        assert store.increment("c", by=2**53 - 1) == 2**53 - 1
        assert store.increment("c") is None

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
        assert _aware(documents["ns\x1fc"]["expires_at"]) == START + timedelta(
            seconds=60
        )
        assert set(documents["ns\x1fc"]) == {"_id", "ns", "key", "n", "expires_at"}

    @pytest.mark.mongodb
    def test_rejected_new_counter(self, mongo_backend: MongoStoreBackend) -> None:
        """New counters beyond the limit leave an expired document only."""
        store = make_store(mongo_backend, Clock())
        assert store.increment("c", by=5, limit=1) is None
        document = mongo_backend.collection.find_one({"_id": "ns\x1fc"})
        assert document is not None
        assert _aware(document["expires_at"]) == START
        assert "n" not in document

    @pytest.mark.mongodb
    def test_timeout(self, mongo_backend: MongoStoreBackend) -> None:
        """Calls that take too long raise `StoreError`."""
        with pytest.raises(StoreError, match="failed") as info:
            mongo_backend.get("ns", "k", START, 1e-6)
        assert info.value.__cause__ is not None

    def test_errors(self) -> None:
        """Database errors become `StoreError`s, without their messages."""
        collection = MagicMock()
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

    def test_invalid_documents(self) -> None:
        """Documents without valid expiry count as expired."""
        collection = MagicMock()
        collection.find_one.return_value = {"_id": "x", "value": "1"}
        collection.find_one_and_delete.return_value = {"_id": "x"}
        collection.find_one_and_update.return_value = {"_id": "x", "value": "1"}
        store = make_store(MongoStoreBackend(collection), Clock())
        assert store.get("k") == 1  # the query filters by expiry
        assert store.delete("k") is False
        assert store.increment("k") == 1

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
        backend.increment = MagicMock(return_value=7)  # type: ignore[method-assign]
        harness = PluginHarness(CountingPlugin, store_backend=backend)
        harness.run_request(make_inbound({}))
        assert harness.executions[0].annotations["mode"] == "shared"
        assert harness.executions[0].annotations["count"] == 7

    def test_off(self) -> None:
        """Plugins whose store is off keep entries in memory."""
        backend = MongoStoreBackend(MagicMock())
        conf = PluginsConfig.model_validate(
            {
                "available": {
                    "test.off": {"store": {"enabled": False, "max_ttl": 5}},
                    "test.on": {},
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
            store = host.store("test.off")
            store.set("k", 1)
            assert store.get("k") == 1
            with pytest.raises(KeyError):
                host.store("test.unknown")
        finally:
            host.close()

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
        """Defaults: on, 4 KiB, 30 days; overrides replace set keys only."""
        defaults = StoreConfig()
        assert (defaults.enabled, defaults.max_entry_size, defaults.max_ttl) == (
            True,
            4096,
            2592000,
        )
        assert StoreOverrideConfig().apply(defaults) == defaults
        assert StoreOverrideConfig(max_ttl=60).apply(defaults) == StoreConfig(
            max_ttl=60
        )

    @pytest.mark.parametrize(
        "store", [{"max_ttl": 0}, {"max_entry_size": -1}, {"unknown": 1}]
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
        try:
            assert host.store_settings("foca.routing.ordered").mode == "shared"
        finally:
            host.close()
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
