"""Key-value store of plugins (`ctx.store`).

Plugins keep small data across requests, processes and replicas in a
store: e.g., caches of lookups and counters for budgets of third-party
APIs. Each distribution of plugins has a namespace of its own (FOCA's
built-in plugins: `foca`), so plugins cannot read the data of other
distributions.

With a database (the app's first one of `db.dbs`), all processes share
the entries of the collection :py:data:`STORE_COLLECTION`; without one, or
for plugins whose store the operator turned off
(`plugins.available.<id>.store.enabled`), each process keeps its own
entries in memory. Every entry expires, after `max_ttl` seconds at the
latest. Each namespace holds at most `max_namespace_size` bytes (counted
approximately, cf. :py:data:`STORE_ENTRY_OVERHEAD`); writes that would
exceed it raise :py:class:`StoreFullError`.

```python
if ctx.principal.subject is None:  # anonymous: no per-user budget
    raise NotApplicable("lookups need an authenticated caller")
count = ctx.store.increment(
    ctx.store.key_for("lookups", ctx.principal.subject_type, ctx.principal.subject),
    ttl=86400,
    limit=100,
)
if count is None:
    raise NotApplicable("daily lookup budget used up")
location = ctx.store.get(f"geo:{host}")
if location is None:
    location = lookup(host)
    try:
        ctx.store.set(f"geo:{host}", location, ttl=3600)
    except StoreFullError:  # go on without caching
        pass
```
"""

import contextvars
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import pymongo
from bson.codec_options import CodecOptions, DatetimeConversion
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_preferences import ReadPreference

from foca.plugins.base import PluginError
from foca.plugins.selection import MAX_SAFE_INTEGER, check_i_json

logger = logging.getLogger(__name__)

STORE_COLLECTION = "plugin_store"
"""Name of the collection of the store, in the app's first database."""

STORE_SIZES_COLLECTION = "plugin_store_sizes"
"""Name of the collection of the sizes of the namespaces of the store, in
the database of :py:data:`STORE_COLLECTION`: `{_id: <namespace>, size}`."""

STORE_INDEXES: list[dict[str, Any]] = [
    # removes expired entries (MongoDB checks about every 60 s)
    {
        "keys": [("expires_at", pymongo.ASCENDING)],
        "options": {"name": "expires_at_ttl", "expireAfterSeconds": 0},
    },
    # entries by namespace, e.g., for cleanups
    {"keys": [("ns", pymongo.ASCENDING)], "options": {"name": "ns"}},
]
"""Indexes of the collection of the store, as for `create_index()`."""

MIN_TTL = 0.001
"""Minimum time to live of entries, in seconds (times are stored in
milliseconds)."""

MAX_KEY_LENGTH = 256
"""Maximum length of keys, in characters."""

STORE_ENTRY_OVERHEAD = 64
"""Bytes that each entry counts towards the size of its namespace, besides
its key and value (UTF-8; counters: 8 bytes), for the space that an entry
takes beyond them (e.g., its expiry and identifier)."""

_COUNTER_SIZE = 8

_SIZE_FIELDS = {"size": True}
"""Fields of documents that :py:func:`_document_size` needs."""

StoreMode = Literal["shared", "local", "off"]
"""Where a plugin's entries are kept: `shared` (database), `local` (memory
of the process, as no database is configured) or `off` (memory of the
process, as the operator turned the store off for the plugin)."""

# Separates namespace and key in identifiers of entries; no namespace has it
_SEPARATOR = "\x1f"


class StoreError(PluginError):
    """The store failed, e.g., the database is unreachable or too slow.

    Raised by `ctx.store`; like any other exception of a hook, it fails the
    alternative.
    """


class StoreFullError(StoreError):
    """The namespace has no room for an entry (`max_namespace_size`).

    Raised by `ctx.store.set()` and by `ctx.store.increment()` for new
    counters; the entry is left unchanged. Plugins that use the store as a
    cache may catch it and go on without caching.
    """


def entry_size(key: str, value: str | None) -> int:
    """Get the bytes that an entry counts towards the size of its namespace.

    Args:
        key: Key.
        value: Value as compact JSON; `None` for counters.

    Returns:
        :py:data:`STORE_ENTRY_OVERHEAD`, plus the bytes of the key and of
        the value (counters: 8) in UTF-8.
    """
    value_size = _COUNTER_SIZE if value is None else len(value.encode())
    return STORE_ENTRY_OVERHEAD + len(key.encode()) + value_size


def _full(size: int, max_size: int) -> StoreFullError:
    """Build the error for entries that don't fit into their namespace.

    The message leaves out the key, which may hold personal data.

    Args:
        size: Bytes that the entry counts.
        max_size: Maximum size of the namespace, in bytes.

    Returns:
        Error.
    """
    return StoreFullError(
        f"Plugin store namespace full: entry of {size} bytes does not fit"
        f" into max_namespace_size of {max_size} bytes"
    )


@dataclass(frozen=True)
class StoreEntry:
    """Entry as kept by a backend.

    Attributes:
        value: Value as compact JSON; `None` for counters.
        count: Value of a counter; `None` for values.
        expires_at: Time at which the entry expires (UTC).
    """

    value: str | None
    count: int | None
    expires_at: datetime


