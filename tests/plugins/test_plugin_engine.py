"""Tests of the pipeline engine (`foca.plugins.engine`)."""

import contextlib
import json
import threading
import time
from typing import Any

import pytest

from foca.plugins import (
    Backend,
    BackendResponse,
    Headers,
    InboundRequest,
    Principal,
    Rejected,
)
from foca.plugins.checks import _check_forward, _check_route, default_outbound
from foca.plugins.config import PluginsConfig
from foca.plugins.engine import (
    ForwardTimeout,
    InvalidVia,
    LoopDetected,
    NoBackendAvailable,
    PipelineFailed,
    PipelineRejected,
    PluginHost,
    _default_gateway_id,
)
from foca.plugins.errors import GatewayError
from foca.plugins.executor import _Executor
from foca.plugins.model import OutboundRequest
from foca.plugins.outcomes import describe_error, sanitize_error
from foca.utils.http import GuardedClient
from tests.plugins.fakes import (
    SlowPlugin,
    TagPlugin,
    fake_entry_points,
    make_host,
    wait_for,
)

BACKENDS = [
    Backend(id="a", url="https://a.example.org"),
    Backend(id="b", url="https://b.example.org", source="discovered", group="reg"),
    Backend(id="c", url="https://c.example.org"),
]


def inbound(
    payload: Any = None, headers: dict[str, str] | None = None
) -> InboundRequest:
    """Build an inbound request.

    Args:
        payload: Payload; defaults to a task.
        headers: Headers.

    Returns:
        Request.
    """
    return InboundRequest(
        method="POST",
        path="/ga4gh/tes/v1/tasks",
        payload={"name": "task"} if payload is None else payload,
        headers=Headers(headers or {}),
    )


PRINCIPAL = Principal(subject="alice")


def test_request_phase_commits_successful_alternative() -> None:
    """The first successful alternative's copy is kept."""
    host = make_host(
        [
            {
                "id": "tag",
                "alternatives": [
                    {"plugin": "test.fail"},
                    {"plugin": "test.skip"},
                    {"plugin": "test.tag", "params": {"value": "v"}},
                ],
            }
        ]
    )
    run = host.start(inbound(), PRINCIPAL, request_id="r1")
    result = run.request()
    assert result.payload == {"name": "task", "tags": {"tagged": "v"}}
    assert run.original == {"name": "task"}
    outcomes = [(e.plugin, e.outcome) for e in run.executions]
    assert outcomes == [
        ("test.fail", "error"),
        ("test.skip", "not_applicable"),
        ("test.tag", "ok"),
    ]
    assert run.executions[0].error is not None
    assert "secret" not in run.executions[0].error
    assert run.executions[2].annotations == {"key": "tagged"}
    changes = run.executions[2].changes
    assert changes is not None
    assert changes["paths"] == ["/payload/tags"]
    assert run.implements("request")
    assert not run.implements("route")
    host.close()


def test_failed_step_fails_request() -> None:
    """A required step without success fails the request (500)."""
    host = make_host([{"plugin": "test.fail"}])
    run = host.start(inbound(), PRINCIPAL)
    with pytest.raises(PipelineFailed) as exc_info:
        run.request()
    assert exc_info.value.code == 500
    assert exc_info.value.step == "test.fail"
    assert exc_info.value.run is run
    assert exc_info.value.problem_type.endswith("#plugin-step-failed")
    assert run.inbound.payload == {"name": "task"}


def test_skipped_step_keeps_payload() -> None:
    """A step with `on_failure: skip` is skipped when all alternatives fail."""
    host = make_host([{"plugin": "test.fail", "on_failure": "skip"}])
    run = host.start(inbound(), PRINCIPAL)
    assert run.request().payload == {"name": "task"}
    assert [e.outcome for e in run.executions] == ["error", "skipped"]


def test_rejection_stops_at_once() -> None:
    """A rejection stops the request without trying other alternatives."""
    host = make_host(
        [
            {
                "id": "s",
                "alternatives": [{"plugin": "test.reject"}, {"plugin": "test.tag"}],
            }
        ]
    )
    run = host.start(inbound(), PRINCIPAL)
    with pytest.raises(PipelineRejected) as exc_info:
        run.request()
    assert exc_info.value.code == 403
    assert exc_info.value.title == "Forbidden"
    assert exc_info.value.detail == "not allowed"
    assert exc_info.value.step == "s"
    assert [e.outcome for e in run.executions] == ["rejected"]


