"""Provenance of gateway requests: canonical hashes, changes and storage.

The engine records each pipeline run (cf.
:py:meth:`foca.plugins.PipelineRun.provenance`); this module provides the
pieces of those records and the helpers that store them in collections of
the app:

- :py:func:`canonical_json` and :py:func:`content_hash`: RFC 8785 (JSON
  Canonicalization Scheme) serialization and its SHA-256 digest, which
  identify pipelines and plugin builds independently of key order and
  number formatting;
- :py:func:`changed_paths`: the JSON Pointers (RFC 6901) that a plugin
  changed, without values;
- :py:class:`ProvenanceStore`: stores plugin builds and admin-defined
  pipelines once, by hash, and failed-pipeline records if configured.

Records hold no IP addresses, tokens, header values or claims: changes are
recorded as paths and hashes only.

Plain data and pymongo collections; no web framework.
"""

import hashlib
import json
import logging
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from foca.plugins.loader import LoadedPlugin
from foca.plugins.model import CREDENTIAL_HEADERS

if TYPE_CHECKING:  # pragma: no cover (types only; the engine imports this module)
    from foca.plugins.engine import PipelineRun

logger = logging.getLogger(__name__)

MAX_SAFE_INTEGER = 2**53 - 1
"""Largest integer that hashes represent exactly (I-JSON, RFC 7493)."""

MAX_CHANGED_PATHS = 256
"""Maximum number of changed paths recorded per call."""

IMAGE_DIGEST_ENV = "FOCA_IMAGE_DIGEST"
"""Environment variable with the digest of the app's container image, if set
(e.g., by a Helm chart); stored with the plugin builds."""

FAILURE_INDEXES: list[dict[str, Any]] = [
    {"keys": {"failed_at": 1}, "options": {"expireAfterSeconds": 30 * 24 * 3600}},
]
"""Index specs for the collection of failed-pipeline records, in the format
of FOCA's `db` config: a TTL index on `failed_at` (30 days; set the
retention you need). The other collections are keyed by `_id` and need no
index."""


def _number(value: float) -> str:
    """Serialize a number as ECMAScript does (RFC 8785, section 3.2.2.3).

    Args:
        value: Number.

    Returns:
        Shortest representation that round-trips, in ECMAScript notation.

    Raises:
        ValueError: The number is not finite.
    """
    if not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits_tuple = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(str(digit) for digit in digits_tuple.digits)
    exponent = int(digits_tuple.exponent)
    count = len(digits)
    point = count + exponent  # position of the decimal point
    if count <= point <= 21:
        text = digits + "0" * (point - count)
    elif 0 < point <= 21:
        text = f"{digits[:point]}.{digits[point:]}"
    elif -6 < point <= 0:
        text = f"0.{'0' * -point}{digits}"
    else:
        mantissa = digits if count == 1 else f"{digits[0]}.{digits[1:]}"
        text = f"{mantissa}e{'+' if point > 0 else '-'}{abs(point - 1)}"
    return sign + text