class StoreBackend(Protocol):
    """Backend of stores: keeps the entries of all namespaces.

    Arguments are checked by :py:class:`Store`. Times are UTC, with
    millisecond precision; entries that expired (`expires_at <= now`)
    count as absent.

    Attributes:
        shared: Whether the processes of the app share the entries.
    """

    shared: bool

    def setup(self) -> None:
        """Prepare the backend, e.g., create indexes, at startup."""

    def cap_expiry(self, ns: str, expires_at: datetime, timeout: float) -> None:
        """Cut the expiry of a namespace's entries that expire later.

        Called at startup, so that a lowered `max_ttl` applies to entries
        written before.

        Args:
            ns: Namespace.
            expires_at: Latest expiry.
            timeout: Seconds that the call may take.
        """

    def get(
        self, ns: str, key: str, now: datetime, timeout: float
    ) -> StoreEntry | None:
        """Get an entry.

        Args:
            ns: Namespace.
            key: Key.
            now: Current time.
            timeout: Seconds that the call may take.

        Returns:
            Entry, unless absent or expired.
        """

    def set(
        self,
        ns: str,
        key: str,
        value: str,
        now: datetime,
        expires_at: datetime,
        timeout: float,
        max_size: int | None = None,
    ) -> None:
        """Set a value, replacing an entry with the key (also a counter).

        Args:
            ns: Namespace.
            key: Key.
            value: Value as compact JSON.
            now: Current time.
            expires_at: Time at which the entry expires.
            timeout: Seconds that the call may take.
            max_size: Maximum size of the namespace, in bytes (cf.
                :py:func:`entry_size`); none if `None`.

        Raises:
            StoreFullError: The namespace would exceed `max_size`; writes
                that don't grow it are never refused for size.
        """

    def delete(self, ns: str, key: str, now: datetime, timeout: float) -> bool:
        """Delete an entry.

        Args:
            ns: Namespace.
            key: Key.
            now: Current time.
            timeout: Seconds that the call may take.

        Returns:
            Whether an entry that had not expired was deleted.
        """

    def increment(
        self,
        ns: str,
        key: str,
        by: int,
        limit: int,
        now: datetime,
        expires_at: datetime,
        timeout: float,
        max_size: int | None = None,
    ) -> int | None:
        """Increment a counter atomically, creating it if absent or expired.

        Args:
            ns: Namespace.
            key: Key.
            by: Increment.
            limit: Maximum value of the counter.
            now: Current time.
            expires_at: Expiry of the counter, if it is created.
            timeout: Seconds that the call may take.
            max_size: Maximum size of the namespace, in bytes; none if
                `None`.

        Returns:
            New value; `None` if it would exceed `limit` (the counter is
            left unchanged then).

        Raises:
            StoreError: The key holds a value that is no counter.
            StoreFullError: A new counter would make the namespace exceed
                `max_size`.
        """


def _counter_conflict() -> StoreError:
    """Build the error for increments of values that are no counters.

    The message leaves out the key, which may hold personal data (e.g., a
    user identifier), as errors of plugins are logged.

    Returns:
        Error.
    """
    return StoreError("The key holds a value that is no counter")


