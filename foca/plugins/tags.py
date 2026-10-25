"""Server-written gateway tags of tasks and runs.

Gateways record where a request went in tags (TES and WES string maps)
under the reserved prefix `plugins.tags.prefix` (default `gateway.`), so
that clients and dashboards can read it with `GET /tasks/{id}` (views
`BASIC` and `FULL`) or `GET /runs/{id}`. Clients may set only
`<prefix>pipeline` and `<prefix>backends` (cf.
:py:mod:`foca.plugins.selection`); all other keys under the prefix are
written by the gateway (keys and values: cf. the docs, "Gateway tags").

FOCA provides the tags; apps write them: `creation_tags()` into new tasks
after the `request` phase (merged into `without_server_tags()` of the
payload's tags), `routing_tags()` after forwarding, and
`strip_for_forwarding()` for the tags relayed to the backend. The engine
refuses `request` hook output that adds, changes or removes keys under the
prefix (`check_reserved_tags()`), so plugins cannot forge them.

Values are strings of at most `MAX_VALUE_SIZE` bytes (`route`:
`MAX_ROUTE_SIZE`); URLs have no user info, query or fragment; failures are
reason codes, never messages. Relayed requests carry no key under the
prefix (cf. :py:func:`strip_for_forwarding`).

Pure functions on string maps and pipeline runs; no web framework.
"""

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from foca.plugins.provenance import clean_url as _clean_url

if TYPE_CHECKING:  # pragma: no cover
    from foca.plugins.engine import PipelineRun

MAX_VALUE_SIZE = 1024
"""Maximum size of tag values, in UTF-8 bytes, except for `route`."""

MAX_ROUTE_SIZE = 4096
"""Maximum size of the `route` tag, in UTF-8 bytes."""


def _size(value: str) -> int:
    """Get the size of a value.

    Args:
        value: Value.

    Returns:
        Size in UTF-8 bytes (lone surrogates count as `?`).
    """
    return len(value.encode("utf-8", errors="replace"))


def _clip(value: str, limit: int = MAX_VALUE_SIZE) -> str:
    """Shorten a value to a size limit.

    Args:
        value: Value.
        limit: Maximum size in UTF-8 bytes.

    Returns:
        The value, without trailing characters beyond the limit, and with
        lone surrogates (no valid UTF-8) replaced by `?`.
    """
    encoded = value.encode("utf-8", errors="replace")
    return encoded[:limit].decode("utf-8", errors="ignore")


def _reference(value: str | None) -> str | None:
    """Get a reference (path or identifier) as a tag value, if it fits.

    Cut references would point nowhere, so longer ones are left out.

    Args:
        value: Reference, if any.

    Returns:
        The value (with lone surrogates replaced by `?`), or `None` if there
        is none or it exceeds `MAX_VALUE_SIZE` bytes.
    """
    if value is None or _size(value) > MAX_VALUE_SIZE:
        return None
    return _clip(value)


def clean_url(url: str) -> str:
    """Remove user info, query and fragment from a URL.

    Args:
        url: URL.

    Returns:
        URL with scheme, host, port and path only, without trailing slash
        (only the scheme if the URL cannot be parsed, e.g., with an invalid
        port).
    """
    cleaned = _clean_url(url)
    return cleaned if cleaned.endswith("://") else cleaned.rstrip("/")


def strip_for_forwarding(tags: Mapping[str, str] | None, prefix: str) -> dict[str, str]:
    """Remove all gateway tags, e.g., from a task relayed to a backend.

    Args:
        tags: Tags of the request, if any.
        prefix: Reserved prefix (`plugins.tags.prefix`).

    Returns:
        Tags without any key under the prefix, also without the client's
        pipeline and backend choices.
    """
    return {
        key: value
        for key, value in (tags or {}).items()
        if not str(key).startswith(prefix)
    }


CLIENT_KEYS = ("pipeline", "backends")
"""Keys under the prefix that clients set (without the prefix)."""


def _tags_of(payload: object) -> Mapping[str, object]:
    """Get the tags of a payload (e.g., a TES task).

    Args:
        payload: Payload.

    Returns:
        Its `tags`, if it is a mapping with a mapping there; else none.
    """
    tags = payload.get("tags") if isinstance(payload, Mapping) else None
    return tags if isinstance(tags, Mapping) else {}


def reserved_tags(tags: Mapping[str, object] | None, prefix: str) -> dict[str, object]:
    """Get the tags under the reserved prefix.

    Args:
        tags: Tags, if any.
        prefix: Reserved prefix (`plugins.tags.prefix`).

    Returns:
        Tags whose keys start with the prefix.
    """
    return {
        key: value for key, value in (tags or {}).items() if str(key).startswith(prefix)
    }


