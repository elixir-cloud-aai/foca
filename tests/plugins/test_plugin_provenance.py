"""Tests of provenance (`foca.plugins.provenance`, `PipelineRun.provenance`)."""

import json
import struct
import time
from typing import Any

import mongomock
import pytest

from foca.plugins import Backend, BackendResponse, ProvenanceStore
from foca.plugins.engine import ForwardTimeout, NoBackendAvailable, _changes
from foca.plugins.model import InboundRequest
from foca.plugins.provenance import (
    FAILURE_INDEXES,
    IMAGE_DIGEST_ENV,
    build_document,
    canonical_json,
    changed_paths,
    content_hash,
)
from tests.plugins.fakes import make_host
from tests.plugins.test_plugin_engine import BACKENDS, PRINCIPAL, inbound

# RFC 8785, appendix B: IEEE 754 doubles (hex) and their serialization
NUMBERS = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]


@pytest.mark.parametrize(("bits", "expected"), NUMBERS)
def test_numbers(bits: str, expected: str) -> None:
    """Numbers are serialized as in ECMAScript (RFC 8785 test vectors)."""
    (value,) = struct.unpack(">d", bytes.fromhex(bits))
    assert canonical_json(value) == expected


def test_canonical_json() -> None:
    """Objects, arrays, strings and literals follow RFC 8785 (section 3.2.2)."""
    data = json.loads(
        '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3,'
        " 0.000000000000000000000000001],"
        ' "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",'
        ' "literals": [null, true, false]}'
    )
    assert canonical_json(data) == (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )
    assert canonical_json((1, 10, -7)) == "[1,10,-7]"


def test_key_order() -> None:
    """Keys are sorted by UTF-16 code units (RFC 8785, section 3.2.3)."""
    keys = ["€", "\r", "דּ", "1", "\U0001f600", "\u0080", "ö"]
    text = canonical_json(dict.fromkeys(keys, 0))
    order = list(json.loads(text))
    assert order == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        ({1: "a"}, TypeError),
        (b"x", TypeError),
        ({"a": object()}, TypeError),
        ("\ud800", ValueError),
    ],
)
def test_not_json(value: Any, error: type[Exception]) -> None:
    """Values that are no JSON data cannot be hashed."""
    with pytest.raises(error):
        content_hash(value)


def test_content_hash() -> None:
    """Hashes ignore key order and number formatting."""
    assert content_hash({"a": 1, "b": 2.0}) == content_hash({"b": 2, "a": 1.0})
    assert content_hash({"a": 1}).startswith("sha256:")
    assert content_hash([1]) != content_hash([2])


def test_changed_paths() -> None:
    """Changes are listed as JSON Pointers, without values."""
    before = {"a": {"b": 1, "c": [1, 2, 3]}, "d": "x", "e~": 1, "f": 1}
    after = {"a": {"b": 2, "c": [1, 2]}, "d": "x", "e~": 2, "f": True, "g": 1}
    assert changed_paths(before, after) == ["/a/b", "/a/c/2", "/e~0", "/f", "/g"]
    assert changed_paths([1], [1, {"x": 1}]) == ["/1"]
    assert changed_paths({"a": 1}, [1], prefix="/p") == ["/p"]
    assert changed_paths({"a": [1]}, {"a": [1]}) == []


def test_changes_not_json() -> None:
    """Payloads that are no JSON data are recorded as such."""
    request = InboundRequest(method="POST", path="/", payload={"a": object()})
    assert _changes("request", request, request) == {"error": "not_json"}


def test_run_provenance() -> None:
    """Runs record the pipeline, the hook calls, routing and attempts."""
    host = make_host(
        [
            {"plugin": "test.fail", "on_failure": "skip"},
            {"plugin": "test.tag", "params": {"key": "k", "value": "v"}},
            {"plugin": "test.reverse"},
            {"plugin": "test.header", "params": {"key": "X-Hop", "value": "1"}},
        ],
        app_info={"name": "app", "version": "1.0"},
    )
    run = host.start(
        inbound(
            {
                "name": "task",
                "tags": {"gateway.backends": '{"type": "forbid", "backends": ["x"]}'},
            },
            headers={"X-Secret-Header": "secret-value"},
        ),
        PRINCIPAL,
        request_id="r1",
    )
    run.request()
    candidates = run.route(BACKENDS)
    attempts = run.attempts(candidates)
    next(attempts).failed("http_403")
    next(attempts).succeeded(BackendResponse(status=200), remote_id="t1")
    record = run.provenance()
    text = json.dumps(record)
    assert "secret-value" not in text
    assert "Bearer" not in text
    assert record["engine"]["app"] == {"name": "app", "version": "1.0"}
    assert record["engine"]["plugin_api"] == 1
    assert record["request_id"] == "r1"
    pipeline = record["pipeline"]
    assert pipeline["source"] == "default"
    assert pipeline["requested"] is None
    assert pipeline["resolved_hash"] == content_hash(pipeline["resolved"])
    assert sorted(record["plugins"].values()) == [
        "test.fail",
        "test.header",
        "test.reverse",
        "test.tag",
    ]
    executions = record["executions"]
    assert executions[0]["outcome"] == "error"
    assert "changes" not in executions[0]
    assert executions[1] == {
        "step": "test.fail",
        "phase": "request",
        "alternative": -1,
        "outcome": "skipped",
        "started_at": executions[1]["started_at"],
        "duration_ms": 0.0,
    }
    tag = executions[2]
    assert tag["plugin"] in record["plugins"]
    assert tag["plugin_id"] == "test.tag"
    assert tag["annotations"] == {"key": "k"}
    assert tag["changes"]["paths"] == ["/payload/tags/k"]
    assert tag["changes"]["before"] != tag["changes"]["after"]
    assert executions[3]["changes"]["paths"] == ["/0", "/2"]
    forward = [e for e in executions if e["phase"] == "forward"]
    assert [e["attempt"] for e in forward] == [1, 2]
    assert forward[0]["changes"]["paths"] == ["/headers/x-hop"]
    response = [e for e in executions if e["phase"] == "response"]
    assert response[0]["changes"]["paths"] == ["/client_headers/x-hop"]
    assert record["route"] == {
        "candidates_in": ["a", "b", "c"],
        "candidates_out": ["c", "b", "a"],
        "snapshots": {},
    }
    assert record["selection"] == {
        "type": "forbid",
        "backends": ["x"],
        "discarded": ["x"],
    }
    assert [(a["backend"], a["outcome"], a["reason"]) for a in record["attempts"]] == [
        ("c", "not_accepted", "http_403"),
        ("b", "accepted", None),
    ]
    assert record["attempts"][1]["remote_id"] == "t1"
    assert record["final_reason"] is None
    host.close()


