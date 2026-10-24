"""Engine that runs the plugin pipelines of gateway requests.

Apps create one :py:class:`PluginHost` per process, at startup, and one
:py:class:`PipelineRun` per gateway request (e.g., `POST /tasks`), after
authentication, validation, access control and their own sanitation:

```python
host = PluginHost.from_config(get_config(), api="tes", validate=check_task)

run = host.start(inbound, principal)          # e.g., from foca.plugins.adapter
inbound = run.request()                       # phase `request`
candidates = run.route(backends)              # phase `route`
for attempt in run.attempts(candidates):      # phase `forward`, per backend
    try:
        result = send(attempt.outbound, timeout=attempt.remaining())
    except NotAccepted:                       # the backend certainly did not
        attempt.failed("connect_error")       # accept: next backend
        continue
    except OutcomeUnknown:                    # it may have accepted: stop
        attempt.unknown("read_timeout")
        raise
    response = attempt.succeeded(result)      # phase `response`
    break
```

Semantics:

- Each alternative of a step works on a copy of its input, which is kept
  only if the alternative succeeds; otherwise the next alternative runs on
  the unchanged input.
- `NotApplicable`, other exceptions, timeouts and invalid output (e.g., a
  payload that the app's `validate` rejects) make the engine try the next
  alternative; `Rejected` stops the request at once.
- When all alternatives of a step failed, the request fails
  (:py:class:`PipelineFailed`, `500`), unless the step has
  `on_failure: skip`. In the `forward` phase, the attempt fails instead and
  the next backend is tried; in the `response` phase, the step is skipped,
  as the backend accepted the request already.
- Hooks run in a bounded thread pool and are abandoned at their deadline;
  as they work on copies, abandoned hooks cannot change the request.
- Outbound requests carry a `Via` header with the gateway's name, and
  inbound requests that already passed this gateway are refused
  (:py:class:`LoopDetected`); backends at or below the gateway's own public
  URL are never candidates.
"""

import copy
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from foca.plugins.base import NotApplicable, Rejected
from foca.plugins.config import PluginsConfig, StepConfig
from foca.plugins.context import Context
from foca.plugins.loader import LoadedPlugin, check_legacy_keys, load_plugins
from foca.plugins.model import (
    Backend,
    BackendResponse,
    Headers,
    InboundRequest,
    OutboundRequest,
    Principal,
)

logger = logging.getLogger(__name__)

Outcome = Literal[
    "ok", "not_applicable", "error", "timeout", "invalid_output", "rejected", "skipped"
]
AttemptOutcome = Literal["accepted", "not_accepted", "unknown"]

MAX_ERROR_LENGTH = 500
"""Maximum length of error messages in records."""

PROBLEM_TYPE_BASE = "https://foca.readthedocs.io/en/latest/plugins/#"
"""Base of the problem types of gateway errors."""

_REASON = re.compile(r"[a-z0-9_]{1,64}")
# User info of URLs in error messages, which may hold credentials
_USER_INFO = re.compile(r"(//)[^/@\s]*@")


class NotAccepted(Exception):
    """The backend certainly did not accept the request; for apps' clients.

    Attributes:
        reason: Reason code, e.g., `connect_error`, `tls_error`,
            `not_public`, `no_credentials` or `http_<status>` (4xx, 3xx).
    """

    def __init__(self, reason: str, message: str = "") -> None:
        """Create the outcome.

        Args:
            reason: Reason code (`[a-z0-9_]{1,64}`).
            message: Description, for logs.

        Raises:
            ValueError: The reason code is invalid.
        """
        if not _REASON.fullmatch(reason):
            raise ValueError("Reason codes match [a-z0-9_]{1,64}")
        super().__init__(message or reason)
        self.reason = reason


class OutcomeUnknown(NotAccepted):
    """The backend may have accepted the request; for apps' clients.

    Raised, e.g., on read timeouts, lost connections after sending, and
    `5xx` statuses (reasons `read_timeout`, `connection_lost`,
    `http_<status>`); no other backend is then tried.
    """


class GatewayError(Exception):
    """Error of a gateway request, with problem details for the client.

    Attributes:
        code: HTTP status of the response to the client.
        title: Title of the problem.
        detail: Description of the problem, for the client.
        problem_type: URI of the problem type.
        run: Run of the request, e.g., for provenance; `None` before it
            started.
    """

    code = 500
    title = "Internal Server Error"
    slug = "gateway-error"

    def __init__(self, detail: str, run: "PipelineRun | None" = None) -> None:
        """Create the error.

        Args:
            detail: Description of the problem, for the client.
            run: Run of the request, if started.
        """
        super().__init__(detail)
        self.detail = detail
        self.run = run
        self.problem_type = f"{PROBLEM_TYPE_BASE}{self.slug}"


