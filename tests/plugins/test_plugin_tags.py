"""Tests of gateway tags (`foca.plugins.tags`)."""

import json
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from foca.plugins import Backend, BackendResponse, NoBackendAvailable
from foca.plugins.config import PluginsConfig
from foca.plugins.tags import (
    MAX_ROUTE_SIZE,
    MAX_VALUE_SIZE,
    clean_url,
    creation_tags,
    routing_tags,
    strip_for_forwarding,
    without_server_tags,
)
from tests.plugins.fakes import TagPlugin, make_host
from tests.plugins.test_plugin_engine import BACKENDS, PRINCIPAL, inbound

BACKENDS_TAG = '{"type": "order", "backends": ["a"]}'


def test_strip_for_forwarding() -> None:
    """All gateway tags are removed from relayed requests."""
    tags = {"gateway.pipeline": "p", "gateway.backend": "x", "project": "a"}
    assert strip_for_forwarding(tags, "gateway.") == {"project": "a"}
    assert strip_for_forwarding(None, "gateway.") == {}


def test_clean_url() -> None:
    """URLs lose user info, query and fragment."""
    assert clean_url("https://u:p@a.example.org:8443/x/?q=1#f") == (
        "https://a.example.org:8443/x"
    )
    assert clean_url("https://[::1]/") == "https://[::1]"


def test_creation_tags() -> None:
    """New tasks get the pipeline hash, source and plugins."""
    host = make_host([{"plugin": "test.fail", "on_failure": "skip"}, "test.tag"])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    tags = creation_tags(run, provenance_path="/ga4gh/tes/v1/tasks/A/provenance")
    assert tags == {
        "gateway.pipeline_hash": run.provenance()["pipeline"]["resolved_hash"],
        "gateway.pipeline_source": "default",
        # plugins that ran, whatever the outcome
        "gateway.plugins": "test.fail@1.2.0,test.tag@1.2.0",
        "gateway.provenance": "/ga4gh/tes/v1/tasks/A/provenance",
    }
    host = make_host(
        [], presets={"p": ["test.tag", {"id": "again", "plugin": "test.tag"}]}
    )
    run = host.start(inbound({"tags": {"gateway.pipeline": "p"}}), PRINCIPAL)
    run.request()
    tags = creation_tags(run)
    assert tags["gateway.pipeline_source"] == "preset:p"
    assert tags["gateway.plugins"] == "test.tag@1.2.0"
    assert "gateway.provenance" not in tags


def test_creation_tags_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long plugin lists are cut to the value limit."""
    host = make_host(["test.tag"])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    loaded = host.plugins["test.tag"]
    monkeypatch.setitem(host.plugins, "test.tag", replace(loaded, version="9" * 2000))
    assert creation_tags(run)["gateway.plugins"] == ""  # the first does not fit
    monkeypatch.setitem(host.plugins, "test.tag", replace(loaded, version=None))
    assert creation_tags(run)["gateway.plugins"] == "test.tag@unknown"


def test_creation_tags_without_hash() -> None:
    """Pipelines without hash leave the hash tag out (string maps only)."""
    host = make_host([{"plugin": "test.tag", "params": {"value": "x"}}])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    original = run.provenance

    def provenance() -> dict[str, Any]:
        record = original()
        record["pipeline"]["resolved_hash"] = None
        return record

    run.provenance = provenance  # type: ignore[method-assign]
    tags = creation_tags(run)
    assert "gateway.pipeline_hash" not in tags
    assert all(isinstance(value, str) for value in tags.values())


def test_app_flow() -> None:
    """Apps merge the tags into tasks and strip them before forwarding."""
    host = make_host(["test.tag"])
    task: dict[str, Any] = {
        "name": "t",
        "tags": {"project": "a", "gateway.pipeline": "[]"},
    }
    run = host.start(inbound(task), PRINCIPAL)
    request = run.request()
    stored = dict(request.payload)
    prefix = host.conf.tags.prefix
    stored["tags"] = {
        **without_server_tags(stored.get("tags"), prefix),
        **creation_tags(run),
    }
    relayed = {
        **stored,
        "tags": strip_for_forwarding(stored["tags"], host.conf.tags.prefix),
    }
    assert relayed["tags"] == {"project": "a"}
    attempt = next(run.attempts(BACKENDS))
    attempt.succeeded(BackendResponse(status=200), remote_id="T1")
    stored["tags"].update(routing_tags(run))
    assert stored["tags"]["project"] == "a"
    assert stored["tags"]["gateway.backend_task_id"] == "T1"
    assert stored["tags"]["gateway.pipeline_source"] == "client"
    assert all(isinstance(value, str) for value in stored["tags"].values())


@pytest.mark.parametrize(
    ("before", "key", "value"),
    [
        ({}, "gateway.backend", "https://evil.example"),  # added
        ({}, "gateway.provenance", "/x"),
        ({"gateway.backends": BACKENDS_TAG}, "gateway.backends", "x"),  # changed
        ({"gateway.backends": BACKENDS_TAG}, "gateway.backends", None),  # removed
    ],
)
def test_plugins_cannot_forge_server_tags(
    before: dict[str, str], key: str, value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Request hooks that change tags under the prefix give invalid output."""

    def on_request(self: Any, request: Any, ctx: Any) -> None:
        tags = request.payload.setdefault("tags", {})
        if value is None:
            del tags[key]
        else:
            tags[key] = value
        tags["project"] = "b"  # other tags are fine

    monkeypatch.setattr(TagPlugin, "on_request", on_request)
    host = make_host([{"plugin": "test.tag", "on_failure": "skip"}])
    run = host.start(inbound({"tags": dict(before)}), PRINCIPAL)
    request = run.request()
    assert run.executions[0].outcome == "invalid_output"
    assert request.payload["tags"] == before


