"""Tests of provenance (`foca.plugins.provenance`, `PipelineRun.provenance`)."""

import json
import struct
import time
from collections.abc import Iterator
from typing import Any

import mongomock
import pytest
from pydantic import BaseModel, SecretStr

from foca.plugins import Backend, BackendResponse, ProvenanceStore, engine, provenance
from foca.plugins.engine import ForwardTimeout, NoBackendAvailable
from foca.plugins.model import Headers, InboundRequest, OutboundRequest
from foca.plugins.provenance import (
    FAILURE_INDEXES,
    IMAGE_DIGEST_ENV,
    MAX_CHANGED_PATHS,
    build_document,
    build_key,
    canonical_json,
    change_hash,
    changed_paths,
    clean_url,
    content_hash,
    describe_changes,
    limit_record,
)
from foca.plugins.records import ResolvedAlternative, ResolvedPipeline, ResolvedStep
from foca.plugins.selection import BackendSelection, ClientSelection
from tests.plugins.fakes import HeaderPlugin, TagPlugin, make_host
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
    assert changed_paths(before, after) == ["/a/b", "/a/c", "/e~0", "/f", "/g"]
    assert changed_paths([1], [1, {"x": 1}]) == [""]
    assert changed_paths([1, [2]], [1, [3]]) == ["/1/0"]
    assert changed_paths({"a": 1}, [1], prefix="/p") == ["/p"]
    assert changed_paths({"a": [1]}, {"a": [1]}) == []


@pytest.mark.parametrize("payload", [{"a": object()}, {"a": 10**400}, {"a": 2**53}])
def test_changes_not_json(payload: Any) -> None:
    """Payloads that are no I-JSON are recorded as such, without errors."""
    request = InboundRequest(method="POST", path="/", payload=payload)
    assert describe_changes("request", request, request) == {"error": "not_json"}
    with pytest.raises(ValueError, match="I-JSON"):
        canonical_json(-(2**53))
    assert canonical_json(2**53 - 1) == "9007199254740991"


def test_huge_integers_in_runs() -> None:
    """Huge integers in payloads do not fail runs."""
    host = make_host([{"plugin": "test.tag"}])
    run = host.start(inbound({"n": 10**400}), PRINCIPAL)
    run.request()
    assert run.executions[0].outcome == "ok"
    assert run.provenance()["executions"][0]["changes"] == {"error": "not_json"}


def test_changes_truncated() -> None:
    """Calls record a bounded number of changed paths."""
    before = InboundRequest(method="POST", path="/", payload={})
    after = InboundRequest(
        method="POST", path="/", payload={f"k{i}": i for i in range(300)}
    )
    changes = describe_changes("request", before, after)
    assert len(changes["paths"]) == MAX_CHANGED_PATHS
    assert changes["truncated"] is True
    assert "truncated" not in describe_changes("request", before, before)


def test_changes_without_credentials() -> None:
    """Credentials and secret headers are not part of the hashes."""
    backend = Backend(id="a", url="https://a.example.org")

    def outbound(headers: dict[str, str], auth: str | None) -> OutboundRequest:
        return OutboundRequest(
            backend=backend,
            method="POST",
            path="/",
            headers=Headers(headers),
            auth=auth,
        )

    before = outbound({"X-Key": "1", "Accept": "a"}, None)
    after = outbound(
        {"Authorization": "Bearer t", "X-Key": "2", "Accept": "a"}, "basic"
    )
    changes = describe_changes("forward", before, after, secret_headers=["X-Key"])
    assert changes["paths"] == []
    assert changes["before"] == changes["after"]
    assert describe_changes("forward", before, after)["paths"] == ["/headers/x-key"]