class PipelineRejected(GatewayError):
    """A plugin rejected the request (cf. :py:class:`foca.plugins.Rejected`)."""

    slug = "rejected"

    def __init__(
        self, rejection: Rejected, step: str, run: "PipelineRun | None" = None
    ) -> None:
        """Create the error.

        Args:
            rejection: Rejection raised by the plugin.
            step: Identifier of the step.
            run: Run of the request.
        """
        super().__init__(rejection.detail, run=run)
        self.code = rejection.status
        self.title = rejection.title or _phrase(rejection.status)
        self.step = step


class PipelineFailed(GatewayError):
    """All alternatives of a required step failed."""

    title = "Internal Server Error"
    slug = "plugin-step-failed"

    def __init__(self, step: str, run: "PipelineRun | None" = None) -> None:
        """Create the error.

        Args:
            step: Identifier of the step.
            run: Run of the request.
        """
        super().__init__(f"Plugin step failed: {step}", run=run)
        self.step = step


class NoBackendAvailable(GatewayError):
    """No backend accepted the request."""

    code = 503
    title = "Service Unavailable"
    slug = "no-backend-available"


class ForwardTimeout(GatewayError):
    """The forwarding attempts of the request took too long."""

    code = 503
    title = "Service Unavailable"
    slug = "forward-timeout"


class LoopDetected(GatewayError):
    """The request already passed this gateway (`Via` header)."""

    code = 508
    title = "Loop Detected"
    slug = "loop-detected"


def _phrase(status: int) -> str:
    """Get the reason phrase of a rejection status.

    Args:
        status: HTTP status, one of :py:data:`foca.plugins.base.REJECTION_STATUSES`.

    Returns:
        Reason phrase as per RFC 9110.
    """
    return {
        400: "Bad Request",
        403: "Forbidden",
        409: "Conflict",
        413: "Content Too Large",
        422: "Unprocessable Content",
    }[status]


def sanitize_error(exc: BaseException) -> str:
    """Describe an exception for records, without credentials.

    Args:
        exc: Exception.

    Returns:
        Type and message, with user info removed from URLs, at most
        `MAX_ERROR_LENGTH` characters.
    """
    message = _USER_INFO.sub(r"\1", f"{type(exc).__name__}: {exc}")
    return message[:MAX_ERROR_LENGTH]


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


class _DeadlineExceededError(Exception):
    """A hook did not return in time, or no worker was free."""


class _AppError(Exception):
    """The app's sanitation failed; its exception reaches the caller."""

    def __init__(self, error: Exception) -> None:
        """Wrap the app's exception.

        Args:
            error: Exception raised by the app.
        """
        super().__init__(str(error))
        self.error = error


class _InvalidOutputError(Exception):
    """A hook produced invalid output."""


class _StepFailedError(Exception):
    """All alternatives of a step failed."""

    def __init__(self, step: str) -> None:
        """Create the error.

        Args:
            step: Identifier of the step.
        """
        super().__init__(step)
        self.step = step


class _Executor:
    """Bounded thread pool for hook calls, abandoning calls at deadlines."""

    def __init__(self, workers: int) -> None:
        """Create the pool.

        Args:
            workers: Number of threads.
        """
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="foca-plugin"
        )
        self._slots = threading.BoundedSemaphore(workers)

    def call(self, function: Callable[[], Any], timeout: float) -> Any:
        """Call a function in the pool.

        Args:
            function: Function.
            timeout: Seconds to wait for the result.

        Returns:
            Result of the function.

        Raises:
            _DeadlineExceededError: The timeout passed, or no thread is
                free; the call is abandoned.
        """
        if timeout <= 0:
            raise _DeadlineExceededError("deadline passed before the call")
        if not self._slots.acquire(blocking=False):
            raise _DeadlineExceededError("all plugin workers are busy")
        try:
            future = self._pool.submit(function)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            if future.done():
                raise
            raise _DeadlineExceededError(f"no result after {timeout:.3f} s") from None

    def shutdown(self) -> None:
        """Stop the pool, without waiting for running calls."""
        self._pool.shutdown(wait=False, cancel_futures=True)


def _via_names(headers: Mapping[str, str]) -> list[str]:
    """Get the names of the proxies in a `Via` header.

    Args:
        headers: Headers.

    Returns:
        `received-by` names, lowercase (RFC 9110, section 7.6.3).
    """
    names = []
    for entry in headers.get("Via", "").split(","):
        parts = entry.split()
        if len(parts) >= 2:
            names.append(parts[1].lower())
    return names


