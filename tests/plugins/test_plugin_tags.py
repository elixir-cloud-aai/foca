"""Tests of gateway tags (`foca.plugins.tags`)."""

import json

from foca.plugins import Backend, BackendResponse
from foca.plugins.tags import (
    MAX_ROUTE_SIZE,
    MAX_VALUE_SIZE,
    clean_url,
    creation_tags,
    routing_tags,
    strip_for_forwarding,
)
from tests.plugins.fakes import make_host
from tests.plugins.test_plugin_engine import BACKENDS, PRINCIPAL, inbound


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
        "gateway.plugins": "test.tag@1.2.0",
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


def test_creation_tags_limits() -> None:
    """Long plugin lists are cut to the value limit."""
    host = make_host(["test.tag"])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    loaded = host.plugins["test.tag"]
    object.__setattr__(loaded, "version", "9" * 2000)
    assert creation_tags(run)["gateway.plugins"] == ""
    object.__setattr__(loaded, "version", None)
    assert creation_tags(run)["gateway.plugins"] == "test.tag@unknown"


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
    try:
        for attempt in run.attempts(BACKENDS[:1]):
            attempt.failed("connect_error")
    except Exception:  # noqa: S110 (no backend)
        pass
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
    assert len(tags["gateway.backend_task_id"]) == MAX_VALUE_SIZE
