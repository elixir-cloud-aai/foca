"""Server-written gateway tags of tasks and runs.

Gateways record where a request went in tags (TES and WES string maps)
under the reserved prefix `plugins.tags.prefix` (default `gateway.`), so
that clients and dashboards can read it with `GET /tasks/{id}` (views
`BASIC` and `FULL`) or `GET /runs/{id}`. Clients may set only
`<prefix>pipeline` and `<prefix>backends` (cf.
:py:mod:`foca.plugins.selection`); all other keys under the prefix are
written by the gateway:

| Key | Value | Written |
|---|---|---|
| `pipeline_hash` | `sha256:` of the resolved pipeline (RFC 8785) | at creation |
| `pipeline_source` | `default`, `preset:<name>` or `client` | at creation |
| `plugins` | comma-separated `id@version` of the plugins that ran | at creation |
| `provenance` | path of the provenance endpoint, if given | at creation |
| `backend` | base URL of the backend that accepted (or may have) | after forwarding |
| `backend_task_id` | identifier at the backend, if accepted | after forwarding |
| `forwarded_at` | RFC 3339 UTC time of that attempt | after forwarding |
| `attempts` | number of attempts, as a decimal | after forwarding |
| `route` | JSON of the last attempts (`url`, `outcome`, `reason`) | after forwarding |
| `route_truncated` | `true` if `route` omits earlier attempts | after forwarding |

Values are strings of at most `MAX_VALUE_SIZE` bytes (`route`:
`MAX_ROUTE_SIZE`); URLs have no user info, query or fragment; failures are
reason codes, never messages. Relayed requests carry no key under the
prefix (cf. :py:func:`strip_for_forwarding`).

Pure functions on string maps and pipeline runs; no web framework.
"""

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MAX_KEY_LENGTH = 64
"""Maximum length of tag keys."""

MAX_VALUE_SIZE = 1024
"""Maximum size of tag values, in UTF-8 bytes, except for `route`."""

MAX_ROUTE_SIZE = 4096
"""Maximum size of the `route` tag, in UTF-8 bytes."""


def _size(value: str) -> int:
    """Get the size of a value.

    Args:
        value: Value.

    Returns:
        Size in UTF-8 bytes.
    """
    return len(value.encode("utf-8"))


def _clip(value: str, limit: int = MAX_VALUE_SIZE) -> str:
    """Shorten a value to a size limit.

    Args:
        value: Value.
        limit: Maximum size in UTF-8 bytes.

    Returns:
        The value, without trailing characters beyond the limit.
    """
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def clean_url(url: str) -> str:
    """Remove user info, query and fragment from a URL.

    Args:
        url: URL.

    Returns:
        URL with scheme, host, port and path only, without trailing slash.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", "")).rstrip("/")


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


def creation_tags(run: Any, provenance_path: str | None = None) -> dict[str, str]:
    """Get the gateway tags of a new task, after the `request` phase.

    Args:
        run: Pipeline run (:py:class:`foca.plugins.PipelineRun`).
        provenance_path: Absolute path of the task's provenance endpoint,
            e.g., `/ga4gh/tes/v1/tasks/ABC123/provenance`, if served.

    Returns:
        Tags under the run's prefix.
    """
    prefix = run.host.conf.tags.prefix
    record = run.provenance()
    pipeline = record["pipeline"]
    source = pipeline["source"]
    if source == "preset":
        source = f"preset:{pipeline['preset']}"
    plugins: list[str] = []
    for execution in run.executions:
        if execution.outcome != "ok" or execution.plugin is None:
            continue
        loaded = run.host.plugins[execution.plugin]
        name = f"{execution.plugin}@{loaded.version or 'unknown'}"
        if name not in plugins:
            plugins.append(name)
    while plugins and _size(",".join(plugins)) > MAX_VALUE_SIZE:
        plugins.pop()
    tags = {
        f"{prefix}pipeline_hash": pipeline["resolved_hash"],
        f"{prefix}pipeline_source": _clip(source),
        f"{prefix}plugins": ",".join(plugins),
    }
    if provenance_path is not None:
        tags[f"{prefix}provenance"] = _clip(provenance_path)
    return tags


def routing_tags(run: Any) -> dict[str, str]:
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
        if last.outcome == "accepted" and last.remote_id is not None:
            tags[f"{prefix}backend_task_id"] = _clip(last.remote_id)
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
    while shown and _size(route) > MAX_ROUTE_SIZE:
        shown = shown[1:]
        route = json.dumps(shown, separators=(",", ":"))
    tags[f"{prefix}route"] = route
    if len(shown) < len(entries):
        tags[f"{prefix}route_truncated"] = "true"
    return tags