def default_outbound(backend: Backend, inbound: InboundRequest) -> OutboundRequest:
    """Build the outbound request of an attempt from the inbound request.

    Args:
        backend: Backend of the attempt.
        inbound: Inbound request, after the `request` phase.

    Returns:
        Request with the method, path, query and a copy of the payload of
        the inbound request; no headers of the client are forwarded.
    """
    return OutboundRequest(
        backend=backend,
        method=inbound.method,
        path=inbound.path,
        query=copy.deepcopy(inbound.query),
        headers=Headers(),
        payload=copy.deepcopy(inbound.payload),
    )


class PluginHost:
    """Plugins of a process, and the factory of pipeline runs.

    Attributes:
        conf: Plugin configuration.
        api: API of the app, e.g., `tes`.
        plugins: Loaded plugins, by identifier.
        public_url: Public URL of the gateway, normalized, if known.
        gateway_id: Name of the gateway in `Via` headers, if known.
        default_pipeline: Resolved default pipeline.
    """

    def __init__(
        self,
        conf: PluginsConfig,
        *,
        api: str,
        validate: Callable[[Any], None] | None = None,
        sanitize: Callable[[Any], Any] | None = None,
        validate_outbound: Callable[[OutboundRequest], None] | None = None,
        public_url: str | None = None,
        plugins: Mapping[str, LoadedPlugin] | None = None,
    ) -> None:
        """Load the plugins and resolve the default pipeline.

        Args:
            conf: Plugin configuration.
            api: API of the app (e.g., `tes`, `wes`), which plugins must
                support.
            validate: Function that checks the payload of an inbound request
                (e.g., against the app's model), raising an exception if it
                is invalid; called after each successful `request` hook. If
                it raises, the hook's output counts as invalid, and the next
                alternative is tried.
            sanitize: Function that returns the payload of an inbound
                request after the app's sanitation (e.g., removing
                credentials from URLs); called after each successful
                `request` hook, before `validate`. Its exceptions (e.g., a
                `413` for too much inline content) stop the request and
                reach the caller unchanged.
            validate_outbound: Function that checks the outbound request of
                an attempt after the `forward` phase (e.g., that the app can
                serialize its payload), raising an exception if it is
                invalid; the attempt then counts as not accepted (reason
                `invalid_outbound`), and the next backend is tried.
            public_url: Public URL of the gateway, normalized (e.g.,
                `server.public_url`).
            plugins: Loaded plugins, e.g., for tests (cf.
                :py:mod:`foca.plugins.testing`); by default, the plugins of
                the allowlist are loaded from their entry points.

        Raises:
            ValueError: Plugins cannot be loaded (cf.
                :py:func:`foca.plugins.loader.load_plugins`), or the default
                pipeline is invalid.
        """
        self.conf = conf
        self.api = api
        self._validate = validate
        self._sanitize = sanitize
        self._validate_outbound = validate_outbound
        self.public_url = public_url.rstrip("/") if public_url else None
        self.gateway_id = conf.gateway_id or (
            urlsplit(public_url).netloc.lower() if public_url else None
        )
        self.plugins: dict[str, LoadedPlugin] = (
            dict(plugins) if plugins is not None else load_plugins(conf, api=api)
        )
        self._executor = _Executor(conf.defaults.workers)
        self.default_pipeline = ResolvedPipeline(
            steps=self.resolve_steps(conf.pipeline, origin="pipeline")
        )

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        api: str,
        validate: Callable[[Any], None] | None = None,
        sanitize: Callable[[Any], Any] | None = None,
        validate_outbound: Callable[[OutboundRequest], None] | None = None,
    ) -> "PluginHost":
        """Create the host from the FOCA configuration.

        Args:
            config: FOCA configuration (:py:class:`foca.models.config.Config`).
            api: API of the app.
            validate: Payload check (cf. :py:class:`PluginHost`).
            sanitize: Payload sanitation (cf. :py:class:`PluginHost`).
            validate_outbound: Outbound check (cf. :py:class:`PluginHost`).

        Returns:
            Host with the plugins of `plugins` (none if not configured).

        Raises:
            ValueError: The configuration has keys of earlier middleware
                stacks (cf. :py:func:`foca.plugins.loader.check_legacy_keys`),
                or the plugins cannot be loaded.
        """
        check_legacy_keys(config)
        conf = getattr(config, "plugins", None) or PluginsConfig()
        return cls(
            conf,
            api=api,
            validate=validate,
            sanitize=sanitize,
            validate_outbound=validate_outbound,
            public_url=config.server.public_url,
        )

    def resolve_steps(
        self, steps: Sequence[StepConfig], origin: str
    ) -> tuple[ResolvedStep, ...]:
        """Resolve the steps of a pipeline: enabled plugins, validated params.

        Alternatives of disabled plugins are dropped; steps left without
        alternatives are dropped if they may be skipped.

        Args:
            steps: Steps, as configured.
            origin: Where the steps come from, for records.

        Returns:
            Resolved steps.

        Raises:
            ValueError: A plugin is not in the allowlist, the parameters of
                an alternative are invalid, or a step that may not be
                skipped has no enabled alternative.
        """
        resolved = []
        for step in steps:
            alternatives = []
            for alternative in step.alternatives:
                available = self.conf.available.get(alternative.plugin)
                if available is None:
                    raise ValueError(f"Plugin not available: {alternative.plugin}")
                loaded = self.plugins.get(alternative.plugin)
                if loaded is None:
                    continue
                try:
                    params = loaded.cls.Params.model_validate(alternative.params)
                except ValidationError as exc:
                    raise ValueError(
                        f"Invalid params of plugin {alternative.plugin!r} in step"
                        f" {step.id!r}: {exc}"
                    ) from exc
                alternatives.append(
                    ResolvedAlternative(plugin=alternative.plugin, params=params)
                )
            if not alternatives:
                if step.on_failure == "skip":
                    logger.warning("Step %s has no enabled plugin; dropped", step.id)
                    continue
                raise ValueError(f"Step {step.id!r} has no enabled plugin")
            resolved.append(
                ResolvedStep(
                    id=step.id or "",
                    alternatives=tuple(alternatives),
                    timeout=step.timeout or self.conf.defaults.timeout,
                    on_failure=step.on_failure,
                    origin=origin,
                )
            )
        return tuple(resolved)

    def start(
        self,
        inbound: InboundRequest,
        principal: Principal,
        *,
        request_id: str | None = None,
        pipeline: ResolvedPipeline | None = None,
    ) -> "PipelineRun":
        """Start the pipeline run of a request.

        Args:
            inbound: Inbound request, after the app's checks and sanitation.
            principal: Authenticated caller.
            request_id: Identifier of the request, for logs.
            pipeline: Pipeline to run; defaults to the default pipeline.

        Returns:
            Run of the request.

        Raises:
            LoopDetected: The request already passed this gateway.
        """
        if self.gateway_id is not None and self.gateway_id.lower() in _via_names(
            inbound.headers
        ):
            raise LoopDetected(f"Request already passed gateway {self.gateway_id}")
        return PipelineRun(
            self,
            pipeline=pipeline or self.default_pipeline,
            inbound=inbound,
            principal=principal,
            request_id=request_id,
        )

    def is_own_url(self, url: str) -> bool:
        """Check whether a URL is at or below the gateway's public URL.

        Args:
            url: Normalized URL.

        Returns:
            Whether the URL is the public URL or below it.
        """
        if self.public_url is None:
            return False
        url = url.rstrip("/")
        return url == self.public_url or url.startswith(f"{self.public_url}/")

    def check_payload(self, payload: Any) -> Any:
        """Sanitize and validate a payload, as after each `request` hook.

        Args:
            payload: Payload.

        Returns:
            Sanitized payload.

        Raises:
            _AppError: Sanitation failed (wrapping the app's exception, which
                the engine raises to the caller).
        """
        if self._sanitize is not None:
            try:
                payload = self._sanitize(payload)
            except Exception as exc:
                raise _AppError(exc) from exc
        if self._validate is not None:
            self._validate(payload)
        return payload

    def call(self, function: Callable[[], Any], timeout: float) -> Any:
        """Call a hook in the bounded thread pool.

        Args:
            function: Function calling the hook.
            timeout: Seconds to wait.

        Returns:
            Result of the hook.
        """
        return self._executor.call(function, timeout)

    def close(self) -> None:
        """Close the plugins and stop the thread pool, e.g., at shutdown."""
        for plugin in self.plugins.values():
            try:
                plugin.instance.close()
            except Exception:
                logger.exception("Plugin %s failed to close", plugin.id)
        self._executor.shutdown()


