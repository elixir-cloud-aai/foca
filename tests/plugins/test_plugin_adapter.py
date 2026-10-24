"""Tests of the Connexion 2 / Flask glue (`foca.plugins.adapter`)."""

import json

import pytest
from flask import Flask

from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config
from foca.plugins import adapter
from foca.plugins.engine import LoopDetected


def test_request_id() -> None:
    """Request IDs come from safe client headers or are generated."""
    app = Flask(__name__)
    assert len(adapter.request_id()) == 32
    with app.test_request_context(headers={"X-Request-ID": "abc-1"}):
        assert adapter.request_id() == "abc-1"
    with app.test_request_context(headers={"X-Request-ID": "a b"}):
        assert len(adapter.request_id()) == 32


def test_inbound_from_request() -> None:
    """Inbound requests are built from the current request, without credentials."""
    app = Flask(__name__)

    @app.route("/tasks/<task_id>", methods=["POST"])
    def view(task_id: str) -> str:
        return task_id

    with app.test_request_context(
        "/tasks/abc?view=FULL&view=BASIC&x=1",
        method="post",
        json={"a": 1},
        headers={
            "Authorization": "Bearer t",
            "Cookie": "c=1",
            "X-Request-ID": "r",
        },
    ):
        app.preprocess_request()
        from flask import request  # noqa: PLC0415

        request.url_rule = app.url_map.bind("").match(
            "/tasks/abc", method="POST", return_rule=True
        )[0]
        request.view_args = {"task_id": "abc"}
        inbound = adapter.inbound_from_request({"a": 1})
    assert inbound.method == "POST"
    assert inbound.path == "/tasks/abc"
    assert inbound.route == "/tasks/<task_id>"
    assert inbound.path_params == {"task_id": "abc"}
    assert inbound.query == {"view": ["FULL", "BASIC"], "x": ["1"]}
    assert "authorization" not in inbound.headers
    assert "cookie" not in inbound.headers
    assert inbound.headers["x-request-id"] == "r"
    assert inbound.media_type == "application/json"
    assert inbound.payload == {"a": 1}

    with app.test_request_context("/other", method="POST"):
        inbound = adapter.inbound_from_request(None, route="/other")
    assert inbound.route == "/other"
    with app.test_request_context("/other", method="POST"):
        assert adapter.inbound_from_request(None).route == "/other"


def test_current_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Principals have the token's claims, roles and the bearer token."""
    app = Flask(__name__)
    assert adapter.current_principal() == adapter.Principal()
    monkeypatch.setattr(adapter, "get_current_claims", lambda: {"sub": "a", "iss": "i"})
    monkeypatch.setattr(adapter, "get_current_subject", lambda: "a")
    monkeypatch.setattr(adapter, "get_current_groups", lambda: ["admin"])
    with app.test_request_context(headers={"Authorization": "Bearer tok"}):
        principal = adapter.current_principal()
    assert principal.subject == "a"
    assert principal.issuer == "i"
    assert principal.groups == ("admin",)
    assert principal.claims == {"sub": "a", "iss": "i"}
    assert principal.token == "tok"  # noqa: S105
    monkeypatch.setattr(adapter, "get_current_claims", lambda: {"iss": 1})
    with app.test_request_context(headers={"Authorization": "Basic x"}):
        principal = adapter.current_principal()
    assert principal.token is None
    assert principal.issuer is None


def test_problem() -> None:
    """Gateway errors become problem responses."""
    cnx_app = create_connexion_app(Config())
    with cnx_app.app.test_request_context():
        response = adapter.problem(LoopDetected("passed already"))
    assert response.status_code == 508
    body = json.loads(response.get_data())
    assert body["title"] == "Loop Detected"
    assert body["detail"] == "passed already"
    assert body["problem_type"].endswith("#loop-detected")