def test_resolved_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Records of pipelines that are no I-JSON have no hash, but no error."""
    host = make_host([{"plugin": "test.tag"}])
    run = host.start(inbound(), PRINCIPAL)
    monkeypatch.setattr(ResolvedPipeline, "describe", lambda self: [{"x": 2**60}])
    pipeline = run.provenance()["pipeline"]
    assert pipeline["resolved_hash"] is None
    assert pipeline["resolved"] is None
    assert pipeline["error"] == "not_json"
    store, db = stores()
    record = store.save(run)
    assert record["pipeline"]["resolved"] is None
    assert "stored" not in record
    assert db.pipelines.count_documents({}) == 0


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
        "annotations": {},
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
    # the budget ran out during the last attempt
    host = make_host([], defaults={"forward_timeout": 0.001})
    run = host.start(inbound(), PRINCIPAL)
    with pytest.raises(ForwardTimeout):
        for attempt in run.attempts(BACKENDS[:1]):
            time.sleep(0.01)
            attempt.failed("connect_error")
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


def test_builds_per_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """New images give new build documents."""
    store, db = stores()
    host = make_host([{"plugin": "test.tag"}])
    keys = []
    for digest in ("sha256:old", "sha256:new"):
        monkeypatch.setenv(IMAGE_DIGEST_ENV, digest)
        keys.append(build_key(host.plugins["test.tag"]))
        run = host.start(inbound(), PRINCIPAL)
        run.request()
        store.save(run)
    assert keys[0] != keys[1]
    digests = {build["image_digest"] for build in db.plugin_builds.find()}
    assert digests == {"sha256:old", "sha256:new"}


class Broken:
    """Collection whose writes fail."""

    def update_one(self, *args: Any, **kwargs: Any) -> None:
        """Fail.

        Args:
            args: Ignored.
            kwargs: Ignored.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("down")

    def insert_one(self, document: Any) -> None:
        """Fail.

        Args:
            document: Ignored.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("down")

    def replace_one(self, *args: Any, **kwargs: Any) -> None:
        """Fail.

        Args:
            args: Ignored.
            kwargs: Ignored.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("down")


def test_store_errors() -> None:
    """Errors of the collections are logged, never raised."""
    store = ProvenanceStore(Broken(), Broken(), Broken())
    host = make_host([{"plugin": "test.reject"}], provenance={"record_failed": True})
    run = host.start(inbound(), PRINCIPAL)
    with pytest.raises(Exception) as info:
        run.request()
    record = store.save(run)
    assert record["stored"] is False
    # the builds stay inline
    assert list(record["builds"]) == list(record["plugins"])
    assert next(iter(record["builds"].values()))["id"] == "test.reject"
    assert store.save_failure(run, info.value) is False


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
        assert store.save_failure(run, info.value)
        assert db.failures.count_documents({}) == 1
        failure = db.failures.find_one()
        assert failure["_id"] == f"run-{run.run_id}"
        assert failure["error"]["type"] == "PipelineRejected"
        assert failure["error"]["slug"] == "rejected"
        assert failure["error"]["status"] == 403
        assert failure["failed_at"] is not None


def test_failure_indexes() -> None:
    """Failure records expire after 30 days by default."""
    _, db = stores(record_failed=True)
    for index in FAILURE_INDEXES:
        db.failures.create_index(list(index["keys"].items()), **index["options"])
    info = db.failures.index_information()
    ttl = next(spec for name, spec in info.items() if name != "_id_")
    assert ttl["key"] == [("failed_at", 1)]
    assert ttl["expireAfterSeconds"] == 30 * 24 * 3600


