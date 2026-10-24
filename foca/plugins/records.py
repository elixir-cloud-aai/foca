"""Resolved pipelines and records of hook calls and attempts."""

import functools
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from pydantic import BaseModel

from foca.plugins.provenance import changed_paths, content_hash

Outcome = Literal[
    "ok", "not_applicable", "error", "timeout", "invalid_output", "rejected", "skipped"
]


AttemptOutcome = Literal["accepted", "not_accepted", "unknown"]


@functools.cache
def _foca_version() -> str:
    """Get the installed version of FOCA, without importing the app factory.

    Returns:
        Version, or `0+unknown` if FOCA is not installed.
    """
    try:
        return version("foca")
    except PackageNotFoundError:  # pragma: no cover (source checkouts)
        return "0+unknown"


def _utc_now() -> str:
    """Get the current time for records.

    Returns:
        RFC 3339 UTC timestamp, with milliseconds.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ResolvedAlternative:
    """Alternative of a resolved pipeline step.

    Attributes:
        plugin: Identifier of the plugin.
        params: Validated parameters.
    """

    plugin: str
    params: BaseModel


@dataclass(frozen=True)
class ResolvedStep:
    """Step of a resolved pipeline.

    Attributes:
        id: Identifier of the step.
        alternatives: Alternatives with enabled plugins, in order.
        timeout: Seconds per alternative and call.
        on_failure: What happens when all alternatives failed.
        origin: Where the step comes from (e.g., `pipeline`).
    """

    id: str
    alternatives: tuple[ResolvedAlternative, ...]
    timeout: float
    on_failure: Literal["fail", "skip"]
    origin: str = "pipeline"


@dataclass(frozen=True)
class ResolvedPipeline:
    """Pipeline of a request, after resolution.

    Attributes:
        steps: Steps, in order.
        source: `default`, `preset` or `client`.
        preset: Name of the preset, if any.
    """

    steps: tuple[ResolvedStep, ...]
    source: Literal["default", "preset", "client"] = "default"
    preset: str | None = None

    def describe(self) -> list[dict[str, Any]]:
        """Describe the pipeline as JSON data, e.g., for provenance.

        Returns:
            Steps with identifiers, alternatives (plugins with all
            parameters, including defaults), timeouts and failure modes.
        """
        return [
            {
                "id": step.id,
                "origin": step.origin,
                "alternatives": [
                    {"plugin": alt.plugin, "params": alt.params.model_dump(mode="json")}
                    for alt in step.alternatives
                ],
                "timeout": step.timeout,
                "on_failure": step.on_failure,
            }
            for step in self.steps
        ]


@dataclass
class Execution:
    """Record of one call of a plugin hook (or of a skipped step).

    Attributes:
        step: Identifier of the step.
        phase: Phase.
        alternative: Index of the alternative in the step; `-1` for skipped
            steps.
        plugin: Identifier of the plugin; `None` for skipped steps.
        outcome: Outcome of the call.
        started_at: RFC 3339 UTC time of the call.
        duration_ms: Duration in milliseconds.
        attempt: Number of the forwarding attempt (from 1), for the
            `forward` and `response` phases.
        error: Sanitized error, if any (cf. :py:func:`sanitize_error`).
        annotations: Annotations recorded by the plugin.
        changes: Changes of a successful call (cf.
            `plugins.provenance.detail`): `before` and `after` hashes of the
            phase's data and the changed JSON Pointers (`paths`), without
            values.
    """

    step: str
    phase: str
    alternative: int
    plugin: str | None
    outcome: Outcome
    started_at: str
    duration_ms: float
    attempt: int | None = None
    error: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)
    changes: dict[str, Any] | None = None

    def describe(self, builds: Mapping[str, str]) -> dict[str, Any]:
        """Describe the call as JSON data, for provenance.

        Args:
            builds: Build keys by plugin identifier (cf.
                :py:func:`foca.plugins.provenance.build_key`).

        Returns:
            Record with the plugin's build key as `plugin`, its identifier
            as `plugin_id`, and without empty fields.
        """
        record = {
            "step": self.step,
            "phase": self.phase,
            "alternative": self.alternative,
            "plugin": builds.get(self.plugin) if self.plugin else None,
            "plugin_id": self.plugin,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "attempt": self.attempt,
            "error": self.error,
            "annotations": self.annotations,
            "changes": self.changes,
        }
        return {key: value for key, value in record.items() if value or value == 0}


@dataclass
class AttemptRecord:
    """Record of a forwarding attempt.

    Attributes:
        number: Number of the attempt, from 1.
        backend: Identifier of the backend.
        url: Base URL of the backend.
        outcome: `accepted`, `not_accepted` or `unknown`.
        reason: Reason code (e.g., `http_403`, `connect_error`), if any.
        remote_id: Identifier of the resource at the backend, if accepted.
        started_at: RFC 3339 UTC time of the attempt.
        duration_ms: Duration in milliseconds.
    """

    number: int
    backend: str
    url: str
    outcome: AttemptOutcome
    reason: str | None
    remote_id: str | None
    started_at: str
    duration_ms: float


def _snapshot(phase: str, value: Any) -> Any:
    """Get the data of a phase that plugins may change, as JSON data.

    Args:
        phase: Phase.
        value: Input or output of a hook of the phase.

    Returns:
        Data of the phase: method, path, query, headers and payload of
        requests; the client's part of responses; candidate identifiers.
    """
    if phase == "route":
        return [backend.id for backend in value]
    if phase == "response":
        return {
            "client_payload": value.client_payload,
            "client_headers": {k.lower(): v for k, v in value.client_headers.items()},
        }
    data = {
        "method": value.method,
        "path": value.path,
        "query": value.query,
        "headers": {name.lower(): text for name, text in value.headers.items()},
        "payload": value.payload,
    }
    if phase == "forward":
        data["auth"] = value.auth
    return data


def _changes(phase: str, before: Any, after: Any) -> dict[str, Any]:
    """Describe the changes of a hook call, without values.

    Args:
        phase: Phase.
        before: Input of the call.
        after: Committed output of the call.

    Returns:
        Hashes before and after and the changed JSON Pointers; `error` if
        the data is not JSON data.
    """
    old, new = _snapshot(phase, before), _snapshot(phase, after)
    try:
        return {
            "before": content_hash(old),
            "after": content_hash(new),
            "paths": changed_paths(old, new),
        }
    except (TypeError, ValueError):
        return {"error": "not_json"}