class Attempt:
    """Forwarding attempt to one backend.

    Attributes:
        number: Number of the attempt, from 1.
        backend: Backend.
        outbound: Outbound request, after the `forward` phase.
        state: Outcome, once reported; `None` before.
    """

    def __init__(
        self,
        run: "PipelineRun",
        number: int,
        outbound: OutboundRequest,
        budget_end: float,
    ) -> None:
        """Create the attempt.

        Args:
            run: Run of the request.
            number: Number of the attempt.
            outbound: Outbound request.
            budget_end: `time.monotonic()` value by which forwarding must
                end.
        """
        self._run = run
        self.number = number
        self.backend = outbound.backend
        self.outbound = outbound
        self.state: AttemptOutcome | None = None
        self._budget_end = budget_end
        self._started = time.monotonic()
        self._started_at = _utc_now()

    def remaining(self) -> float:
        """Get the seconds left for this and further attempts.

        Returns:
            Seconds, at least 0; use them as timeout of the request.
        """
        return max(0.0, self._budget_end - time.monotonic())

    def _report(
        self, outcome: AttemptOutcome, reason: str | None, remote_id: str | None
    ) -> None:
        """Record the outcome of the attempt.

        Args:
            outcome: Outcome.
            reason: Reason code, if any.
            remote_id: Identifier of the resource at the backend, if any.

        Raises:
            RuntimeError: The outcome was reported before.
            ValueError: The reason code is invalid.
        """
        if self.state is not None:
            raise RuntimeError("The outcome of the attempt was reported already")
        if reason is not None and not _REASON.fullmatch(reason):
            raise ValueError("Reason codes match [a-z0-9_]{1,64}")
        self.state = outcome
        self._run.attempt_records.append(
            AttemptRecord(
                number=self.number,
                backend=self.backend.id,
                url=self.backend.url,
                outcome=outcome,
                reason=reason,
                remote_id=remote_id,
                started_at=self._started_at,
                duration_ms=round((time.monotonic() - self._started) * 1000, 3),
            )
        )

    def failed(self, reason: str) -> None:
        """Report that the backend certainly did not accept the request.

        The engine then tries the next backend.

        Args:
            reason: Reason code, e.g., `connect_error`, `tls_error`,
                `http_403` or `no_credentials`.
        """
        self._report("not_accepted", reason, None)

    def unknown(self, reason: str) -> None:
        """Report that the backend may have accepted the request.

        The engine then tries no other backend, so that the request is not
        executed twice (e.g., after a read timeout or a `5xx` status).

        Args:
            reason: Reason code, e.g., `read_timeout` or `http_502`.
        """
        self._report("unknown", reason, None)

    def report(self, outcome: NotAccepted) -> None:
        """Report the outcome of the attempt from an exception of the app.

        Args:
            outcome: :py:class:`OutcomeUnknown` (the backend may have
                accepted) or :py:class:`NotAccepted`.
        """
        if isinstance(outcome, OutcomeUnknown):
            self.unknown(outcome.reason)
        else:
            self.failed(outcome.reason)

    def succeeded(
        self, response: BackendResponse, remote_id: str | None = None
    ) -> BackendResponse:
        """Report that the backend accepted the request; run `response` hooks.

        Args:
            response: Response of the backend; `client_payload` and
                `client_headers` are what the client gets, unless plugins
                change them.
            remote_id: Identifier of the resource at the backend, if any.

        Returns:
            The response after the `response` phase.
        """
        self._report("accepted", None, remote_id)
        return self._run.respond(response, attempt=self.number)