def test_invalid_output_tries_next_alternative(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Output rejected by the app's check counts as invalid."""

    def validate(payload: Any) -> None:
        if "tags" in payload:
            raise ValueError("no tags")

    host = make_host(
        [
            {
                "id": "s",
                "alternatives": [{"plugin": "test.tag"}],
                "on_failure": "skip",
            }
        ],
        validate=validate,
        sanitize=lambda payload: payload,
    )
    run = host.start(inbound(), PRINCIPAL)
    assert run.request().payload == {"name": "task"}
    assert run.executions[0].outcome == "invalid_output"
    # messages of other code go to logs only
    assert run.executions[0].error == "ValueError"
    assert "ValueError: no tags" in caplog.text


def test_timeout_abandons_hook() -> None:
    """Hooks are abandoned at their deadline; their changes are discarded."""
    SlowPlugin.release.clear()
    host = make_host([{"plugin": "test.slow", "timeout": 0.05, "on_failure": "skip"}])
    run = host.start(inbound(), PRINCIPAL)
    assert run.request().payload == {"name": "task"}
    assert run.executions[0].outcome == "timeout"
    SlowPlugin.release.set()
    host.close()


def test_pipeline_deadline_passed() -> None:
    """Calls after the pipeline deadline time out at once."""
    host = make_host(
        [{"plugin": "test.tag", "on_failure": "skip"}],
        defaults={"pipeline_timeout": 0.001},
    )
    run = host.start(inbound(), PRINCIPAL)
    time.sleep(0.01)
    run.request()
    assert run.executions[0].outcome == "timeout"
    assert "deadline passed" in (run.executions[0].error or "")


def test_executor_saturation_and_errors() -> None:
    """Calls fail fast when all workers are busy; hook errors propagate."""
    executor = _Executor(1)
    release = threading.Event()
    started = threading.Event()

    def block() -> None:
        started.set()
        release.wait(5)

    thread = threading.Thread(target=lambda: _ignore(executor, block))
    thread.start()
    assert started.wait(5)
    with pytest.raises(Exception, match="busy"):
        executor.call(lambda: None, 1)
    release.set()
    thread.join()
    assert wait_for(lambda: _free(executor))

    def raise_timeout() -> None:
        raise TimeoutError("own")

    with pytest.raises(TimeoutError, match="own"):
        executor.call(raise_timeout, 1)
    executor.shutdown()
    with pytest.raises(RuntimeError):
        executor.call(lambda: None, 1)
    assert _free(executor)


def _ignore(executor: _Executor, function: Any) -> None:
    """Call a function in the executor, ignoring errors.

    Args:
        executor: Executor.
        function: Function.
    """
    with contextlib.suppress(Exception):
        executor.call(function, 10)


def _free(executor: _Executor) -> bool:
    """Check that the executor's slot is free.

    Args:
        executor: Executor with one worker.

    Returns:
        Whether a slot could be taken (and is released again).
    """
    if executor._slots.acquire(blocking=False):
        executor._slots.release()
        return True
    return False


def test_state_persists_across_phases() -> None:
    """Plugin state of successful calls is kept for later phases."""
    host = make_host(
        [{"plugin": "test.header", "params": {"key": "X-A", "value": "1"}}]
    )
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    candidates = run.route(BACKENDS)
    attempts = []
    for attempt in run.attempts(candidates):
        attempts.append(attempt)
        assert attempt.outbound.headers["X-A"] == "1"
        if attempt.number == 1:
            attempt.failed("http_403")
            continue
        response = attempt.succeeded(
            BackendResponse(status=200, payload={"id": "remote"}, client_payload={}),
            remote_id="remote",
        )
        assert response.client_headers["X-A"] == "1"
        break
    assert run._states["test.header"]["forwards"] == 2
    assert [(r.backend, r.outcome, r.reason) for r in run.attempt_records] == [
        ("a", "not_accepted", "http_403"),
        ("b", "accepted", None),
    ]
    assert run.attempt_records[1].remote_id == "remote"


def test_route_phase() -> None:
    """Route hooks reorder candidates; own URLs are removed first."""
    host = make_host([{"plugin": "test.reverse"}], public_url="https://c.example.org/")
    run = host.start(inbound(), PRINCIPAL)
    assert [b.id for b in run.route(BACKENDS)] == ["b", "a"]
    assert run.candidates_in == ["a", "b", "c"]
    assert run.candidates_out == ["b", "a"]


def test_route_invalid_output() -> None:
    """Route hooks may not return unknown backends."""
    host = make_host([{"plugin": "test.bad-router"}])
    run = host.start(inbound(), PRINCIPAL)
    with pytest.raises(PipelineFailed):
        run.route(BACKENDS)
    assert run.executions[0].outcome == "invalid_output"


def test_check_route() -> None:
    """Route output must be a list of unchanged candidates without duplicates."""
    with pytest.raises(TypeError):
        _check_route(("a",), BACKENDS)
    with pytest.raises(ValueError, match="twice"):
        _check_route([BACKENDS[0], BACKENDS[0]], BACKENDS)
    changed = Backend(id="a", url="https://other.example.org")
    with pytest.raises(ValueError, match="unchanged"):
        _check_route([changed], BACKENDS)
    copies = [
        Backend(id="b", url="https://b.example.org", source="discovered", group="reg")
    ]
    assert _check_route(copies, BACKENDS)[0] is BACKENDS[1]


def test_forward_failures_and_via() -> None:
    """Failing forward steps skip the backend; Via headers are added."""
    host = make_host([{"plugin": "test.redirect"}], gateway_id="gw.example.org")
    run = host.start(inbound(), PRINCIPAL)
    with pytest.raises(NoBackendAvailable) as exc_info:
        list(run.attempts(BACKENDS[:2]))
    assert exc_info.value.code == 503
    assert [r.reason for r in run.attempt_records] == ["forward_plugin_failed"] * 2
    assert run.executions[0].outcome == "invalid_output"

    host = make_host([], gateway_id="gw.example.org")
    run = host.start(inbound(headers={"Via": "1.1 other"}), PRINCIPAL)
    run.inbound.headers["Via"] = "1.1 other"
    attempts = run.attempts(BACKENDS, build=_with_via)
    attempt = next(attempts)
    assert attempt.outbound.headers["Via"] == "1.1 other, 1.1 gw.example.org"
    attempt.unknown("read_timeout")
    with pytest.raises(StopIteration):
        next(attempts)
    assert run.final_reason == "outcome_unknown"
    assert run.attempt_records[0].outcome == "unknown"


def _with_via(backend: Backend, request: InboundRequest) -> OutboundRequest:
    """Build an outbound request that keeps the inbound `Via` header.

    Args:
        backend: Backend.
        request: Inbound request.

    Returns:
        Outbound request.
    """
    outbound = default_outbound(backend, request)
    outbound.headers["Via"] = request.headers["Via"]
    return outbound


def test_attempt_reporting() -> None:
    """Outcomes must be reported once, with valid reasons."""
    host = make_host([])
    run = host.start(inbound(), PRINCIPAL)
    attempts = run.attempts(BACKENDS)
    attempt = next(attempts)
    assert 0 < attempt.remaining() <= 60
    with pytest.raises(ValueError, match="Reason"):
        attempt.failed("Bad Reason")
    attempt.failed("connect_error")
    with pytest.raises(RuntimeError, match="already"):
        attempt.failed("connect_error")
    next(attempts)
    with pytest.raises(RuntimeError, match="Report"):
        next(attempts)


def test_attempts_skip_own_url_and_time_out() -> None:
    """Own URLs are never attempted; the forward budget is enforced."""
    host = make_host(
        [], public_url="https://a.example.org", defaults={"forward_timeout": 0.05}
    )
    run = host.start(inbound(), PRINCIPAL)
    attempts = run.attempts(BACKENDS)
    attempt = next(attempts)
    assert attempt.backend.id == "b"
    time.sleep(0.06)
    attempt.failed("connect_error")
    with pytest.raises(ForwardTimeout) as exc_info:
        next(attempts)
    assert exc_info.value.code == 503
    assert exc_info.value.run is run


def test_response_phase() -> None:
    """Response hooks run in reverse; failing steps keep the response."""
    host = make_host(
        [
            {"plugin": "test.reject"},
            {"plugin": "test.redirect", "on_failure": "skip"},
            {"plugin": "test.header", "params": {"key": "X-R", "value": "r"}},
        ]
    )
    run = host.start(inbound(), PRINCIPAL)
    response = BackendResponse(status=200)
    result = run.respond(response, attempt=1)
    assert result.client_headers["X-R"] == "r"
    phases = [(e.plugin, e.outcome) for e in run.executions]
    assert phases == [
        ("test.header", "ok"),
        ("test.redirect", "invalid_output"),
        (None, "skipped"),
        ("test.reject", "rejected"),
        (None, "skipped"),
    ]
    assert result is not response


def test_check_forward() -> None:
    """Forward output must keep the backend and have a valid path."""
    given = default_outbound(BACKENDS[0], inbound())
    changes: list[tuple[dict[str, Any], type[Exception]]] = [
        ({"headers": {}}, TypeError),
        ({"method": "P OST"}, ValueError),
        ({"path": "//evil.example.org"}, ValueError),
        ({"path": "/x?y"}, ValueError),
        ({"path": "/x y"}, ValueError),
    ]
    for change, error in changes:
        candidate = default_outbound(BACKENDS[0], inbound())
        for key, value in change.items():
            setattr(candidate, key, value)
        with pytest.raises(error):
            _check_forward(candidate, given)
    candidate = default_outbound(BACKENDS[0], inbound())
    candidate.method = "post"
    assert _check_forward(candidate, given).method == "POST"


def test_loop_detection() -> None:
    """Requests that passed this gateway are refused."""
    host = make_host([], public_url="https://gw.example.org/protes")
    assert host.gateway_id == "gw.example.org"
    with pytest.raises(LoopDetected) as exc_info:
        host.start(
            inbound(headers={"Via": "1.0 proxy, 1.1 gw.example.org (x)"}), PRINCIPAL
        )
    assert exc_info.value.code == 508
    host.start(inbound(headers={"Via": "garbage"}), PRINCIPAL)
    with pytest.raises(InvalidVia) as via_info:
        host.start(inbound(headers={"Via": "1.1 p, " * 200}), PRINCIPAL)
    assert via_info.value.code == 400
    longest = "x" * (1024 - len(", 1.1 gw.example.org"))
    run = host.start(inbound(headers={"Via": longest}), PRINCIPAL)
    assert len(next(run.attempts(BACKENDS)).outbound.headers["Via"]) == 1024
    with pytest.raises(InvalidVia):
        host.start(inbound(headers={"Via": longest + "x"}), PRINCIPAL)
    assert host.is_own_url("https://gw.example.org/protes/")
    assert host.is_own_url("https://gw.example.org/protes/x")
    assert not host.is_own_url("https://gw.example.org/protest")
    assert not make_host([]).is_own_url("https://gw.example.org")


def test_resolve_steps() -> None:
    """Disabled plugins are dropped; params are validated."""
    available = {"test.tag": {}, "test.fail": {"enabled": False}}
    host = make_host(
        [
            {
                "id": "a",
                "alternatives": [{"plugin": "test.fail"}, {"plugin": "test.tag"}],
            },
            {"plugin": "test.fail", "on_failure": "skip"},
        ],
        available=available,
    )
    assert [step.id for step in host.default_pipeline.steps] == ["a"]
    assert host.default_pipeline.describe() == [
        {
            "id": "a",
            "origin": "pipeline",
            "alternatives": [
                {"plugin": "test.tag", "params": {"key": "tagged", "value": "yes"}}
            ],
            "timeout": 10,
            "on_failure": "fail",
        }
    ]
    with pytest.raises(ValueError, match="no enabled plugin"):
        make_host([{"plugin": "test.fail"}], available=available)
    with pytest.raises(ValueError, match="Invalid params"):
        make_host([{"plugin": "test.tag", "params": {"other": 1}}], available=available)
    from foca.plugins.config import StepConfig  # noqa: PLC0415

    with pytest.raises(ValueError, match="not available"):
        host.resolve_steps(
            [StepConfig.model_validate({"plugin": "test.other"})], origin="client"
        )


def test_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts are created from the FOCA configuration."""
    from foca.models.config import Config  # noqa: PLC0415
    from foca.plugins import loader  # noqa: PLC0415

    monkeypatch.setattr(loader, "entry_points", lambda group: fake_entry_points()())
    config = Config.model_validate(
        {
            "server": {"public_url": "https://gw.example.org"},
            "plugins": {
                "available": {"test.tag": {}},
                "pipeline": [{"plugin": "test.tag"}],
            },
        }
    )
    host = PluginHost.from_config(config, api="tes")
    assert list(host.plugins) == ["test.tag", "foca.routing.ordered"]
    assert host.public_url == "https://gw.example.org"
    assert list(PluginHost.from_config(Config(), api="tes").plugins) == [
        "foca.routing.ordered"
    ]
    with pytest.raises(ValueError, match="middlewares"):
        PluginHost.from_config(
            Config.model_validate({"custom": {"middlewares": [["x"]]}}), api="tes"
        )


def test_close_logs_errors(caplog: pytest.LogCaptureFixture) -> None:
    """Errors of plugins on closing are logged."""
    host = make_host([])

    def fail() -> None:
        raise RuntimeError("close")

    host.plugins["test.tag"].instance.close = fail  # type: ignore[method-assign]
    host.close()
    assert "failed to close" in caplog.text


def test_sanitize_error() -> None:
    """Records get types of errors only; logs get sanitized messages."""
    error = ValueError("see http://u:p@example.org/?token=abc " + "a" * 600)
    assert sanitize_error(error) == "ValueError"
    message = describe_error(error)
    assert message.startswith("ValueError: see http://example.org/?token")
    assert len(message) == 500
    assert sanitize_error(GatewayError("bad http://u:p@x.example.org/")) == (
        "GatewayError: bad http://x.example.org/"
    )
    assert sanitize_error(Rejected("no", 403)).startswith("Rejected")


def test_plugin_messages_not_recorded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Messages of plugin errors, e.g., with tokens, are logged only."""

    def on_request(self: Any, request: Any, ctx: Any) -> None:
        raise RuntimeError("token=s3cr3t")

    monkeypatch.setattr(TagPlugin, "on_request", on_request)
    host = make_host([{"plugin": "test.tag", "on_failure": "skip"}])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    assert run.executions[0].error == "RuntimeError"
    assert "s3cr3t" not in json.dumps(run.provenance())
    assert "RuntimeError: token=s3cr3t" in caplog.text


def test_config_default_host() -> None:
    """Hosts work without plugins."""
    host = PluginHost(PluginsConfig(), api="tes")
    run = host.start(inbound(), PRINCIPAL)
    assert run.request().payload == {"name": "task"}
    assert run.route(BACKENDS) == BACKENDS


def test_context_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks get a guarded HTTP client bounded by their deadline."""
    clients: list[Any] = []

    def on_request(self: Any, request: InboundRequest, ctx: Any) -> None:
        clients.append(ctx.http)
        assert ctx.http.session is not None
        assert 0 < ctx.http.remaining() <= 3
        assert abs(ctx.http.deadline - ctx.deadline) < 1e-6

    monkeypatch.setattr(TagPlugin, "on_request", on_request)
    host = make_host(
        [{"plugin": "test.tag", "timeout": 3}],
        defaults={"http": {"allow_http": True, "max_response_size": 1024}},
    )
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    assert run.executions[0].outcome == "ok"
    client = clients[0]
    assert isinstance(client, GuardedClient)
    assert client.allow_http
    assert not client.allow_private_addresses
    assert client.max_response_size == 1024
    assert client.timeout == 3
    assert client._session is None  # closed after the call
    host.close()


@pytest.mark.parametrize(
    ("public_url", "expected"),
    [
        ("https://user:secret@GW.example.org:8443/x", "gw.example.org:8443"),
        ("https://gw.example.org", "gw.example.org"),
        ("https://[::1]:80/", "[::1]:80"),
        ("https://gw.example.org:bad/", None),
        ("file:///x", None),
        ("https://gw_x%2F.example.org/", None),
        (None, None),
    ],
)
def test_default_gateway_id(public_url: str | None, expected: str | None) -> None:
    """The default name is the host and port of the public URL, if valid."""
    assert _default_gateway_id(public_url) == expected


def test_outbound_check_sees_via() -> None:
    """The app's outbound check gets the request with its `Via` header."""
    seen: list[str | None] = []
    host = make_host(
        [],
        gateway_id="gw.example.org",
        validate_outbound=lambda outbound: seen.append(outbound.headers.get("Via")),
    )
    run = host.start(inbound(), PRINCIPAL)
    next(run.attempts(BACKENDS)).failed("connect_error")
    assert seen == ["1.1 gw.example.org"]


def test_host_closes_plugins_on_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts that cannot start close the plugins they loaded."""
    closed: list[str] = []
    monkeypatch.setattr(
        SlowPlugin, "close", lambda self: closed.append(self.id), raising=False
    )
    with pytest.raises(ValueError, match=r"test\.tag"):
        make_host(
            [{"plugin": "test.tag"}],
            available={"test.slow": {}, "test.tag": {"enabled": False}},
        )
    assert closed == ["test.slow"]
