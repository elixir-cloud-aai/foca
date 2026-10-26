"""Tests of the Connexion 2 / Flask glue (`foca.plugins.adapter`)."""

import json

import pytest
from connexion.exceptions import Forbidden
from flask import Flask

from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config
from foca.plugins import Rejected, adapter
from foca.plugins.engine import LoopDetected, PipelineRejected
from foca.plugins.model import Principal
from foca.security.access_control.enforcement import locked_enforcer
from tests.security.access_control.test_ownership import (
    ADMINS,
    ALICE,
    CAROL,
    CONQUEST,
    DAVE,
    HEADER,
    PROTES,
    _create_app,
    as_caller,
)


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
        """Unused view; only its URL rule matters."""
        return "ok"

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
    monkeypatch.setattr(adapter, "get_current_roles", lambda: ["admin"])
    monkeypatch.setattr(adapter, "get_current_client", lambda: None)
    monkeypatch.setattr(adapter, "get_on_behalf_of_user", lambda: None)
    with app.test_request_context(headers={"Authorization": "Bearer tok"}):
        principal = adapter.current_principal()
    assert principal.subject == "a"
    assert principal.subject_type == "user"
    assert principal.issuer == "i"
    assert principal.groups == ("admin",)
    assert principal.claims == {"sub": "a", "iss": "i"}
    assert principal.token == "tok"  # noqa: S105
    monkeypatch.setattr(adapter, "get_current_claims", lambda: {"iss": 1})
    with app.test_request_context(headers={"Authorization": "Basic x"}):
        principal = adapter.current_principal()
    assert principal.token is None
    assert principal.issuer is None
    monkeypatch.setattr(adapter, "get_current_claims", lambda: {})
    with app.test_request_context(headers={"Authorization": "Bearer unchecked"}):
        assert adapter.current_principal().token is None


def test_repeated_headers() -> None:
    """Repeated header fields are combined, in order."""
    app = Flask(__name__)
    with app.test_request_context(
        "/tasks", method="POST", headers=[("Via", "1.1 a"), ("via", "1.1 b")]
    ):
        inbound = adapter.inbound_from_request(None, route="/tasks")
    assert inbound.headers["Via"] == "1.1 a, 1.1 b"


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
    rejection = PipelineRejected(Rejected("no", 403, title="Image Not Allowed"), "s")
    with cnx_app.app.test_request_context():
        response = adapter.problem(rejection)
    assert response.status_code == 403
    assert json.loads(response.get_data())["title"] == "Image Not Allowed"


class TestPrincipalIdentity:
    """The principal matches the identity of access control (unmocked)."""

    def test_user(self) -> None:
        """Users are identified by their subject, with group roles."""
        app = _create_app()
        with as_caller(app, ALICE):
            principal = adapter.current_principal()
        assert principal.subject == "alice"
        assert principal.subject_type == "user"
        assert principal.acting_for is None
        assert principal.groups == ("group:devs",)

    def test_client_mapping(self) -> None:
        """Service clients get their client ID and mapped role."""
        app = _create_app(client_mapping={"protes": "admin"})
        with as_caller(app, PROTES):
            principal = adapter.current_principal()
        assert principal.subject == "protes"
        assert principal.subject_type == "client"
        assert principal.groups == ("admin",)

    def test_g_rules(self) -> None:
        """Roles from global role rules count, also transitively."""
        app = _create_app()
        with app.app_context(), locked_enforcer() as enforcer:
            enforcer.add_grouping_policy("user:dave", "admin")
            enforcer.add_grouping_policy("superusers", "admin")
            enforcer.add_grouping_policy("group:devs", "superusers")
        with as_caller(app, DAVE):
            assert adapter.current_principal().groups == ("admin",)
        with as_caller(app, ALICE):
            groups = adapter.current_principal().groups
        assert groups == ("group:devs", "superusers", "admin")

    def test_on_behalf_of(self) -> None:
        """Service clients acting for users name them."""
        app = _create_app()
        with as_caller(app, CONQUEST, headers={HEADER: "alice"}):
            principal = adapter.current_principal()
        assert principal.subject == "conquest"
        assert principal.subject_type == "client"
        assert principal.acting_for == "alice"
        with (
            as_caller(app, ALICE, headers={HEADER: "bob"}),
            pytest.raises(Forbidden),
        ):
            adapter.current_principal()

    def test_without_access_control(self) -> None:
        """Without access control, roles come from group claims only."""
        app = _create_app()
        app.config.foca.security.access_control.enabled = False  # type: ignore[attr-defined]  # dynamic attribute
        del app.config["CASBIN_MODEL"]
        with as_caller(app, CAROL):
            principal = adapter.current_principal()
        assert principal.subject == "carol"
        assert principal.groups == ("admin", f"group:{ADMINS}")
        with as_caller(app, None):
            assert adapter.current_principal() == Principal()