def test_final_reason() -> None:
    """Runs record why forwarding failed."""
    host = make_host([])
    run = host.start(inbound(), PRINCIPAL)
    with pytest.raises(NoBackendAvailable):
        for attempt in run.attempts(BACKENDS[:1]):
            attempt.failed("connect_error")
    assert run.provenance()["final_reason"] == "no_backend"
    host = make_host([], defaults={"forward_timeout": 0.001})
    run = host.start(inbound(), PRINCIPAL)
    attempts = run.attempts([*BACKENDS, Backend(id="d", url="https://d.example.org")])
    with pytest.raises(ForwardTimeout):
        for attempt in attempts:
            attempt.failed("connect_error")
            time.sleep(0.01)
    assert run.provenance()["final_reason"] == "forward_timeout"


def test_client_pipeline_requested() -> None:
    """Client pipelines are recorded by reference to their tag."""
    host = make_host([], presets={"p": [{"plugin": "test.tag"}]})
    run = host.start(inbound({"tags": {"gateway.pipeline": "p"}}), PRINCIPAL)
    record = run.provenance()
    assert record["pipeline"]["source"] == "preset"
    assert record["pipeline"]["preset"] == "p"
    assert record["pipeline"]["requested"] == "tags[gateway.pipeline]"


def stores(record_failed: bool = False) -> tuple[ProvenanceStore, Any]:
    """Create a store on mongomock collections.

    Args:
        record_failed: Whether the store has a collection for failures.

    Returns:
        Store and database.
    """
    db: Any = mongomock.MongoClient().db
    store = ProvenanceStore(
        db.plugin_builds, db.pipelines, db.failures if record_failed else None
    )
    return store, db


def test_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Builds and admin-defined pipelines are stored once, by hash."""
    monkeypatch.setenv(IMAGE_DIGEST_ENV, "sha256:abc")
    store, db = stores()
    host = make_host([{"plugin": "test.tag"}])
    for _ in range(2):
        run = host.start(inbound(), PRINCIPAL)
        run.request()
        record = store.save(run)
    assert "resolved" not in record["pipeline"]
    assert db.pipelines.count_documents({}) == 1
    assert db.pipelines.find_one()["_id"] == record["pipeline"]["resolved_hash"]
    build = db.plugin_builds.find_one()
    assert db.plugin_builds.count_documents({}) == 1
    assert build["id"] == "test.tag"
    assert build["image_digest"] == "sha256:abc"
    assert build["_id"] in record["plugins"]
    assert build_document(host.plugins["test.tag"]) == build


def test_store_client_pipeline() -> None:
    """Client pipelines stay inline."""
    store, db = stores()
    host = make_host([])
    run = host.start(
        inbound({"tags": {"gateway.pipeline": '[{"plugin": "test.tag"}]'}}),
        PRINCIPAL,
    )
    record = store.save(run)
    assert record["pipeline"]["source"] == "client"
    assert record["pipeline"]["resolved"][0]["alternatives"][0]["plugin"] == "test.tag"
    assert db.pipelines.count_documents({}) == 0


@pytest.mark.parametrize(
    ("record_failed", "collection", "stored"),
    [(False, True, False), (True, False, False), (True, True, True)],
)
def test_save_failure(record_failed: bool, collection: bool, stored: bool) -> None:
    """Failed runs are stored only if configured."""
    store, db = stores(record_failed=collection)
    host = make_host(
        [{"plugin": "test.reject"}], provenance={"record_failed": record_failed}
    )
    run = host.start(inbound(), PRINCIPAL)
    with pytest.raises(Exception) as info:
        run.request()
    assert store.save_failure(run, info.value) is stored
    if stored:
        failure = db.failures.find_one()
        assert failure["error"]["type"] == "PipelineRejected"
        assert failure["error"]["slug"] == "rejected"
        assert failure["failed_at"] is not None
    assert FAILURE_INDEXES[0]["keys"] == {"failed_at": 1}