def check_reserved_tags(before: object, after: object, prefix: str) -> None:
    """Check that a hook kept the tags under the reserved prefix of a payload.

    Args:
        before: Payload given to the hook.
        after: Payload after the hook.
        prefix: Reserved prefix.

    Raises:
        ValueError: A key under the prefix was added, changed or removed
            (the client's own `pipeline` and `backends` keys too).
    """
    old = reserved_tags(_tags_of(before), prefix)
    new = reserved_tags(_tags_of(after), prefix)
    if old != new:
        changed = sorted(
            key for key in old.keys() | new.keys() if old.get(key) != new.get(key)
        )
        raise ValueError(f"Plugins must not change tags under {prefix!r}: {changed}")


def without_server_tags(tags: Mapping[str, str] | None, prefix: str) -> dict[str, str]:
    """Drop the keys under the prefix that only the gateway writes.

    Use it on the payload's tags before merging the gateway's tags, as a
    second line of defence besides the engine's check.

    Args:
        tags: Tags, if any.
        prefix: Reserved prefix.

    Returns:
        Tags without keys under the prefix, except the client's
        `<prefix>pipeline` and `<prefix>backends`.
    """
    client = {f"{prefix}{key}" for key in CLIENT_KEYS}
    return {
        key: value
        for key, value in (tags or {}).items()
        if not str(key).startswith(prefix) or key in client
    }


def creation_tags(
    run: "PipelineRun", provenance_path: str | None = None
) -> dict[str, str]:
    """Get the gateway tags of a new task, after the `request` phase.

    Args:
        run: Pipeline run (:py:class:`foca.plugins.PipelineRun`).
        provenance_path: Absolute path of the task's provenance endpoint,
            e.g., `/ga4gh/tes/v1/tasks/ABC123/provenance`, if served.

    Returns:
        Tags under the run's prefix. `pipeline_hash` is left out if the
        pipeline has no hash (data that is no I-JSON); `plugins` lists as
        many plugins as fit into `MAX_VALUE_SIZE` bytes, in order (empty if
        the first does not fit).
    """
    prefix = run.host.conf.tags.prefix
    record = run.provenance()
    pipeline = record["pipeline"]
    source = pipeline["source"]
    if source == "preset":
        source = f"preset:{pipeline['preset']}"
    plugins: list[str] = []
    for execution in run.executions:
        # plugins that ran, whatever the outcome (e.g., a failed step that
        # was skipped)
        if execution.outcome in ("not_applicable", "skipped") or (
            execution.plugin is None
        ):
            continue
        loaded = run.host.plugins[execution.plugin]
        name = f"{execution.plugin}@{loaded.version or 'unknown'}"
        if name not in plugins:
            plugins.append(name)
    while plugins and _size(",".join(plugins)) > MAX_VALUE_SIZE:
        plugins.pop()
    tags = {
        f"{prefix}pipeline_source": _clip(source),
        f"{prefix}plugins": ",".join(plugins),
    }
    if pipeline["resolved_hash"] is not None:
        tags[f"{prefix}pipeline_hash"] = pipeline["resolved_hash"]
    provenance = _reference(provenance_path)
    if provenance is not None:
        tags[f"{prefix}provenance"] = provenance
    return tags


def routing_tags(run: "PipelineRun") -> dict[str, str]:
    """Get the gateway tags of a task after forwarding.

    Args:
        run: Pipeline run (:py:class:`foca.plugins.PipelineRun`).

    Returns:
        Tags under the run's prefix; `backend`, `backend_task_id` and
        `forwarded_at` only if a backend accepted the request or may have
        (`backend_task_id` only if it accepted).
    """
    conf = run.host.conf.tags
    prefix = conf.prefix
    records = run.attempt_records
    tags: dict[str, str] = {f"{prefix}attempts": str(len(records))}
    if records and records[-1].outcome != "not_accepted":
        last = records[-1]
        tags[f"{prefix}backend"] = _clip(clean_url(last.url))
        tags[f"{prefix}forwarded_at"] = last.started_at
        remote_id = _reference(last.remote_id)
        if last.outcome == "accepted" and remote_id is not None:
            tags[f"{prefix}backend_task_id"] = remote_id
    entries = [
        {
            key: value
            for key, value in (
                ("url", clean_url(record.url)),
                ("outcome", record.outcome),
                ("reason", record.reason),
            )
            if value is not None
        }
        for record in records
    ]
    shown = entries[-conf.max_route_entries :]
    route = json.dumps(shown, separators=(",", ":"))
    while len(shown) > 1 and _size(route) > MAX_ROUTE_SIZE:
        shown = shown[1:]
        route = json.dumps(shown, separators=(",", ":"))
    cut = False
    if shown and _size(route) > MAX_ROUTE_SIZE:
        # keep the latest attempt, with its URL cut
        shown = [{**shown[0], "url": _clip(shown[0]["url"])}]
        route = json.dumps(shown, separators=(",", ":"))
        cut = True
    tags[f"{prefix}route"] = route
    if cut or len(shown) < len(entries):
        tags[f"{prefix}route_truncated"] = "true"
    return tags
