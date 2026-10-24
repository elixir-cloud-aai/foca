"""Resolved pipelines and records of hook calls and attempts."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

Outcome = Literal[
    "ok", "not_applicable", "error", "timeout", "invalid_output", "rejected", "skipped"
]


AttemptOutcome = Literal["accepted", "not_accepted", "unknown"]


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
        before: Input of the call, for provenance (not serialized).
        after: Output of a successful call, for provenance (not serialized).
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
    before: Any = field(default=None, repr=False)
    after: Any = field(default=None, repr=False)


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