@pytest.mark.parametrize("headers", [None, {1: "x"}, ["x"]])
def test_response_headers_kept(headers: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Response hooks that replace the headers fail, not the request."""

    def on_response(self: Any, response: BackendResponse, ctx: Any) -> None:
        response.client_headers = headers

    monkeypatch.setattr(HeaderPlugin, "on_response", on_response)
    host = make_host([{"plugin": "test.header", "params": {"key": "k", "value": "v"}}])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    attempt = next(run.attempts(run.route(BACKENDS)))
    response = attempt.succeeded(BackendResponse(status=200), remote_id="t1")
    assert isinstance(response.client_headers, Headers)
    execution = next(e for e in run.executions if e.phase == "response")
    assert execution.outcome == "invalid_output"
    assert execution.error == "TypeError"
    json.dumps(run.provenance())


def test_changes_never_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Errors while describing changes are recorded, not raised."""

    def fail(*args: Any) -> None:
        raise AttributeError("bug")

    monkeypatch.setattr(engine, "describe_changes", fail)
    host = make_host([{"plugin": "test.tag"}])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    assert run.executions[0].outcome == "ok"
    assert run.executions[0].changes == {"error": "internal"}


def test_long_pointers() -> None:
    """Long keys are recorded as their parent; collecting stops at the cap."""
    key = "k" * 1000
    assert changed_paths({"a": {}}, {"a": {key: 1}}) == ["/a"]
    assert changed_paths({"a": {key: 1}}, {"a": {key: 1}}) == []
    many = {f"k{i}": i for i in range(10000)}
    assert changed_paths({}, many, limit=3) == ["/k0", "/k1", "/k10"]
    before = InboundRequest(method="POST", path="/", payload={})
    after = InboundRequest(method="POST", path="/", payload={key: 1})
    assert describe_changes("request", before, after)["paths"] == ["/payload"]


def test_run_changes_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The changed paths of a run are bounded in size."""
    monkeypatch.setattr(engine, "MAX_RUN_CHANGES_SIZE", 40)
    host = make_host(
        [
            {"id": "one", "plugin": "test.tag", "params": {"key": "a" * 20}},
            {"id": "two", "plugin": "test.tag", "params": {"key": "b" * 20}},
        ]
    )
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    first, second = (execution.changes for execution in run.executions)
    assert first is not None and second is not None
    assert first["paths"] == ["/payload/tags"]
    assert second["paths"] == []
    assert second["truncated"] is True


@pytest.mark.parametrize("value", [2**64, "\ud800", [1, 2**60]])
def test_annotations_are_i_json(value: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Annotations that are no I-JSON are refused."""
    errors: list[Exception] = []

    def on_request(self: Any, request: InboundRequest, ctx: Any) -> None:
        try:
            ctx.record("x", value)
        except ValueError as exc:
            errors.append(exc)

    monkeypatch.setattr(TagPlugin, "on_request", on_request)
    host = make_host([{"plugin": "test.tag"}])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    assert len(errors) == 1
    assert run.executions[0].annotations == {}


def test_secret_params_redacted() -> None:
    """Secret parameters (SecretStr) are not recorded."""

    class Params(BaseModel):
        token: SecretStr

    step = ResolvedStep(
        id="s",
        alternatives=(
            ResolvedAlternative(plugin="p", params=Params(token=SecretStr("abc"))),
        ),
        timeout=1,
        on_failure="fail",
        origin="pipeline",
    )
    described = ResolvedPipeline(steps=(step,)).describe()
    assert described[0]["alternatives"][0]["params"] == {"token": "**********"}


def test_client_addresses_not_hashed() -> None:
    """Client address headers are left out of the hashes."""
    before = InboundRequest(
        method="POST", path="/", headers=Headers({"X-Forwarded-For": "10.0.0.1"})
    )
    after = InboundRequest(
        method="POST", path="/", headers=Headers({"X-Forwarded-For": "10.0.0.2"})
    )
    changes = describe_changes("request", before, after)
    assert changes["paths"] == []
    assert changes["before"] == changes["after"]


def test_save_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs whose provenance cannot be built still give a record."""
    store, _ = stores()
    host = make_host([{"plugin": "test.tag"}])
    run = host.start(inbound(), PRINCIPAL, request_id="r1")

    def fail() -> None:
        raise KeyError("bug")

    monkeypatch.setattr(run, "provenance", fail)
    assert store.save(run) == {"request_id": "r1", "error": "internal", "stored": False}


def test_save_failure_by_request_id() -> None:
    """Failure records are keyed by the request identifier."""
    store, db = stores(record_failed=True)
    host = make_host([{"plugin": "test.reject"}], provenance={"record_failed": True})
    run = host.start(inbound(), PRINCIPAL, request_id="r1")
    with pytest.raises(Exception) as info:
        run.request()
    assert store.save_failure(run, info.value)
    assert store.save_failure(run, info.value)
    assert [doc["_id"] for doc in db.failures.find()] == ["r1"]


def test_set_cookie_not_hashed() -> None:
    """Cookies set by backends are left out of the hashes."""
    before = BackendResponse(status=200, client_headers=Headers({"Set-Cookie": "a"}))
    after = BackendResponse(status=200, client_headers=Headers({"Set-Cookie": "b"}))
    changes = describe_changes("response", before, after)
    assert changes["paths"] == []
    assert changes["before"] == changes["after"]


def test_change_hashes_keyed() -> None:
    """Hashes of changes are keyed per process, unlike content hashes."""
    before = InboundRequest(method="POST", path="/", payload={"a": 1})
    after = InboundRequest(method="POST", path="/", payload={"a": 2})
    changes = describe_changes("request", before, after)
    assert changes["before"].startswith("hmac-sha256:")
    assert changes["before"] != changes["after"]
    plain = content_hash({"a": 1}).removeprefix("sha256:")
    assert change_hash({"a": 1}) != f"hmac-sha256:{plain}"
    assert change_hash({"a": 1}) == change_hash({"a": 1.0})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://u:p@a.example.org:8443/x?token=t#f", "https://a.example.org:8443/x"),
        ("https://10.0.0.1/", "https://10.0.0.1/"),
        ("https://[::1]:80/?q", "https://[::1]:80/"),
        ("https://a.example.org:bad/", "https://"),
        ("backend-a", "backend-a"),
    ],
)
def test_clean_url(value: str, expected: str) -> None:
    """URLs lose user info, query and fragment; identifiers are kept."""
    assert clean_url(value) == expected


def test_provenance_urls_clean() -> None:
    """Provenance records hold no user info or queries of backend URLs."""
    url = "https://u:p@x.example.org/?token=t"
    host = make_host([])
    run = host.start(inbound(), PRINCIPAL)
    run.candidates_in = run.candidates_out = [url]
    run.backends_discarded = [url]
    run.selection = ClientSelection(
        backends=BackendSelection.model_construct(type="allow", backends=[url])
    )
    attempt = next(run.attempts([Backend(id=url, url=url)]))
    attempt.succeeded(BackendResponse(status=200), remote_id="t1")
    record = run.provenance()
    clean = "https://x.example.org/"
    assert record["route"]["candidates_in"] == [clean]
    assert record["route"]["candidates_out"] == [clean]
    assert record["selection"]["backends"] == [clean]
    assert record["selection"]["discarded"] == [clean]
    assert [item["backend"] for item in record["attempts"]] == [clean]
    assert url not in set(strings(record))


def strings(data: Any) -> Iterator[str]:
    """List the strings in JSON data.

    Args:
        data: JSON data.

    Yields:
        Strings (keys and values), depth first.
    """
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for key, value in data.items():
            yield key
            yield from strings(value)
    elif isinstance(data, list):
        for item in data:
            yield from strings(item)


def test_array_pointers_bounded() -> None:
    """Long pointers to array items are recorded as their parent."""
    key = "k" * 510  # pointers of the items are too long
    assert changed_paths({key: [1]}, {key: [2]}) == [f"/{key}"]
    assert changed_paths({key: [1]}, {key: [1]}) == []


def test_record_size_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Records are cut to their maximum size, in a fixed order."""

    def record() -> dict[str, Any]:
        return {
            "executions": [
                {"changes": {"paths": ["/" + "p" * 200]}, "annotations": {"k": "v"}},
                {"annotations": {"k": "v"}},
            ],
            "pipeline": {"resolved": [{"plugin": "x"}], "resolved_hash": "h"},
        }

    size = len(json.dumps(record()))
    assert limit_record(record()) == record()
    monkeypatch.setattr(provenance, "MAX_RECORD_SIZE", size - 100)
    cut = limit_record(record())
    assert cut["truncated"] is True
    assert cut["executions"][0]["changes"] == {"paths": [], "truncated": True}
    assert cut["executions"][0]["annotations"] == {"k": "v"}
    assert cut["pipeline"]["resolved"] == [{"plugin": "x"}]
    monkeypatch.setattr(provenance, "MAX_RECORD_SIZE", size - 220)
    cut = limit_record(record())
    assert "annotations" not in cut["executions"][1]
    assert cut["pipeline"]["resolved"] == [{"plugin": "x"}]
    monkeypatch.setattr(provenance, "MAX_RECORD_SIZE", 10)
    cut = limit_record(record())
    assert cut["pipeline"] == {"resolved": None, "resolved_hash": "h"}


def test_huge_configuration_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs with huge pipelines give records within the maximum size."""
    host = make_host([{"plugin": "test.tag", "params": {"value": "v" * 2_000_000}}])
    run = host.start(inbound(), PRINCIPAL)
    run.request()
    record = run.provenance()
    assert record["truncated"] is True
    assert record["pipeline"]["resolved"] is None
    assert record["pipeline"]["resolved_hash"].startswith("sha256:")
    assert len(json.dumps(record).encode()) <= provenance.MAX_RECORD_SIZE
