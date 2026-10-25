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
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from foca.plugins.base import NotApplicable, Rejected
from foca.plugins.checks import (
    _check_forward,
    _check_response,
    _check_route,
    _via_names,
    default_outbound,
)
from foca.plugins.config import (
    GATEWAY_ID_PATTERN,
    PluginsConfig,
    StepConfig,
    check_pipeline,
)
from foca.plugins.context import Context
from foca.plugins.errors import (
    PROBLEM_TYPE_BASE,
    ForwardTimeout,
    GatewayError,
    InvalidSelection,
    InvalidVia,
    LoopDetected,
    NoBackendAvailable,
    PipelineFailed,
    PipelineRejected,
)
from foca.plugins.executor import _Executor
from foca.plugins.loader import (
    LoadedPlugin,
    check_legacy_keys,
    close_plugins,
    load_plugins,
)
from foca.plugins.model import (
    Backend,
    BackendResponse,
    FrozenMapping,
    InboundRequest,
    OutboundRequest,
    Principal,
)
from foca.plugins.outcomes import (
    _REASON,
    NotAccepted,
    OutcomeUnknown,
    _AppError,
    _DeadlineExceededError,
    _InvalidOutputError,
    _NoEnabledPluginError,
    _StepFailedError,
    sanitize_error,
)
from foca.plugins.records import (
    AttemptOutcome,
    AttemptRecord,
    Execution,
    ResolvedAlternative,
    ResolvedPipeline,
    ResolvedStep,
    _utc_now,
)
from foca.plugins.selection import (
    PRESET_PATTERN,
    ClientSelection,
    apply_backend_selection,
    parse_client_pipeline,
    parse_client_tags,
)

logger = logging.getLogger(__name__)

MAX_VIA_LENGTH = 1024
"""Maximum length of outbound `Via` headers (the inbound header plus this
gateway's entry), in characters."""

_GATEWAY_ID = re.compile(GATEWAY_ID_PATTERN)