class PipelineRun:
    """Run of the pipeline of one gateway request.

    Attributes:
        host: Plugin host.
        pipeline: Resolved pipeline.
        inbound: Inbound request; after `request()`, the committed one.
        principal: Authenticated caller.
        request_id: Identifier of the request.
        task_id: Identifier of the gateway's resource, once created; set by
            the app, passed to hooks of later phases.
        original: Payload of the inbound request before any plugin ran.
        executions: Records of the hook calls, in order.
        attempt_records: Records of the forwarding attempts, in order.
        candidates_in: Identifiers of the candidates before the `route`
            phase.
        candidates_out: Identifiers of the candidates after it.
    """

    def __init__(
        self,
        host: PluginHost,
        *,
        pipeline: ResolvedPipeline,
        inbound: InboundRequest,
        principal: Principal,
        request_id: str | None,
    ) -> None:
        """Create the run.

        Args:
            host: Plugin host.
            pipeline: Resolved pipeline.
            inbound: Inbound request.
            principal: Authenticated caller.
            request_id: Identifier of the request.
        """
        self.host = host
        self.pipeline = pipeline
        self.inbound = inbound
        self.principal = principal
        self.request_id = request_id
        self.task_id: str | None = None
        self.original = copy.deepcopy(inbound.payload)
        self.executions: list[Execution] = []
        self.attempt_records: list[AttemptRecord] = []
        self.candidates_in: list[str] = []
        self.candidates_out: list[str] = []
        self._states: dict[str, dict[str, Any]] = {}
        self._deadline = time.monotonic() + host.conf.defaults.pipeline_timeout

    def implements(self, phase: str) -> bool:
        """Check whether a step of the pipeline implements a phase.

        Args:
            phase: Phase.

        Returns:
            Whether a plugin of an alternative has the phase's hook.
        """
        return any(
            phase in self.host.plugins[alt.plugin].cls.phases()
            for step in self.pipeline.steps
            for alt in step.alternatives
        )

    def request(self) -> InboundRequest:
        """Run the `request` phase.

        Returns:
            The inbound request after the phase (also in `inbound`).

        Raises:
            PipelineRejected: A plugin rejected the request.
            PipelineFailed: All alternatives of a required step failed.
            Exception: The app's `sanitize` failed (its exception).
        """

        def check(candidate: InboundRequest, _: InboundRequest) -> InboundRequest:
            candidate.payload = self.host.check_payload(candidate.payload)
            return candidate

        self.inbound = self._run_phase(
            "request", self.inbound, check=check, deadline=self._deadline
        )
        return self.inbound

    def route(self, candidates: Sequence[Backend]) -> list[Backend]:
        """Run the `route` phase.

        Backends at or below the gateway's public URL are removed first.

        Args:
            candidates: Candidate backends, in the server's order.

        Returns:
            Candidates after the phase, in order.

        Raises:
            PipelineRejected: A plugin rejected the request.
            PipelineFailed: All alternatives of a required step failed.
        """
        own = [
            backend.id for backend in candidates if self.host.is_own_url(backend.url)
        ]
        if own:
            logger.warning("Backends at the gateway's own URL removed: %s", own)
        self.candidates_in = [backend.id for backend in candidates]
        routed: list[Backend] = self._run_phase(
            "route",
            [backend for backend in candidates if backend.id not in own],
            check=_check_route,
            deadline=self._deadline,
        )
        self.candidates_out = [backend.id for backend in routed]
        return routed

    def attempts(
        self,
        candidates: Sequence[Backend],
        build: Callable[[Backend, InboundRequest], OutboundRequest] = default_outbound,
    ) -> Iterator[Attempt]:
        """Run the `forward` phase for each candidate, until one accepts.

        Args:
            candidates: Candidates, in order (e.g., from `route()`).
            build: Function building the outbound request for a backend
                (cf. :py:func:`default_outbound`).

        Yields:
            Attempts, after the `forward` phase; report the outcome of each
            (`failed()`, `unknown()` or `succeeded()`). Attempts whose
            `forward` step failed, or whose outbound request the app's
            `validate_outbound` rejects, are skipped (recorded as not
            accepted, reason `forward_plugin_failed` or `invalid_outbound`).

        Raises:
            ForwardTimeout: `plugins.defaults.forward_timeout` passed before
                a backend accepted.
            NoBackendAvailable: No backend accepted the request.
            PipelineRejected: A plugin rejected the request.
            RuntimeError: The outcome of an attempt was not reported.
        """
        budget_end = time.monotonic() + self.host.conf.defaults.forward_timeout
        for backend in candidates:
            if self.host.is_own_url(backend.url):
                continue
            if time.monotonic() >= budget_end:
                timeout = self.host.conf.defaults.forward_timeout
                raise ForwardTimeout(
                    f"No backend accepted within {timeout} s", run=self
                )
            number = len(self.attempt_records) + 1
            started, started_at = time.monotonic(), _utc_now()
            reason = None
            try:
                outbound = self._run_phase(
                    "forward",
                    build(backend, self.inbound),
                    check=_check_forward,
                    deadline=budget_end,
                    attempt=number,
                )
                if self.host._validate_outbound is not None:
                    try:
                        self.host._validate_outbound(outbound)
                    except Exception as exc:
                        logger.warning(
                            "Outbound request to %s invalid: %s",
                            backend.id,
                            sanitize_error(exc),
                        )
                        reason = "invalid_outbound"
            except _StepFailedError:
                reason = "forward_plugin_failed"
            if reason is not None:
                self.attempt_records.append(
                    AttemptRecord(
                        number=number,
                        backend=backend.id,
                        url=backend.url,
                        outcome="not_accepted",
                        reason=reason,
                        remote_id=None,
                        started_at=started_at,
                        duration_ms=round((time.monotonic() - started) * 1000, 3),
                    )
                )
                continue
            if self.host.gateway_id is not None:
                via = outbound.headers.get("Via")
                entry = f"1.1 {self.host.gateway_id}"
                outbound.headers["Via"] = f"{via}, {entry}" if via else entry
            attempt = Attempt(
                self, number=number, outbound=outbound, budget_end=budget_end
            )
            yield attempt
            if attempt.state is None:
                raise RuntimeError("Report the outcome of each attempt")
            if attempt.state != "not_accepted":
                return
        raise NoBackendAvailable("No backend accepted the request", run=self)

    def respond(self, response: BackendResponse, attempt: int) -> BackendResponse:
        """Run the `response` phase, in reverse step order.

        Steps whose alternatives all fail are skipped, as the backend has
        accepted the request already.

        Args:
            response: Response of the backend.
            attempt: Number of the attempt.

        Returns:
            The response after the phase.
        """
        return self._run_phase(
            "response",
            response,
            check=_check_response,
            deadline=float("inf"),
            attempt=attempt,
            reverse=True,
        )

    def _run_phase(
        self,
        phase: str,
        value: Any,
        check: Callable[[Any, Any], Any],
        deadline: float,
        attempt: int | None = None,
        reverse: bool = False,
    ) -> Any:
        """Run the steps of a phase on a value.

        Args:
            phase: Phase.
            value: Input of the first step.
            check: Function checking the output of a call against its input,
                returning the output to commit, or raising an exception if
                it is invalid.
            deadline: `time.monotonic()` value by which the phase must end.
            attempt: Number of the forwarding attempt, if any.
            reverse: Whether to run the steps in reverse order.

        Returns:
            Output of the last step.

        Raises:
            PipelineRejected: A plugin rejected the request.
            PipelineFailed: All alternatives of a required step failed
                (`request` and `route` phases).
            _StepFailedError: All alternatives of a required step failed
                (`forward` phase).
        """
        steps = reversed(self.pipeline.steps) if reverse else self.pipeline.steps
        for step in steps:
            value = self._run_step(phase, step, value, check, deadline, attempt)
        return value

    def _run_step(
        self,
        phase: str,
        step: ResolvedStep,
        value: Any,
        check: Callable[[Any, Any], Any],
        deadline: float,
        attempt: int | None,
    ) -> Any:
        """Run the alternatives of a step until one succeeds.

        Args:
            phase: Phase.
            step: Step.
            value: Input.
            check: Output check (cf. `_run_phase()`).
            deadline: `time.monotonic()` value by which the phase must end.
            attempt: Number of the forwarding attempt, if any.

        Returns:
            Output of the successful alternative, or the input if no
            alternative implements the phase or the step was skipped.

        Raises:
            PipelineRejected: A plugin rejected the request.
            PipelineFailed: All alternatives failed (`request`, `route`).
            _StepFailedError: All alternatives failed (`forward`).
        """
        tried = False
        for index, alternative in enumerate(step.alternatives):
            loaded = self.host.plugins[alternative.plugin]
            hook = getattr(loaded.instance, f"on_{phase}", None)
            if hook is None:
                continue
            tried = True
            execution, output = self._call(
                phase, step, index, alternative, hook, value, check, deadline, attempt
            )
            self.executions.append(execution)
            if execution.outcome == "ok":
                return output
        if not tried:
            return value
        if step.on_failure == "skip" or phase == "response":
            self.executions.append(
                Execution(
                    step=step.id,
                    phase=phase,
                    alternative=-1,
                    plugin=None,
                    outcome="skipped",
                    started_at=_utc_now(),
                    duration_ms=0.0,
                    attempt=attempt,
                )
            )
            return value
        if phase in ("request", "route"):
            raise PipelineFailed(step.id, run=self)
        raise _StepFailedError(step.id)

    def _call(
        self,
        phase: str,
        step: ResolvedStep,
        index: int,
        alternative: ResolvedAlternative,
        hook: Callable[[Any, Context], Any],
        value: Any,
        check: Callable[[Any, Any], Any],
        deadline: float,
        attempt: int | None,
    ) -> tuple[Execution, Any]:
        """Call the hook of one alternative on a copy of the input.

        Args:
            phase: Phase.
            step: Step.
            index: Index of the alternative.
            alternative: Alternative.
            hook: Hook of the plugin.
            value: Input.
            check: Output check (cf. `_run_phase()`).
            deadline: `time.monotonic()` value by which the phase must end.
            attempt: Number of the forwarding attempt, if any.

        Returns:
            Record of the call, and the checked output (`None` unless the
            call succeeded).

        Raises:
            PipelineRejected: The plugin rejected the request (except in
                the `response` phase, where rejections count as errors).
        """
        candidate = copy.deepcopy(value)
        state = copy.deepcopy(self._states.get(alternative.plugin, {}))
        call_deadline = min(time.monotonic() + step.timeout, deadline)
        ctx = Context(
            api=self.host.api,
            plugin_id=alternative.plugin,
            principal=self.principal,
            params=alternative.params,
            original=self.original,
            deadline=call_deadline,
            state=state,
            request_id=self.request_id,
            task_id=self.task_id,
        )
        execution = Execution(
            step=step.id,
            phase=phase,
            alternative=index,
            plugin=alternative.plugin,
            outcome="ok",
            started_at=_utc_now(),
            duration_ms=0.0,
            attempt=attempt,
            before=value,
        )
        started = time.monotonic()
        output = None
        try:
            result = self.host.call(
                lambda: hook(candidate, ctx), call_deadline - time.monotonic()
            )
            try:
                output = check(result if phase == "route" else candidate, value)
            except _AppError:
                raise
            except Exception as exc:
                raise _InvalidOutputError(sanitize_error(exc)) from exc
        except _AppError as exc:
            execution.outcome = "error"
            execution.error = sanitize_error(exc.error)
            self._finish(execution, started, ctx)
            self.executions.append(execution)
            raise exc.error from None
        except Rejected as exc:
            execution.outcome = "rejected"
            execution.error = sanitize_error(exc)
            if phase != "response":
                self._finish(execution, started, ctx)
                self.executions.append(execution)
                raise PipelineRejected(exc, step=step.id, run=self) from exc
        except NotApplicable:
            execution.outcome = "not_applicable"
        except _DeadlineExceededError as exc:
            execution.outcome = "timeout"
            execution.error = str(exc)
        except _InvalidOutputError as exc:
            execution.outcome = "invalid_output"
            execution.error = str(exc)
        except Exception as exc:
            execution.outcome = "error"
            execution.error = sanitize_error(exc)
            logger.warning(
                "Plugin %s failed in step %s (%s): %s",
                alternative.plugin,
                step.id,
                phase,
                execution.error,
            )
        self._finish(execution, started, ctx)
        if execution.outcome != "ok":
            return execution, None
        self._states[alternative.plugin] = state
        execution.after = output
        return execution, output

    @staticmethod
    def _finish(execution: Execution, started: float, ctx: Context) -> None:
        """Complete the record of a call.

        Args:
            execution: Record.
            started: `time.monotonic()` value at the start of the call.
            ctx: Context of the call.
        """
        execution.duration_ms = round((time.monotonic() - started) * 1000, 3)
        execution.annotations = dict(ctx.annotations)