class MemoryStoreBackend:
    """Backend that keeps entries in the memory of the process.

    Thread-safe; expired entries are removed when read, from time to time
    when entries are written, and when a namespace is full (at most every
    `_FULL_SWEEP_INTERVAL` seconds per namespace, as that scans all
    entries). The sizes of the namespaces are exact.

    Attributes:
        shared: `False`: each process has its own entries.
    """

    shared = False
    _SWEEP_INTERVAL = 1024
    _FULL_SWEEP_INTERVAL = 10.0

    def __init__(self) -> None:
        """Create an empty backend."""
        self._entries: dict[tuple[str, str], StoreEntry] = {}
        self._sizes: dict[str, int] = {}
        # `time.monotonic()` of the last sweep of a full namespace
        self._full_swept: dict[str, float] = {}
        self._lock = threading.Lock()
        self._writes = 0

    def setup(self) -> None:
        """Do nothing: the backend needs no preparation."""

    def cap_expiry(self, ns: str, expires_at: datetime, timeout: float) -> None:
        """Cut the expiry of a namespace's entries (cf. :py:class:`StoreBackend`).

        Args:
            ns: Namespace.
            expires_at: Latest expiry.
            timeout: Seconds that the call may wait for the lock.
        """
        with self._locked(timeout):
            for ident, entry in self._entries.items():
                if ident[0] == ns and entry.expires_at > expires_at:
                    self._entries[ident] = replace(entry, expires_at=expires_at)

    @contextmanager
    def _locked(self, timeout: float) -> Iterator[None]:
        """Hold the lock, waiting for it at most until the timeout.

        Args:
            timeout: Seconds that the call may take.

        Yields:
            Nothing; the call runs with the lock held.

        Raises:
            StoreError: The lock was not acquired in time.
        """
        if not self._lock.acquire(timeout=max(timeout, 0.0)):
            raise StoreError("Plugin store busy: lock not acquired in time")
        try:
            yield
        finally:
            self._lock.release()

    def _live(self, ns: str, key: str, now: datetime) -> StoreEntry | None:
        """Get an entry that has not expired; call with the lock held.

        Args:
            ns: Namespace.
            key: Key.
            now: Current time.

        Returns:
            Entry, unless absent or expired (expired ones are removed).
        """
        entry = self._entries.get((ns, key))
        if entry is not None and entry.expires_at <= now:
            self._remove((ns, key))
            return None
        return entry

    def _remove(self, ident: tuple[str, str]) -> None:
        """Remove an entry, updating the size of its namespace.

        Call with the lock held.

        Args:
            ident: Namespace and key of an entry that exists.
        """
        entry = self._entries.pop(ident)
        self._sizes[ident[0]] -= entry_size(ident[1], entry.value)

    def _sweep(self, now: datetime, ns: str | None = None) -> None:
        """Remove expired entries; call with the lock held.

        Args:
            now: Current time.
            ns: Namespace whose entries to remove; all if `None`.
        """
        for ident in [
            ident
            for ident, item in self._entries.items()
            if item.expires_at <= now and ns in (None, ident[0])
        ]:
            self._remove(ident)

    def _write(
        self,
        ns: str,
        key: str,
        entry: StoreEntry,
        now: datetime,
        max_size: int | None,
    ) -> None:
        """Write an entry, removing expired ones from time to time.

        Call with the lock held.

        Args:
            ns: Namespace.
            key: Key.
            entry: Entry.
            now: Current time.
            max_size: Maximum size of the namespace, in bytes, if any.

        Raises:
            StoreFullError: The entry would make the namespace exceed
                `max_size`, also without its expired entries.
        """
        size = entry_size(key, entry.value)

        def growth() -> int:
            """Get the bytes by which the write grows the namespace.

            Returns:
                Bytes; negative if it shrinks.
            """
            previous = self._entries.get((ns, key))
            return size - (entry_size(key, previous.value) if previous else 0)

        delta = growth()
        if max_size is not None and delta > 0:
            if self._sizes.get(ns, 0) + delta > max_size:
                swept = self._full_swept.get(ns)
                if (
                    swept is None
                    or time.monotonic() - swept >= self._FULL_SWEEP_INTERVAL
                ):
                    self._full_swept[ns] = time.monotonic()
                    self._sweep(now, ns)
                    # the entry itself may have expired and been removed
                    delta = growth()
            if self._sizes.get(ns, 0) + delta > max_size:
                raise _full(size, max_size)
        self._entries[(ns, key)] = entry
        self._sizes[ns] = self._sizes.get(ns, 0) + delta
        self._writes += 1
        if self._writes % self._SWEEP_INTERVAL == 0:
            self._sweep(now)

    def get(
        self, ns: str, key: str, now: datetime, timeout: float
    ) -> StoreEntry | None:
        """Get an entry (cf. :py:class:`StoreBackend`).

        Args:
            ns: Namespace.
            key: Key.
            now: Current time.
            timeout: Seconds that the call may wait for the lock.

        Returns:
            Entry, unless absent or expired.
        """
        with self._locked(timeout):
            return self._live(ns, key, now)

    def set(
        self,
        ns: str,
        key: str,
        value: str,
        now: datetime,
        expires_at: datetime,
        timeout: float,
        max_size: int | None = None,
    ) -> None:
        """Set a value (cf. :py:class:`StoreBackend`).

        Args:
            ns: Namespace.
            key: Key.
            value: Value as compact JSON.
            now: Current time.
            expires_at: Time at which the entry expires.
            timeout: Seconds that the call may wait for the lock.
            max_size: Maximum size of the namespace, in bytes, if any.

        Raises:
            StoreFullError: The namespace would exceed `max_size`.
        """
        with self._locked(timeout):
            self._write(ns, key, StoreEntry(value, None, expires_at), now, max_size)

    def delete(self, ns: str, key: str, now: datetime, timeout: float) -> bool:
        """Delete an entry (cf. :py:class:`StoreBackend`).

        Args:
            ns: Namespace.
            key: Key.
            now: Current time.
            timeout: Seconds that the call may wait for the lock.

        Returns:
            Whether an entry that had not expired was deleted.
        """
        with self._locked(timeout):
            entry = self._live(ns, key, now)
            if entry is None:
                return False
            self._remove((ns, key))
            return True

    def increment(
        self,
        ns: str,
        key: str,
        by: int,
        limit: int,
        now: datetime,
        expires_at: datetime,
        timeout: float,
        max_size: int | None = None,
    ) -> int | None:
        """Increment a counter (cf. :py:class:`StoreBackend`).

        Args:
            ns: Namespace.
            key: Key.
            by: Increment.
            limit: Maximum value of the counter.
            now: Current time.
            expires_at: Expiry of the counter, if it is created.
            timeout: Seconds that the call may wait for the lock.
            max_size: Maximum size of the namespace, in bytes, if any.

        Returns:
            New value, or `None` if it would exceed `limit`.

        Raises:
            StoreError: The key holds a value that is no counter.
            StoreFullError: A new counter would make the namespace exceed
                `max_size`.
        """
        with self._locked(timeout):
            entry = self._live(ns, key, now)
            if entry is not None and entry.count is None:
                raise _counter_conflict()
            count = (entry.count or 0) if entry is not None else 0
            if not _within(count + by, limit):
                return None
            self._write(
                ns,
                key,
                StoreEntry(
                    None,
                    count + by,
                    entry.expires_at if entry is not None else expires_at,
                ),
                now,
                max_size,
            )
            return count + by


