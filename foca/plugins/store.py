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
latest.

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
    ctx.store.set(f"geo:{host}", location, ttl=3600)
```
"""

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import pymongo
from bson.codec_options import CodecOptions, DatetimeConversion
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_preferences import ReadPreference

from foca.plugins.base import PluginError
from foca.plugins.selection import MAX_SAFE_INTEGER, check_i_json

STORE_COLLECTION = "plugin_store"
"""Name of the collection of the store, in the app's first database."""

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
    ) -> None:
        """Set a value, replacing an entry with the key (also a counter).

        Args:
            ns: Namespace.
            key: Key.
            value: Value as compact JSON.
            now: Current time.
            expires_at: Time at which the entry expires.
            timeout: Seconds that the call may take.
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

        Returns:
            New value; `None` if it would exceed `limit` (the counter is
            left unchanged then).

        Raises:
            StoreError: The key holds a value that is no counter.
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

    Thread-safe; expired entries are removed when read and, from time to
    time, when entries are written.

    Attributes:
        shared: `False`: each process has its own entries.
    """

    shared = False
    _SWEEP_INTERVAL = 1024

    def __init__(self) -> None:
        """Create an empty backend."""
        self._entries: dict[tuple[str, str], StoreEntry] = {}
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
            del self._entries[(ns, key)]
            return None
        return entry

    def _write(self, ns: str, key: str, entry: StoreEntry, now: datetime) -> None:
        """Write an entry, removing expired ones from time to time.

        Call with the lock held.

        Args:
            ns: Namespace.
            key: Key.
            entry: Entry.
            now: Current time.
        """
        self._entries[(ns, key)] = entry
        self._writes += 1
        if self._writes % self._SWEEP_INTERVAL == 0:
            for ident in [
                ident for ident, item in self._entries.items() if item.expires_at <= now
            ]:
                del self._entries[ident]

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
    ) -> None:
        """Set a value (cf. :py:class:`StoreBackend`).

        Args:
            ns: Namespace.
            key: Key.
            value: Value as compact JSON.
            now: Current time.
            expires_at: Time at which the entry expires.
            timeout: Seconds that the call may wait for the lock.
        """
        with self._locked(timeout):
            self._write(ns, key, StoreEntry(value, None, expires_at), now)

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
            del self._entries[(ns, key)]
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

        Returns:
            New value, or `None` if it would exceed `limit`.

        Raises:
            StoreError: The key holds a value that is no counter.
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

    Documents: `{_id, ns, key, value | n, expires_at}`, with `_id` the
    namespace and key, separated by the control character `U+001F`;
    where `value` is compact JSON and `n` the value of a counter.

    Attributes:
        shared: `True`: all processes share the entries.
        collection: Collection of the entries.
    """

    shared = True
    _ATTEMPTS = 5
    _SETUP_TIMEOUT = 10.0

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
        self._indexed = False

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
    ) -> None:
        """Set a value (cf. :py:class:`StoreBackend`).

        Args:
            ns: Namespace.
            key: Key.
            value: Value as compact JSON.
            now: Current time.
            expires_at: Time at which the entry expires.
            timeout: Seconds that the call may take.

        Raises:
            StoreError: The database failed or was too slow.
        """
        ident = _ident(ns, key)
        document = {"ns": ns, "key": key, "value": value, "expires_at": expires_at}
        with _guard(timeout):
            self._ensure_indexes()
            _retry_upsert(
                lambda: self.collection.replace_one(
                    {"_id": ident}, document, upsert=True
                )
            )

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
                {"_id": _ident(ns, key)}, projection={"expires_at": True}
            )
        if document is None:
            return False
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

        Returns:
            New value, or `None` if it would exceed `limit` or go below
            -(2^53 - 1).

        Raises:
            StoreError: The key holds a value that is no counter, the
                database failed or was too slow, or concurrent calls kept
                the increment from applying.
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
                counter = {"ns": ns, "key": key, "n": by, "expires_at": expires_at}
                try:
                    self.collection.insert_one({"_id": ident, **counter})
                except DuplicateKeyError:
                    # an expired entry (also without valid expiry), or one
                    # created in between
                    replaced = self.collection.replace_one(
                        {"_id": ident, "expires_at": {"$not": live}}, counter
                    )
                    if not replaced.matched_count:
                        continue
                return by
        raise StoreError("Plugin store busy: increment not applied")


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
    """

    namespace: str
    mode: StoreMode
    backend: StoreBackend
    max_entry_size: int
    max_ttl: float


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
    `ttl` get. Calls are bounded by the deadline of the hook call.

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
            self.namespace, key, dumped, now, expires_at, self._timeout()
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
        )

    def key_for(self, *parts: str) -> str:
        """Derive a key from parts that may be personal data.

        E.g., `ctx.store.key_for("budget", ctx.principal.subject_type,
        ctx.principal.subject)` for per-user counters. The key is a SHA-256
        hash of the namespace and the parts: it pseudonymizes the parts in
        the store, but doesn't anonymize them (identifiers can be guessed
        and hashed again), so the entries remain personal data.

        Args:
            parts: Parts, e.g., a purpose and an identifier.

        Returns:
            Key: `h:` and the hexadecimal hash.

        Raises:
            ValueError: A part is no string, or no part is given.
        """
        if not parts or not all(isinstance(part, str) for part in parts):
            raise ValueError("key_for() needs one or more strings")
        digest = hashlib.sha256(
            json.dumps([self.namespace, *parts]).encode()
        ).hexdigest()
        return f"h:{digest}"


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