def _check_route(result: Any, candidates: list[Backend]) -> list[Backend]:
    """Check the output of a `route` hook.

    Args:
        result: Return value of the hook.
        candidates: Candidates given to the hook.

    Returns:
        The candidates returned, as the original objects.

    Raises:
        TypeError: The result is not a list of backends.
        ValueError: The result has backends that are not candidates, or
            duplicates.
    """
    if not isinstance(result, list) or not all(isinstance(b, Backend) for b in result):
        raise TypeError("Route hooks return a list of backends")
    by_id = {backend.id: backend for backend in candidates}
    ids = [backend.id for backend in result]
    if len(set(ids)) != len(ids):
        raise ValueError("Route hooks must not return backends twice")
    unknown = [backend_id for backend_id in ids if backend_id not in by_id]
    if unknown or any(by_id[b.id] != b for b in result):
        raise ValueError("Route hooks may only return given candidates, unchanged")
    return [by_id[backend_id] for backend_id in ids]


def _check_forward(
    candidate: OutboundRequest, given: OutboundRequest
) -> OutboundRequest:
    """Check the output of a `forward` hook.

    Args:
        candidate: Outbound request after the hook.
        given: Outbound request given to the hook.

    Returns:
        The outbound request.

    Raises:
        ValueError: The backend was changed, or the method or path is
            invalid.
        TypeError: The headers are not :py:class:`foca.plugins.model.Headers`.
    """
    if candidate.backend != given.backend:
        raise ValueError("Forward hooks must not change the backend")
    if not isinstance(candidate.headers, Headers):
        raise TypeError("Forward hooks must keep Headers")
    if not isinstance(candidate.method, str) or not candidate.method.isalpha():
        raise ValueError("Invalid method")
    candidate.method = candidate.method.upper()
    path = candidate.path
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("Paths start with a single '/'")
    if any(char in path for char in "?#\\") or any(ord(char) < 33 for char in path):
        raise ValueError("Paths must not contain queries, fragments or controls")
    return candidate


def _check_response(
    candidate: BackendResponse, given: BackendResponse
) -> BackendResponse:
    """Check the output of a `response` hook.

    Args:
        candidate: Response after the hook.
        given: Response given to the hook.

    Returns:
        The response.

    Raises:
        ValueError: The backend's status, headers or payload were changed.
    """
    if (
        candidate.status != given.status
        or candidate.headers != given.headers
        or candidate.payload != given.payload
    ):
        raise ValueError("Response hooks may only change the client's response")
    return candidate


__all__ = [
    "Attempt",
    "AttemptRecord",
    "Execution",
    "ForwardTimeout",
    "GatewayError",
    "LoopDetected",
    "NoBackendAvailable",
    "NotAccepted",
    "OutcomeUnknown",
    "PipelineFailed",
    "PipelineRejected",
    "PipelineRun",
    "PluginHost",
    "ResolvedAlternative",
    "ResolvedPipeline",
    "ResolvedStep",
    "default_outbound",
    "sanitize_error",
]