def _aware(value: Any) -> datetime | None:
    """Get a time read from MongoDB as UTC time.

    Args:
        value: Time, naive (UTC) unless the client is time zone aware; or
            anything else for entries without valid expiry.

    Returns:
        Time with time zone UTC, or `None` if the value is no time.
    """
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class MongoStoreBackend:
    """Backend that keeps entries in a MongoDB collection, for all processes.

    Documents: `{_id, ns, key, value | n, expires_at, size}`, with `_id`
    the namespace and key, separated by the control character `U+001F`;
    where `value` is compact JSON, `n` the value of a counter and `size`
    the bytes that the entry counts (cf. :py:func:`entry_size`).

    The size of each namespace is kept in the collection
    :py:data:`STORE_SIZES_COLLECTION`, approximately: writes add and
    deletions subtract the sizes of their entries, but entries removed by
    the TTL index still count until the namespace is counted again. Each
    process counts a namespace again (summing `size` over its entries,
    expired ones included until removed; entries of FOCA versions before
    0.23.0, without `size`, count :py:data:`STORE_ENTRY_OVERHEAD`) before
    its first write to it, every `_RECOUNT_INTERVAL` seconds, and when a
    write does not fit, at most every `_RECOUNT_RETRY_INTERVAL` seconds,
    retrying the write then. Counts take at most half the time left for
    the write; if one fails (e.g., a large namespace and a short step
    timeout), the write goes on with the size kept so far (the failure is
    logged), and a thread of the process counts the namespace again in the
    background, within `_BACKGROUND_TIMEOUT` seconds, so that the size
    doesn't drift. Writes and deletions while a count runs (cf.
    :py:meth:`recount`), and writes that failed after their size was
    added (they may have been applied all the same), may be miscounted
    until the next count.

    Attributes:
        shared: `True`: all processes share the entries.
        collection: Collection of the entries.
        sizes: Collection of the sizes of the namespaces.
    """

    shared = True
    _ATTEMPTS = 5
    _SETUP_TIMEOUT = 10.0
    _RECOUNT_INTERVAL = 300.0
    _RECOUNT_RETRY_INTERVAL = 10.0
    _BACKGROUND_TIMEOUT = 60.0

    def __init__(self, collection: Collection[dict[str, Any]]) -> None:
        """Create the backend.

        Args:
            collection: Collection of the entries (cf. `STORE_COLLECTION`).
        """
        # time zone aware UTC times whatever the client's options (e.g.,
        # `datetime_conversion` in `MONGO_URI`), as results depend on them
        # and reads from the primary, as increments read what they wrote
        self.collection: Collection[dict[str, Any]] = collection.with_options(
            codec_options=CodecOptions(
                tz_aware=True,
                datetime_conversion=DatetimeConversion.DATETIME,
            ),
            read_preference=ReadPreference.PRIMARY,
        )
        self.sizes: Collection[dict[str, Any]] = self.collection.database[
            STORE_SIZES_COLLECTION
        ].with_options(
            codec_options=self.collection.codec_options,
            read_preference=ReadPreference.PRIMARY,
        )
        self._indexed = False
        # `time.monotonic()` of the last count, by namespace
        self._counted: dict[str, float] = {}
        # threads that count namespaces in the background, by namespace
        self._background: dict[str, threading.Thread] = {}
        self._counted_lock = threading.Lock()

    def setup(self) -> None:
        """Create the indexes of the collection, if they don't exist yet.

        Writes call it too, until it succeeded once in the process, so that
        entries are never written without the TTL index. It takes at most
        `_SETUP_TIMEOUT` seconds (less within a shorter timeout), so that an
        unreachable database does not stall the startup.

        Raises:
            pymongo.errors.PyMongoError: The database cannot be reached in
                time, or an index conflicts with an existing one.
        """
        with pymongo.timeout(self._SETUP_TIMEOUT):
            for index in STORE_INDEXES:
                self.collection.create_index(index["keys"], **index["options"])
        self._indexed = True

    def cap_expiry(self, ns: str, expires_at: datetime, timeout: float) -> None:
        """Cut the expiry of a namespace's entries (cf. :py:class:`StoreBackend`).

        The TTL index then removes them at the new expiry.

        Args:
            ns: Namespace.
            expires_at: Latest expiry.
            timeout: Seconds that the call may take.

        Raises:
            pymongo.errors.PyMongoError: The database failed or was too
                slow.
        """
        with pymongo.timeout(timeout):
            self.collection.update_many(
                {"ns": ns, "expires_at": {"$gt": expires_at}},
                {"$set": {"expires_at": expires_at}},
            )

    def _ensure_indexes(self) -> None:
        """Create the indexes unless done in this process already.

        Raises:
            pymongo.errors.PyMongoError: The indexes cannot be created.
        """
        if not self._indexed:
            self.setup()

    def recount(self, ns: str) -> int:
        """Count the size of a namespace again, from its entries.

        One aggregation over the entries of the namespace (index `ns`);
        entries without `size` (of FOCA versions before 0.23.0) count
        :py:data:`STORE_ENTRY_OVERHEAD`. The stored size is corrected by
        the difference between the count and the size read before it
        (`$inc`, not `$set`), so that sizes that writes and deletions add
        while the count runs are kept: those of entries that the count
        sees too then count twice, deletions that it misses not at all,
        until the next count. Each correction bumps the field `gen` of the
        size, and applies only if it is still the one read, so that
        concurrent counts (of this or other processes) correct it once.

        Args:
            ns: Namespace.

        Returns:
            Size of the namespace, in bytes.

        Raises:
            pymongo.errors.PyMongoError: The database failed or was too
                slow.
        """
        kept = self.sizes.find_one({"_id": ns})
        before = int(kept.get("size", 0)) if kept is not None else 0
        # `None` matches sizes without `gen` (inserted by writes) too
        gen = kept.get("gen") if kept is not None else None
        size = {"$ifNull": ["$size", STORE_ENTRY_OVERHEAD]}
        counted = list(
            self.collection.aggregate(
                [
                    {"$match": {"ns": ns}},
                    {"$group": {"_id": None, "size": {"$sum": size}}},
                ]
            )
        )
        total = int(counted[0]["size"]) if counted else 0
        # a duplicate: corrected by a concurrent count in the meantime
        with suppress(DuplicateKeyError):
            self.sizes.update_one(
                {"_id": ns, "gen": gen},
                {"$inc": {"size": total - before}, "$set": {"gen": (gen or 0) + 1}},
                upsert=True,
            )
        return total

    def _recount_if_due(self, ns: str, interval: float, timeout: float) -> bool:
        """Count a namespace again unless this process did so recently.

        Only one thread of the process counts a namespace at a time; a
        failed count is logged, not raised, and tried again in the
        background (:py:meth:`_recount_in_background`).

        Args:
            ns: Namespace.
            interval: Seconds after which a count is due.
            timeout: Seconds left for the write; the count takes at most
                half of them.

        Returns:
            Whether the namespace was counted.
        """
        now = time.monotonic()
        with self._counted_lock:
            counted = self._counted.get(ns)
            if counted is not None and now - counted < interval:
                return False
            self._counted[ns] = now
        try:
            with pymongo.timeout(timeout / 2):
                self.recount(ns)
        except PyMongoError as exc:
            logger.warning(
                "Size of the plugin store namespace %s not counted again (%s):"
                " writes go on with the size kept so far; counting it in the"
                " background",
                ns,
                type(exc).__name__,
            )
            self._recount_in_background(ns)
            return False
        return True

    def _recount_in_background(self, ns: str) -> None:
        """Count a namespace again in a thread, unless one already does.

        The count takes at most `_BACKGROUND_TIMEOUT` seconds, outside the
        deadline of the plugin call; a failure is logged.

        Args:
            ns: Namespace.
        """

        def run() -> None:
            """Count the namespace, logging a failure."""
            try:
                with pymongo.timeout(self._BACKGROUND_TIMEOUT):
                    self.recount(ns)
            except PyMongoError as exc:
                logger.warning(
                    "Size of the plugin store namespace %s not counted again"
                    " in the background (%s)",
                    ns,
                    type(exc).__name__,
                )

        with self._counted_lock:
            running = self._background.get(ns)
            if running is not None and running.is_alive():
                return
            # in a context of its own, so that the deadline of the call
            # doesn't apply (threads may inherit contexts, e.g., free-
            # threaded Python 3.14)
            thread = threading.Thread(
                target=contextvars.Context().run,
                args=(run,),
                name=f"plugin-store-recount-{ns}",
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError as exc:  # e.g., no more threads
                logger.warning(
                    "Size of the plugin store namespace %s not counted in the"
                    " background (%s)",
                    ns,
                    exc,
                )
                return
            self._background[ns] = thread

    def _adjust(self, ns: str, delta: int) -> None:
        """Add to the size of a namespace, if it has been counted.

        Args:
            ns: Namespace.
            delta: Bytes to add; negative to subtract.

        Raises:
            pymongo.errors.PyMongoError: The database failed or was too
                slow.
        """
        if delta:
            self.sizes.update_one({"_id": ns}, {"$inc": {"size": delta}})

    def _reserve(
        self,
        ns: str,
        size: int,
        delta: int,
        max_size: int | None,
        timeout: float,
    ) -> int:
        """Add the growth of a namespace by a write, if it fits.

        Writes that don't grow the namespace, or without maximum, add
        nothing: they adjust the size after they succeeded.

        Args:
            ns: Namespace.
            size: Bytes that the written entry counts.
            delta: Bytes by which the write grows the namespace.
            max_size: Maximum size of the namespace, in bytes, if any.
            timeout: Seconds left for the write.

        Returns:
            Bytes added to the size: `delta` or 0.

        Raises:
            StoreFullError: The namespace would exceed `max_size`, also
                after counting it again (unless counted in the last
                `_RECOUNT_RETRY_INTERVAL` seconds).
            pymongo.errors.PyMongoError: The database failed or was too
                slow.
        """
        self._recount_if_due(ns, self._RECOUNT_INTERVAL, timeout)
        if max_size is None or delta <= 0:
            return 0
        if delta <= max_size:
            if self._grow(ns, delta, max_size):
                return delta
            # entries removed by the TTL index may still count
            if self._recount_if_due(
                ns, self._RECOUNT_RETRY_INTERVAL, timeout
            ) and self._grow(ns, delta, max_size):
                return delta
        raise _full(size, max_size)

    def _grow(self, ns: str, delta: int, max_size: int) -> bool:
        """Add to the size of a namespace if it stays within the maximum.

        Args:
            ns: Namespace.
            delta: Bytes to add, at most `max_size`.
            max_size: Maximum size of the namespace, in bytes.

        Returns:
            Whether the size was added.

        Raises:
            pymongo.errors.PyMongoError: The database failed or was too
                slow.
        """
        query = {"_id": ns, "size": {"$lte": max_size - delta}}
        update = {"$inc": {"size": delta}}
        try:
            # inserts the size if absent; fails if present but too large
            result = self.sizes.update_one(query, update, upsert=True)
        except DuplicateKeyError:
            # too large, or inserted concurrently
            return bool(self.sizes.update_one(query, update).matched_count)
        if result.upserted_id is not None:
            # the size was absent (e.g., deleted by an operator): count the
            # namespace again at the next write
            with self._counted_lock:
                self._counted.pop(ns, None)
        return True

    def get(
        self, ns: str, key: str, now: datetime, timeout: float
    ) -> StoreEntry | None:
        """Get an entry (cf. :py:class:`StoreBackend`).

        Args:
            ns: Namespace.
            key: Key.
            now: Current time.
            timeout: Seconds that the call may take.

        Returns:
            Entry, unless absent or expired.

        Raises:
            StoreError: The database failed or was too slow.
        """
        with _guard(timeout):
            document = self.collection.find_one(
                {"_id": _ident(ns, key), "expires_at": {"$gt": now}}
            )
        if document is None:
            return None
        return StoreEntry(
            document.get("value"),
            document.get("n"),
            _aware(document.get("expires_at")) or now,
        )

    def set(
        self,
        ns: str,
        key: str,
        value: str,
        now: datetime,
        expires_at: datetime,
        timeout: float,
        max_size: int | None = None,
    ) -> None:
        """Set a value (cf. :py:class:`StoreBackend`).

        Args:
            ns: Namespace.
            key: Key.
            value: Value as compact JSON.
            now: Current time.
            expires_at: Time at which the entry expires.
            timeout: Seconds that the call may take.
            max_size: Maximum size of the namespace, in bytes, if any.

        Raises:
            StoreError: The database failed or was too slow.
            StoreFullError: The namespace would exceed `max_size`.
        """
        ident = _ident(ns, key)
        size = entry_size(key, value)
        document = {
            "ns": ns,
            "key": key,
            "value": value,
            "expires_at": expires_at,
            "size": size,
        }
        with _guard(timeout):
            self._ensure_indexes()
            known = self.collection.find_one({"_id": ident}, projection=_SIZE_FIELDS)
            reserved = self._reserve(
                ns, size, size - _document_size(known), max_size, timeout
            )
            previous = _retry_upsert(
                lambda: self.collection.find_one_and_replace(
                    {"_id": ident},
                    document,
                    projection=_SIZE_FIELDS,
                    upsert=True,
                )
            )
            # what wasn't reserved (e.g., shrinking), and if the entry changed
            # since it was read
            self._adjust(ns, size - _document_size(previous) - reserved)

    def delete(self, ns: str, key: str, now: datetime, timeout: float) -> bool:
        """Delete an entry (cf. :py:class:`StoreBackend`).

        Args:
            ns: Namespace.
            key: Key.
            now: Current time.
            timeout: Seconds that the call may take.

        Returns:
            Whether an entry that had not expired was deleted.

        Raises:
            StoreError: The database failed or was too slow.
        """
        with _guard(timeout):
            document = self.collection.find_one_and_delete(
                {"_id": _ident(ns, key)},
                projection={**_SIZE_FIELDS, "expires_at": True},
            )
            if document is None:
                return False
            self._adjust(ns, -_document_size(document))
        expires_at = _aware(document.get("expires_at"))
        return expires_at is not None and expires_at > now

    def increment(
        self,
        ns: str,
        key: str,
        by: int,
        limit: int,
        now: datetime,
        expires_at: datetime,
        timeout: float,
        max_size: int | None = None,
    ) -> int | None:
        """Increment a counter atomically (cf. :py:class:`StoreBackend`).

        The server decides with conditional updates of one document, which
        are atomic: first, a live counter is incremented if the new value
        stays within the bounds; otherwise, an absent or expired entry is
        replaced by a new counter. If a concurrent call creates the entry
        in between, the steps are tried again.

        Args:
            ns: Namespace.
            key: Key.
            by: Increment.
            limit: Maximum value of the counter.
            now: Current time.
            expires_at: Expiry of the counter, if it is created.
            timeout: Seconds that the call may take.
            max_size: Maximum size of the namespace, in bytes, if any.

        Returns:
            New value, or `None` if it would exceed `limit` or go below
            -(2^53 - 1).

        Raises:
            StoreError: The key holds a value that is no counter, the
                database failed or was too slow, or concurrent calls kept
                the increment from applying.
            StoreFullError: A new counter would make the namespace exceed
                `max_size`.
        """
        ident = _ident(ns, key)
        live = {"$gt": now}
        with _guard(timeout):
            self._ensure_indexes()
            for _ in range(self._ATTEMPTS):
                counter = self.collection.find_one_and_update(
                    {
                        "_id": ident,
                        "expires_at": live,
                        "value": {"$exists": False},
                        # the bounds of `_within()`, for the new value
                        "n": {"$lte": limit - by, "$gte": -MAX_SAFE_INTEGER - by},
                    },
                    {"$inc": {"n": by}},
                    return_document=pymongo.ReturnDocument.AFTER,
                )
                if counter is not None:
                    return int(counter["n"])
                current = self.collection.find_one({"_id": ident, "expires_at": live})
                if current is not None:
                    if "value" in current:
                        raise _counter_conflict()
                    if not _within(current.get("n", 0) + by, limit):
                        return None
                    continue  # changed in between
                if not _within(by, limit):
                    return None
                size = entry_size(key, None)
                counter = {
                    "ns": ns,
                    "key": key,
                    "n": by,
                    "expires_at": expires_at,
                    "size": size,
                }
                # an expired entry counts until it is replaced or removed
                expired = self.collection.find_one(
                    {"_id": ident}, projection=_SIZE_FIELDS
                )
                reserved = self._reserve(
                    ns, size, size - _document_size(expired), max_size, timeout
                )
                previous: Mapping[str, Any] | None = None
                try:
                    self.collection.insert_one({"_id": ident, **counter})
                except DuplicateKeyError:
                    # an expired entry (also without valid expiry), or one
                    # created in between
                    previous = self.collection.find_one_and_replace(
                        {"_id": ident, "expires_at": {"$not": live}},
                        counter,
                        projection=_SIZE_FIELDS,
                    )
                    if previous is None:
                        # a live entry was created in between: nothing was
                        # written, so take the reserved growth back and try
                        # again
                        self._adjust(ns, -reserved)
                        continue
                self._adjust(ns, size - _document_size(previous) - reserved)
                return by
        raise StoreError("Plugin store busy: increment not applied")


def _document_size(document: Mapping[str, Any] | None) -> int:
    """Get the bytes that a document of the store counts.

    Args:
        document: Document, with the fields of `_SIZE_FIELDS`, if any.

    Returns:
        Its `size`; :py:data:`STORE_ENTRY_OVERHEAD` for documents without
        valid size (of FOCA versions before 0.23.0), as in
        :py:meth:`MongoStoreBackend.recount`; 0 without document.
    """
    if document is None:
        return 0
    size = document.get("size")
    if isinstance(size, int) and not isinstance(size, bool):
        return size
    return STORE_ENTRY_OVERHEAD


def _ident(ns: str, key: str) -> str:
    """Get the identifier of an entry.

    Args:
        ns: Namespace.
        key: Key.

    Returns:
        Identifier.
    """
    return f"{ns}{_SEPARATOR}{key}"


def _retry_upsert(operation: Callable[[], Any]) -> Any:
    """Run an upsert, once more if a concurrent upsert inserted the key.

    Args:
        operation: Upsert.

    Returns:
        Result of the operation.
    """
    try:
        return operation()
    except DuplicateKeyError:
        return operation()


@contextmanager
def _guard(timeout: float) -> Iterator[None]:
    """Bound database calls by a timeout and turn their errors into `StoreError`.

    Args:
        timeout: Seconds that the calls may take together.

    Yields:
        Nothing; the calls run in the context.

    Raises:
        StoreError: The calls raised a PyMongo error (e.g., a timeout); its
            message is left out, as it may name hosts.
    """
    with pymongo.timeout(timeout):
        try:
            yield
        except PyMongoError as exc:
            raise StoreError(f"Plugin store failed: {type(exc).__name__}") from exc


@dataclass(frozen=True)
class StoreSettings:
    """Settings of the store of one plugin.

    Attributes:
        namespace: Namespace of the plugin's entries.
        mode: Where the entries are kept.
        backend: Backend that keeps the entries.
        max_entry_size: Maximum size of values, in bytes, as compact JSON.
        max_ttl: Maximum time to live of entries, in seconds.
        key_secret: Secret for :py:meth:`Store.key_for`, if configured
            (`plugins.defaults.store.key_secret`); not shown in `repr()`.
        max_namespace_size: Maximum size of the namespace, in bytes (cf.
            :py:func:`entry_size`), as checked by the plugin's writes; none
            if `None`.
    """

    namespace: str
    mode: StoreMode
    backend: StoreBackend
    max_entry_size: int
    max_ttl: float
    key_secret: bytes | None = field(default=None, repr=False)
    max_namespace_size: int | None = None


def _utc_now() -> datetime:
    """Get the current time, as stored: UTC, with millisecond precision.

    Returns:
        Time.
    """
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


class Store:
    """Key-value store of a plugin call (`ctx.store`).

    Keys are strings of 1 to 256 characters, values JSON data (I-JSON, at
    most `max_entry_size` bytes as compact JSON). Every entry expires: after
    its `ttl`, capped at the operator's `max_ttl`, which entries without a
    `ttl` get. The entries of a namespace take at most the operator's
    `max_namespace_size` bytes (cf. :py:func:`entry_size`). Calls are
    bounded by the deadline of the hook call.

    Keys are data too: don't use personal data such as user identifiers as
    keys as they are, but :py:meth:`key_for` them.

    Attributes:
        namespace: Namespace of the plugin's entries (its distribution).
        mode: Where the entries are kept: `shared` (database, for all
            processes), `local` (memory of the process, as no database is
            configured) or `off` (memory of the process, as the operator
            turned the store off for the plugin).
    """

    def __init__(
        self,
        settings: StoreSettings,
        deadline: float | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create the store of a call.

        Args:
            settings: Settings of the plugin's store.
            deadline: `time.monotonic()` value by which calls must end;
                none if `None` (e.g., in tests).
            clock: Function returning the current time (UTC), e.g., for
                tests.
        """
        self._settings = settings
        self._deadline = deadline
        self._clock = clock
        self.namespace = settings.namespace
        self.mode: StoreMode = settings.mode

    def _timeout(self) -> float:
        """Get the seconds left for a call.

        Returns:
            Seconds; a day without deadline.

        Raises:
            StoreError: The deadline passed.
        """
        if self._deadline is None:
            return 86400.0
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise StoreError("Plugin store not called: deadline passed")
        return remaining

    def _expiry(self, ttl: float | None, now: datetime) -> datetime:
        """Get the expiry of an entry.

        Args:
            ttl: Time to live, in seconds, if any.
            now: Current time.

        Returns:
            Time at which the entry expires.

        Raises:
            ValueError: The time to live is no finite number of at least
                `MIN_TTL` seconds.
        """
        max_ttl = self._settings.max_ttl
        if ttl is not None:
            if (
                isinstance(ttl, bool)
                or not isinstance(ttl, int | float)
                or not MIN_TTL <= ttl < float("inf")
            ):
                raise ValueError(f"ttl must be a number of at least {MIN_TTL} seconds")
            max_ttl = min(ttl, max_ttl)
        expires_at = now + timedelta(seconds=max_ttl)
        # as stored: millisecond precision
        return expires_at.replace(microsecond=expires_at.microsecond // 1000 * 1000)

    def get(self, key: str, default: Any = None) -> Any:
        """Get the value of a key.

        Args:
            key: Key.
            default: Value to return if the key is absent or expired (not
                for stored `null`, which gives `None`).

        Returns:
            Value (a copy), the value of a counter, or `default`.

        Raises:
            ValueError: The key is invalid.
            StoreError: The store failed, or the deadline passed.
        """
        _check_key(key)
        timeout = self._timeout()
        entry = self._settings.backend.get(self.namespace, key, self._clock(), timeout)
        if entry is None:
            return default
        if entry.value is None:
            return entry.count
        return json.loads(entry.value)

    def set(self, key: str, value: Any, *, ttl: float | None = None) -> None:
        """Set the value of a key, replacing an entry with the key.

        Args:
            key: Key.
            value: JSON data (I-JSON: e.g., integers up to 2^53 - 1); at
                most `max_entry_size` bytes as compact JSON. Mappings need
                string keys; tuples are stored as lists.
            ttl: Time to live, in seconds; at most the operator's
                `max_ttl`, which is also the default.

        Raises:
            ValueError: The key, value or time to live is invalid, or the
                value is too large.
            StoreFullError: The namespace has no room for the entry
                (`max_namespace_size`); replacing an entry by one that is
                not larger is never refused.
            StoreError: The store failed, or the deadline passed.
        """
        _check_key(key)
        invalid = "Values must be JSON data: {}"
        try:
            dumped = json.dumps(
                value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            size = len(dumped.encode())
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError(invalid.format(exc)) from None
        # checked first, as the other checks take longer with the size
        if size > self._settings.max_entry_size:
            raise ValueError(
                f"Value of {size} bytes exceeds {self._settings.max_entry_size} bytes"
            )
        try:
            check_i_json(json.loads(dumped))
            if any(not isinstance(item, str) for item in _mapping_keys(value)):
                raise ValueError("Mappings need string keys")
        except (ValueError, RecursionError) as exc:
            raise ValueError(invalid.format(exc)) from None
        now = self._clock()
        expires_at = self._expiry(ttl, now)
        self._settings.backend.set(
            self.namespace,
            key,
            dumped,
            now,
            expires_at,
            self._timeout(),
            max_size=self._settings.max_namespace_size,
        )

    def delete(self, key: str) -> bool:
        """Delete a key.

        Args:
            key: Key.

        Returns:
            Whether the key had a value that had not expired.

        Raises:
            ValueError: The key is invalid.
            StoreError: The store failed, or the deadline passed.
        """
        _check_key(key)
        timeout = self._timeout()
        return self._settings.backend.delete(
            self.namespace, key, self._clock(), timeout
        )

    def increment(
        self,
        key: str,
        by: int = 1,
        *,
        ttl: float | None = None,
        limit: int | None = None,
    ) -> int | None:
        """Increment a counter atomically, e.g., for budgets.

        A counter that is absent or expired starts at 0 and gets the time to
        live; later increments keep its expiry (a fixed window, e.g., "per
        day" with `ttl=86400`).

        Args:
            key: Key.
            by: Increment; negative values decrement.
            ttl: Time to live of a new counter, in seconds; at most the
                operator's `max_ttl`, which is also the default.
            limit: Maximum value of the counter; at most 2^53 - 1, which is
                also the default; counters never go below -(2^53 - 1).

        Returns:
            New value; `None` if it would exceed `limit` or go below
            -(2^53 - 1), in which case the counter is left unchanged.

        Raises:
            ValueError: The key, increment, limit or time to live is
                invalid.
            StoreFullError: The namespace has no room for a new counter
                (`max_namespace_size`).
            StoreError: The key holds a value that is no counter, the store
                failed, or the deadline passed.
        """
        _check_key(key)
        _check_int("by", by)
        if limit is not None:
            _check_int("limit", limit)
        now = self._clock()
        expires_at = self._expiry(ttl, now)
        return self._settings.backend.increment(
            self.namespace,
            key,
            by,
            MAX_SAFE_INTEGER if limit is None else limit,
            now,
            expires_at,
            self._timeout(),
            max_size=self._settings.max_namespace_size,
        )

    def key_for(self, *parts: str) -> str:
        """Derive a key from parts that may be personal data.

        E.g., `ctx.store.key_for("budget", ctx.principal.subject_type,
        ctx.principal.subject)` for per-user counters. The key is a hash of
        the namespace and the parts. With the operator's secret
        (`plugins.defaults.store.key_secret`), it is an HMAC-SHA256, so that
        nobody without the secret (e.g., with access to the database or a
        backup) can recompute it from a guessed identifier; without, it is
        a plain SHA-256, which only pseudonymizes the parts (identifiers
        can be guessed and hashed again). Either way, the entries remain
        personal data. Changing, adding or removing the secret changes all
        keys: earlier entries are no longer found, and expire (after at
        most `max_ttl`).

        Args:
            parts: Parts, e.g., a purpose and an identifier.

        Returns:
            Key: `hk:` and the hexadecimal HMAC with the operator's
            secret, otherwise `h:` and the hexadecimal hash.

        Raises:
            ValueError: A part is no string, or no part is given.
        """
        if not parts or not all(isinstance(part, str) for part in parts):
            raise ValueError("key_for() needs one or more strings")
        message = json.dumps([self.namespace, *parts]).encode()
        secret = self._settings.key_secret
        if secret:
            return f"hk:{hmac.new(secret, message, hashlib.sha256).hexdigest()}"
        return f"h:{hashlib.sha256(message).hexdigest()}"


def _mapping_keys(value: Any) -> list[Any]:
    """Get the keys of all mappings in JSON-like data.

    Args:
        value: Data.

    Returns:
        Keys of all mappings, at any depth.
    """
    keys: list[Any] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            keys.extend(item)
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
    return keys


def _within(value: int, limit: int) -> bool:
    """Check that a counter value is within its bounds.

    Args:
        value: Value.
        limit: Upper bound.

    Returns:
        Whether the value is at most `limit` and at least -(2^53 - 1).
    """
    return -MAX_SAFE_INTEGER <= value <= limit


def _check_int(name: str, value: Any) -> None:
    """Check an integer argument.

    Args:
        name: Name of the argument, for messages.
        value: Value.

    Raises:
        ValueError: The value is no integer (booleans aren't either), or
            beyond 2^53 - 1.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or abs(value) > MAX_SAFE_INTEGER
    ):
        raise ValueError(f"{name} must be an integer up to 2^53 - 1")


def _check_key(key: Any) -> None:
    """Check a key.

    Args:
        key: Key.

    Raises:
        ValueError: The key is no string of 1 to 256 characters, or no
            valid Unicode.
    """
    if not isinstance(key, str) or not 0 < len(key) <= MAX_KEY_LENGTH:
        raise ValueError(f"Keys must be strings of 1 to {MAX_KEY_LENGTH} characters")
    try:
        key.encode()
    except UnicodeEncodeError:
        raise ValueError("Keys must be valid Unicode") from None