def _serialize(value: Any, parts: list[str]) -> None:
    """Append the canonical JSON of a value.

    Args:
        value: JSON data.
        parts: Output, in pieces.

    Raises:
        TypeError: The value is not JSON data.
        ValueError: A number is not finite.
    """
    if value is None or isinstance(value, bool):
        parts.append({None: "null", True: "true", False: "false"}[value])
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("Integers beyond 2^53 - 1 are not I-JSON")
        parts.append(_number(float(value)))
    elif isinstance(value, float):
        parts.append(_number(value))
    elif isinstance(value, str):
        parts.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        parts.append("{")
        for index, key in enumerate(
            sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        ):
            if index:
                parts.append(",")
            parts.append(json.dumps(key, ensure_ascii=False))
            parts.append(":")
            _serialize(value[key], parts)
        parts.append("}")
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        parts.append("[")
        for index, item in enumerate(value):
            if index:
                parts.append(",")
            _serialize(item, parts)
        parts.append("]")
    else:
        raise TypeError(f"Not JSON data: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize JSON data canonically (RFC 8785).

    Object keys are sorted by their UTF-16 code units, numbers are written
    as in ECMAScript, and there is no whitespace. Integers are treated as
    IEEE 754 doubles, as RFC 8785 requires, so integers beyond 2^53 - 1,
    which doubles do not represent exactly, are refused (I-JSON).

    Args:
        value: JSON data (`dict`, `list`, `tuple`, `str`, `int`, `float`,
            `bool`, `None`).

    Returns:
        Canonical JSON.

    Raises:
        TypeError: The value is not JSON data.
        ValueError: A number is not finite, or an integer is beyond
            :py:data:`MAX_SAFE_INTEGER`.

    Examples:
        >>> canonical_json({"b": [1.0, 2.5e-7], "a": "€"})
        '{"a":"€","b":[1,2.5e-7]}'
    """
    parts: list[str] = []
    _serialize(value, parts)
    return "".join(parts)


def content_hash(value: Any) -> str:
    """Hash JSON data by its canonical JSON (RFC 8785).

    Args:
        value: JSON data.

    Returns:
        `sha256:<hex>` of the UTF-8 encoded canonical JSON.

    Raises:
        TypeError: The value is not JSON data.
        ValueError: A number is not finite, or a string is not valid
            Unicode.
    """
    try:
        data = canonical_json(value).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("JSON strings must be valid Unicode") from exc
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _pointer(parent: str, token: str | int) -> str:
    """Extend a JSON Pointer (RFC 6901).

    Args:
        parent: Pointer of the parent.
        token: Key or index.

    Returns:
        Pointer of the child.
    """
    return f"{parent}/{str(token).replace('~', '~0').replace('/', '~1')}"


def changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """List the JSON Pointers at which two JSON values differ.

    Objects are compared key by key, and arrays of the same length index
    by index; arrays of different lengths (e.g., after an insertion, which
    shifts all later items) and values of other or different types are
    compared as a whole.

    Args:
        before: Value before a change.
        after: Value after it.
        prefix: Pointer of the values.

    Returns:
        Pointers of the changed, added and removed values, in order (keys
        sorted); `[prefix]` if the values differ as a whole; `[]` if they
        are equal.

    Examples:
        >>> changed_paths({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 3], "c/d": 0})
        ['/b/1', '/c~1d']
        >>> changed_paths({"b": [1, 2]}, {"b": [0, 1, 2]})
        ['/b']
    """
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        paths = []
        for key in sorted({*before, *after}, key=str):
            child = _pointer(prefix, key)
            if key not in before or key not in after:
                paths.append(child)
            else:
                paths.extend(changed_paths(before[key], after[key], child))
        return paths
    if _is_array(before) and _is_array(after):
        if len(before) != len(after):
            return [prefix]
        paths = []
        for index, (old, new) in enumerate(zip(before, after, strict=True)):
            paths.extend(changed_paths(old, new, _pointer(prefix, index)))
        return paths
    if type(before) is not type(after) or before != after:
        return [prefix]
    return []


def _snapshot(phase: str, value: Any, secret_headers: frozenset[str]) -> Any:
    """Get the data of a phase that plugins may change, as JSON data.

    Args:
        phase: Phase.
        value: Input or output of a hook of the phase.
        secret_headers: Lowercase names of headers left out, besides
            :py:data:`foca.plugins.model.CREDENTIAL_HEADERS`.

    Returns:
        Data of the phase: method, path, query, headers (without
        credentials and secret headers) and payload of requests; the
        client's part of responses; candidate identifiers. The credential
        type of outbound requests (`auth`) is left out, as hashes of short
        values can be guessed.
    """
    if phase == "route":
        return [backend.id for backend in value]
    excluded = CREDENTIAL_HEADERS | secret_headers
    if phase == "response":
        return {
            "client_payload": value.client_payload,
            "client_headers": _headers(value.client_headers, excluded),
        }
    return {
        "method": value.method,
        "path": value.path,
        "query": value.query,
        "headers": _headers(value.headers, excluded),
        "payload": value.payload,
    }


def _headers(headers: Mapping[str, str], excluded: Iterable[str]) -> dict[str, str]:
    """Get headers with lowercase names, without excluded ones.

    Args:
        headers: Headers.
        excluded: Lowercase names of excluded headers.

    Returns:
        Headers.
    """
    left_out = set(excluded)
    return {
        name.lower(): text
        for name, text in headers.items()
        if name.lower() not in left_out
    }


def describe_changes(
    phase: str,
    before: Any,
    after: Any,
    secret_headers: Iterable[str] = (),
) -> dict[str, Any]:
    """Describe the changes of a hook call, without values.

    Args:
        phase: Phase.
        before: Input of the call.
        after: Committed output of the call.
        secret_headers: Names of headers left out besides credentials (cf.
            `plugins.provenance.secret_headers`).

    Returns:
        Hashes before and after and the changed JSON Pointers (at most
        :py:data:`MAX_CHANGED_PATHS`, with `truncated: true` if there are
        more); `error: not_json` if the data is not I-JSON.
    """
    secret = frozenset(name.lower() for name in secret_headers)
    old, new = _snapshot(phase, before, secret), _snapshot(phase, after, secret)
    try:
        paths = changed_paths(old, new)
        changes: dict[str, Any] = {
            "before": content_hash(old),
            "after": content_hash(new),
            "paths": paths[:MAX_CHANGED_PATHS],
        }
    except (TypeError, ValueError, ArithmeticError, RecursionError):
        return {"error": "not_json"}
    if len(paths) > MAX_CHANGED_PATHS:
        changes["truncated"] = True
    return changes


def _is_array(value: Any) -> bool:
    """Check whether a value is a JSON array.

    Args:
        value: Value.

    Returns:
        Whether it is a list or tuple.
    """
    return isinstance(value, list | tuple)


def _build_fields(loaded: LoadedPlugin) -> dict[str, Any]:
    """Get the fields that identify the build of a loaded plugin.

    Args:
        loaded: Loaded plugin.

    Returns:
        Plugin identifier, distribution, version, entry point, RECORD
        digest and the image digest from `FOCA_IMAGE_DIGEST`, if set.
    """
    return {
        "id": loaded.id,
        "distribution": loaded.distribution,
        "version": loaded.version,
        "entry_point": loaded.entry_point,
        "record_digest": loaded.record_digest,
        "image_digest": os.environ.get(IMAGE_DIGEST_ENV) or None,
    }


def build_key(loaded: LoadedPlugin) -> str:
    """Identify the build of a loaded plugin.

    Args:
        loaded: Loaded plugin.

    Returns:
        Hash of the plugin identifier, distribution, version, entry point,
        RECORD digest and image digest, so that a new image gives new build
        documents.
    """
    return content_hash(_build_fields(loaded))


def build_document(loaded: LoadedPlugin) -> dict[str, Any]:
    """Describe the build of a loaded plugin, for the `plugin_builds` store.

    Args:
        loaded: Loaded plugin.

    Returns:
        Document with the key as `_id`, the build fields, the plugin's API
        version and the image digest from `FOCA_IMAGE_DIGEST`, if set.
    """
    fields = _build_fields(loaded)
    return {
        "_id": content_hash(fields),
        **fields,
        "api_version": loaded.cls.api_version,
    }


class Collection(Protocol):
    """Collection that stores documents, e.g., of pymongo."""

    def update_one(self, filter: Any, update: Any, upsert: bool = ...) -> Any:
        """Update or insert one document.

        Args:
            filter: Query of the document.
            update: Update.
            upsert: Whether to insert the document if none matches.
        """

    def insert_one(self, document: Any) -> Any:
        """Insert one document.

        Args:
            document: Document.
        """


class ProvenanceStore:
    """Stores of provenance data in collections of the app.

    The app owns and declares the collections (e.g., in its `db` config);
    documents of plugin builds and pipelines are keyed by hash (`_id`) and
    written once.

    Attributes:
        plugin_builds: Collection of plugin builds.
        pipelines: Collection of default and preset pipelines.
        failures: Collection of failed-pipeline records, if any (cf.
            `plugins.provenance.record_failed`; index specs in
            :py:data:`FAILURE_INDEXES`).
    """

    def __init__(
        self,
        plugin_builds: Collection,
        pipelines: Collection,
        failures: Collection | None = None,
    ) -> None:
        """Create the store.

        Args:
            plugin_builds: Collection of plugin builds (pymongo).
            pipelines: Collection of default and preset pipelines (pymongo).
            failures: Collection of failed-pipeline records (pymongo), if
                they may be stored.
        """
        self.plugin_builds = plugin_builds
        self.pipelines = pipelines
        self.failures = failures

    def save(self, run: "PipelineRun") -> dict[str, Any]:
        """Store the shared parts of a run's provenance.

        Stores the builds of the run's plugins and, for default and preset
        pipelines, the resolved pipeline; client pipelines stay inline, so
        that they are deleted with the task. Never raises, as the backend
        may have accepted the request already: errors of the collections
        are logged, and the record then keeps what could not be stored
        (`stored: false`).

        Args:
            run: Pipeline run.

        Returns:
            Provenance to store with the task: as from `run.provenance()`,
            with default and preset pipelines by hash only, if stored.
        """
        record: dict[str, Any] = run.provenance()
        try:
            for loaded in run.host.plugins.values():
                document = build_document(loaded)
                if document["_id"] in record["plugins"]:
                    _insert_once(self.plugin_builds, document)
            pipeline = record["pipeline"]
            if pipeline["source"] != "client" and pipeline["resolved_hash"]:
                _insert_once(
                    self.pipelines,
                    {
                        "_id": pipeline["resolved_hash"],
                        "resolved": pipeline["resolved"],
                    },
                )
                del pipeline["resolved"]
        except Exception as exc:
            logger.warning("Provenance not stored: %s", type(exc).__name__)
            record["stored"] = False
        return record

    def save_failure(self, run: "PipelineRun", error: Exception) -> bool:
        """Store the record of a failed pipeline run, if configured.

        Never raises: errors of the collection are logged.

        Args:
            run: Pipeline run.
            error: Error that ended the run, e.g., a
                :py:class:`foca.plugins.GatewayError`.

        Returns:
            Whether the record was stored: only if
            `plugins.provenance.record_failed` is on, the store has a
            collection for failures, and storing succeeded.
        """
        if self.failures is None or not run.host.conf.provenance.record_failed:
            return False
        record = self.save(run)
        record["failed_at"] = datetime.now(UTC)
        record["error"] = {
            "type": type(error).__name__,
            "status": getattr(error, "code", None),
            "slug": getattr(error, "slug", None),
        }
        try:
            self.failures.insert_one(record)
        except Exception as exc:
            logger.warning("Failure record not stored: %s", type(exc).__name__)
            return False
        return True


def _insert_once(collection: Collection, document: dict[str, Any]) -> None:
    """Insert a document unless one with its `_id` exists.

    Args:
        collection: Collection.
        document: Document with `_id`.
    """
    collection.update_one(
        {"_id": document["_id"]}, {"$setOnInsert": document}, upsert=True
    )