def _default_gateway_id(public_url: str | None) -> str | None:
    """Derive the name of the gateway in `Via` headers from its public URL.

    Args:
        public_url: Public URL of the app, if known.

    Returns:
        Host (IPv6 addresses in brackets) and port (if any) of the URL,
        lowercase, without user information; `None` if there is no URL, or
        the result does not match the pattern of `plugins.gateway_id`.
    """
    if not public_url:
        return None
    try:
        parts = urlsplit(public_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    name = f"{host}:{port}" if port is not None else host
    return name if _GATEWAY_ID.fullmatch(name) else None


# Default of `PluginHost.start(tags=...)`: the tags of the payload
_FROM_PAYLOAD = object()


class PluginHost:
    """Plugins of a process, and the factory of pipeline runs.

    Attributes:
        conf: Plugin configuration.
        api: API of the app, e.g., `tes`.
        plugins: Loaded plugins, by identifier.
        public_url: Public URL of the gateway, normalized, if known.
        gateway_id: Name of the gateway in `Via` headers, if known.
        default_pipeline: Resolved default pipeline.
        presets: Resolved presets, by name; presets that need a disabled
            plugin are left out.
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

        The app's callbacks (`validate`, `sanitize`, `validate_outbound`, and
        `build` of :py:meth:`PipelineRun.attempts`) are trusted code that
        runs outside the plugin deadlines, so they must be fast.

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
                pipeline is invalid; the plugins are closed then.
        """
        self.conf = conf
        self.api = api
        self._validate = validate
        self._sanitize = sanitize
        self._validate_outbound = validate_outbound
        self.public_url = public_url.rstrip("/") if public_url else None
        self.gateway_id = conf.gateway_id or _default_gateway_id(public_url)
        self.plugins: dict[str, LoadedPlugin] = (
            dict(plugins) if plugins is not None else load_plugins(conf, api=api)
        )
        self._executor = _Executor(conf.defaults.workers)
        try:
            self.default_pipeline = ResolvedPipeline(
                steps=self.resolve_steps(conf.pipeline, origin="pipeline")
            )
            frame = conf.client_pipelines
            self._prepend = self.resolve_steps(frame.prepend, origin="prepend")
            self._append = [
                (step.if_missing_phase, resolved)
                for step in frame.append
                for resolved in self.resolve_steps([step], origin="append")
            ]
            self.presets: dict[str, tuple[ResolvedStep, ...]] = {}
            for name, steps in conf.presets.items():
                try:
                    self.presets[name] = self.resolve_steps(steps, origin="preset")
                except _NoEnabledPluginError as exc:
                    logger.warning("Preset %s is unavailable: %s", name, exc)
        except BaseException:
            self.close()
            raise

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
                raise _NoEnabledPluginError(f"Step {step.id!r} has no enabled plugin")
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
        tags: object = _FROM_PAYLOAD,
    ) -> "PipelineRun":
        """Start the pipeline run of a request.

        The client's choices of pipeline and backends are read from the
        reserved tags (cf. :py:mod:`foca.plugins.selection`) and checked
        before any plugin runs.

        Args:
            inbound: Inbound request, after the app's checks and sanitation.
            principal: Authenticated caller.
            request_id: Identifier of the request, for logs.
            tags: Tags of the request (a string map), e.g., the parsed
                `tags` form field of WES; by default, the `tags` of the
                payload, if it is a mapping (e.g., a TES task).

        Returns:
            Run of the request.

        Raises:
            InvalidVia: The `Via` header of the request is so long that the
                outbound one, with this gateway's entry, would be longer than
                :py:data:`MAX_VIA_LENGTH` characters.
            LoopDetected: The request already passed this gateway.
            foca.plugins.selection.InvalidSelection: The client's choices are
                invalid or not allowed (`400`).
        """
        own = len(f", 1.1 {self.gateway_id}") if self.gateway_id is not None else 0
        if len(inbound.headers.get("Via", "")) > MAX_VIA_LENGTH - own:
            raise InvalidVia(f"Via header longer than {MAX_VIA_LENGTH} characters")
        if self.gateway_id is not None and self.gateway_id.lower() in _via_names(
            inbound.headers
        ):
            raise LoopDetected(f"Request already passed gateway {self.gateway_id}")
        if tags is _FROM_PAYLOAD:
            payload = inbound.payload
            tags = payload.get("tags") if isinstance(payload, Mapping) else None
        conf = self.conf
        selection = parse_client_tags(
            tags,
            prefix=conf.tags.prefix,
            max_pipeline_size=conf.client_pipelines.max_size,
            max_backends=conf.client_backends.max_items,
        )
        if selection.backends is not None and not conf.client_backends.enabled:
            raise InvalidSelection(
                "Choosing backends per request is disabled", slug="invalid-backends"
            )
        return PipelineRun(
            self,
            pipeline=self.resolve(selection.pipeline, principal),
            inbound=inbound,
            principal=principal,
            request_id=request_id,
            selection=selection,
        )

    def resolve(self, value: str | None, principal: Principal) -> ResolvedPipeline:
        """Resolve the pipeline of a request from the client's choice.

        Args:
            value: Value of the pipeline tag, if any.
            principal: Authenticated caller.

        Returns:
            Default pipeline without a choice; otherwise the preset or the
            client's pipeline, framed by `client_pipelines.prepend` and
            `append`.

        Raises:
            foca.plugins.selection.InvalidSelection: The choice is invalid
                or not allowed. Plugins that are unknown, not installed,
                disabled, not selectable or not allowed for the caller get
                the same message, so that clients cannot probe the setup.
        """
        if value is None:
            return self.default_pipeline
        conf = self.conf.client_pipelines
        if not conf.enabled:
            raise InvalidSelection("Choosing pipelines per request is disabled")
        if conf.roles and not set(conf.roles) & set(principal.groups):
            raise InvalidSelection("Choosing pipelines is not allowed")
        preset: str | None = None
        if PRESET_PATTERN.fullmatch(value) and "presets" in conf.allowed:
            if value not in self.presets:
                raise InvalidSelection(f"Unknown preset: {value}")
            preset = value
            resolved = self.presets[value]
        elif "inline" in conf.allowed:
            steps = parse_client_pipeline(value)
            self._check_client_steps(steps, principal)
            try:
                check_pipeline(steps)
                resolved = self.resolve_steps(steps, origin="client")
            except ValueError as exc:
                raise InvalidSelection(str(exc)) from exc
        else:
            raise InvalidSelection("Pipelines are chosen by preset name only")
        return ResolvedPipeline(
            steps=self._frame(resolved),
            source="preset" if preset is not None else "client",
            preset=preset,
        )

    def _check_client_steps(
        self, steps: Sequence[StepConfig], principal: Principal
    ) -> None:
        """Check the limits and plugins of a client's pipeline.

        Args:
            steps: Steps.
            principal: Authenticated caller.

        Raises:
            foca.plugins.selection.InvalidSelection: The pipeline exceeds the
                limits, or uses a plugin that the caller may not choose.
        """
        conf = self.conf.client_pipelines
        if len(steps) > conf.max_steps:
            raise InvalidSelection(f"At most {conf.max_steps} steps are allowed")
        for step in steps:
            if len(step.alternatives) > conf.max_alternatives:
                raise InvalidSelection(
                    f"At most {conf.max_alternatives} alternatives are allowed"
                )
            for alternative in step.alternatives:
                available = self.conf.available.get(alternative.plugin)
                if (
                    available is None
                    or alternative.plugin not in self.plugins
                    or not available.client_selectable
                    or (
                        available.roles
                        and not set(available.roles) & set(principal.groups)
                    )
                ):
                    raise InvalidSelection(
                        f"Plugin not available: {alternative.plugin}"
                    )

    def _frame(self, steps: tuple[ResolvedStep, ...]) -> tuple[ResolvedStep, ...]:
        """Frame the steps of a client's choice with the admin's steps.

        Args:
            steps: Resolved steps of the preset or client pipeline.

        Returns:
            `client_pipelines.prepend`, the steps and `client_pipelines.append`;
            appended steps with `if_missing_phase` only if no other step
            implements the phase.

        Raises:
            foca.plugins.selection.InvalidSelection: A step of the client
                has the identifier of a framing step (which are reserved).
        """
        conf = self.conf.client_pipelines
        reserved = {step.id for step in (*conf.prepend, *conf.append)}
        clashes = sorted(step.id for step in steps if step.id in reserved)
        if clashes:
            raise InvalidSelection(f"Step identifiers are reserved: {clashes[0]}")
        framed = [*self._prepend, *steps]
        for phase, step in self._append:
            if phase is not None and any(
                phase in self.plugins[alt.plugin].cls.phases()
                for existing in framed
                for alt in existing.alternatives
            ):
                continue
            framed.append(step)
        return tuple(framed)

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
        close_plugins(self.plugins.values())
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
        candidates_out: Identifiers of the candidates after it and the
            client's backend selection.
        selection: Client's choices, from the reserved tags.
        backends_discarded: Backends of the client's selection that were no
            candidates.
    """

    def __init__(
        self,
        host: PluginHost,
        *,
        pipeline: ResolvedPipeline,
        inbound: InboundRequest,
        principal: Principal,
        request_id: str | None,
        selection: ClientSelection | None = None,
    ) -> None:
        """Create the run.

        Args:
            host: Plugin host.
            pipeline: Resolved pipeline.
            inbound: Inbound request.
            principal: Authenticated caller.
            request_id: Identifier of the request.
            selection: Client's choices, from the reserved tags.
        """
        self.host = host
        self.pipeline = pipeline
        self.inbound = inbound
        self.principal = principal
        self.request_id = request_id
        self.task_id: str | None = None
        self.original = copy.deepcopy(inbound.payload)
        self._inbound_via = inbound.headers.get("Via")
        self.executions: list[Execution] = []
        self.attempt_records: list[AttemptRecord] = []
        self.candidates_in: list[str] = []
        self.candidates_out: list[str] = []
        self.selection = selection or ClientSelection()
        self.backends_discarded: list[str] = []
        self._states: dict[str, dict[str, Any]] = {}
        self._deadline = time.monotonic() + host.conf.defaults.pipeline_timeout

    def _set_via(self, outbound: OutboundRequest) -> None:
        """Set the `Via` header of an outbound request (RFC 9110, 7.6.3).

        The header is the inbound request's, passed through verbatim, with
        this gateway's entry appended (together at most
        :py:data:`MAX_VIA_LENGTH` characters), whatever plugins or the app's
        builder set, so that loops through several gateways are detected.

        Args:
            outbound: Outbound request.
        """
        entries = [self._inbound_via] if self._inbound_via else []
        if self.host.gateway_id is not None:
            entries.append(f"1.1 {self.host.gateway_id}")
        if entries:
            outbound.headers["Via"] = ", ".join(entries)
        else:
            outbound.headers.pop("Via", None)

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

        Backends at or below the gateway's public URL are removed first;
        the client's backend selection, if any, applies to the result (cf.
        :py:func:`foca.plugins.selection.apply_backend_selection`).

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
        if self.selection.backends is not None:
            routed, self.backends_discarded = apply_backend_selection(
                routed, self.selection.backends
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
                self._set_via(outbound)
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
        call_deadline = min(time.monotonic() + step.timeout, deadline)
        try:
            # copies per call, so that a failure fails this alternative only
            candidate = copy.deepcopy(value)
            state = copy.deepcopy(self._states.get(alternative.plugin, {}))
            ctx = Context(
                api=self.host.api,
                plugin_id=alternative.plugin,
                principal=replace(
                    self.principal,
                    claims=copy.deepcopy(FrozenMapping(self.principal.claims)),
                ),
                params=alternative.params.model_copy(deep=True),
                original=self.original,
                deadline=call_deadline,
                state=state,
                request_id=self.request_id,
                task_id=self.task_id,
            )
        except Exception as exc:
            execution.outcome = "error"
            execution.error = sanitize_error(exc)
            execution.duration_ms = round((time.monotonic() - started) * 1000, 3)
            logger.warning(
                "Plugin %s not called in step %s (%s): %s",
                alternative.plugin,
                step.id,
                phase,
                execution.error,
            )
            return execution, None
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
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException as exc:  # e.g., `SystemExit` of a hook
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


__all__ = [
    "MAX_VIA_LENGTH",
    "PROBLEM_TYPE_BASE",
    "Attempt",
    "AttemptRecord",
    "Execution",
    "ForwardTimeout",
    "GatewayError",
    "InvalidSelection",
    "InvalidVia",
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