def test_other_tags_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Request hooks may change tags outside the prefix, or add the map."""

    def on_request(self: Any, request: Any, ctx: Any) -> None:
        request.payload["tags"] = {**(request.payload.get("tags") or {}), "a": "1"}

    monkeypatch.setattr(TagPlugin, "on_request", on_request)
    host = make_host([{"plugin": "test.tag"}])
    for task in ({}, {"tags": {"gateway.backends": BACKENDS_TAG}}, {"tags": None}):
        run = host.start(inbound(dict(task)), PRINCIPAL)
        assert run.request().payload["tags"]["a"] == "1"
        assert run.executions[0].outcome == "ok"


def test_without_server_tags() -> None:
    """The defensive merge keeps only the client's keys under the prefix."""
    tags = {
        "project": "a",
        "gateway.pipeline": "[]",
        "gateway.backends": "x",
        "gateway.backend": "https://evil.example",
    }
    assert without_server_tags(tags, "gateway.") == {
        "project": "a",
        "gateway.pipeline": "[]",
        "gateway.backends": "x",
    }
    assert without_server_tags(None, "gateway.") == {}


def test_routing_tags() -> None:
    """Forwarded tasks get the backend and the route."""
    host = make_host([])
    run = host.start(inbound(), PRINCIPAL)
    attempts = run.attempts(BACKENDS)
    next(attempts).failed("http_403")
    next(attempts).succeeded(BackendResponse(status=200), remote_id="T1")
    tags = routing_tags(run)
    assert tags["gateway.attempts"] == "2"
    assert tags["gateway.backend"] == "https://b.example.org"
    assert tags["gateway.backend_task_id"] == "T1"
    assert tags["gateway.forwarded_at"] == run.attempt_records[1].started_at
    assert json.loads(tags["gateway.route"]) == [
        {
            "url": "https://a.example.org",
            "outcome": "not_accepted",
            "reason": "http_403",
        },
        {"url": "https://b.example.org", "outcome": "accepted"},
    ]
    assert "gateway.route_truncated" not in tags
    assert all(isinstance(value, str) for value in tags.values())


def test_routing_tags_unknown() -> None:
    """Unknown outcomes name the backend, without task identifier."""
    host = make_host([])
    run = host.start(inbound(), PRINCIPAL)
    next(run.attempts(BACKENDS)).unknown("read_timeout")
    tags = routing_tags(run)
    assert tags["gateway.backend"] == "https://a.example.org"
    assert "gateway.backend_task_id" not in tags


def test_routing_tags_none_accepted() -> None:
    """Without acceptance, only the attempts are recorded."""
    host = make_host([])
    run = host.start(inbound(), PRINCIPAL)
    with pytest.raises(NoBackendAvailable):
        for attempt in run.attempts(BACKENDS[:1]):
            attempt.failed("connect_error")
    tags = routing_tags(run)
    assert "gateway.backend" not in tags
    assert tags["gateway.attempts"] == "1"


def test_route_truncated() -> None:
    """Routes list the last attempts within the size limit."""
    backends = [
        Backend(id=f"b{i}", url=f"https://{'x' * 200}{i}.example.org")
        for i in range(30)
    ]
    host = make_host(
        [], tags={"max_route_entries": 25}, defaults={"forward_timeout": 60}
    )
    run = host.start(inbound(), PRINCIPAL)
    attempts = run.attempts(backends)
    for _ in range(29):
        next(attempts).failed("connect_error")
    next(attempts).succeeded(BackendResponse(status=201), remote_id="x" * 2000)
    tags = routing_tags(run)
    route = json.loads(tags["gateway.route"])
    assert len(tags["gateway.route"].encode()) <= MAX_ROUTE_SIZE
    assert route[-1]["outcome"] == "accepted"
    assert len(route) < 25
    assert tags["gateway.route_truncated"] == "true"
    assert "gateway.backend_task_id" not in tags  # too long: left out


def test_route_keeps_latest() -> None:
    """A latest attempt too large for the route is kept, with a cut URL."""
    backend = Backend(id="b", url=f"https://{'x' * 60}.example.org/{'p' * 5000}")
    host = make_host([])
    run = host.start(inbound(), PRINCIPAL)
    next(run.attempts([backend])).succeeded(BackendResponse(status=201))
    tags = routing_tags(run)
    route = json.loads(tags["gateway.route"])
    assert len(tags["gateway.route"].encode()) <= MAX_ROUTE_SIZE
    assert [entry["outcome"] for entry in route] == ["accepted"]
    assert route[0]["url"].startswith("https://x")
    assert tags["gateway.route_truncated"] == "true"


def test_tags_of_malformed_text() -> None:
    """Lone surrogates and invalid ports do not break the tags."""
    assert clean_url("https://a.example.org:port/x") == "https://"
    host = make_host(["test.tag"])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    tags = creation_tags(run, provenance_path="/tasks/\ud800/provenance")
    assert tags["gateway.provenance"] == "/tasks/?/provenance"
    tags["gateway.provenance"].encode("utf-8")
    run = host.start(inbound(), PRINCIPAL)
    backend = Backend(id="b", url="https://b.example.org")
    next(run.attempts([backend])).succeeded(
        BackendResponse(status=201), remote_id="T\udc80"
    )
    assert routing_tags(run)["gateway.backend_task_id"] == "T?"
    host.close()


def test_long_provenance_path_left_out() -> None:
    """Provenance paths beyond the value limit are left out, not cut."""
    host = make_host(["test.tag"])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    tags = creation_tags(run, provenance_path="/" + "p" * MAX_VALUE_SIZE)
    assert "gateway.provenance" not in tags
    host.close()


def test_route_truncated_by_count() -> None:
    """Routes list the last `max_route_entries` attempts."""
    host = make_host([], tags={"max_route_entries": 2})
    run = host.start(inbound(), PRINCIPAL)
    attempts = run.attempts(BACKENDS)
    next(attempts).failed("http_403")
    next(attempts).failed("http_403")
    next(attempts).succeeded(BackendResponse(status=200), remote_id=42)  # type: ignore[arg-type]
    tags = routing_tags(run)
    route = json.loads(tags["gateway.route"])
    assert [entry["url"] for entry in route] == [
        "https://b.example.org",
        "https://c.example.org",
    ]
    assert tags["gateway.route_truncated"] == "true"
    assert tags["gateway.attempts"] == "3"
    assert tags["gateway.backend_task_id"] == "42"


def test_max_route_entries_validated() -> None:
    """At least one attempt is listed."""
    with pytest.raises(ValidationError, match="max_route_entries"):
        PluginsConfig.model_validate({"tags": {"max_route_entries": 0}})
