"""Tests of the Connexion 2 / Flask glue (`foca.plugins.adapter`)."""

import json
import re

import pytest
from connexion.exceptions import Forbidden
from flask import Flask, request
from werkzeug.test import EnvironBuilder

from foca.api.routes import Route, _add_route
from foca.config.access import _get_app_config
from foca.context import RequestIdMiddleware
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config
from foca.plugins import Rejected, adapter
from foca.plugins.engine import LoopDetected, PipelineRejected
from foca.plugins.model import Principal
from foca.security.access_control.enforcement import locked_enforcer
from foca.testing import Client, request_context
from tests.security.access_control.callers import (
    ADMINS,
    ALICE,
    CAROL,
    CONQUEST,
    DAVE,
    HEADER,
    PROTES,
    as_caller,
    create_app,
)


def test_request_id() -> None:
    """Requests have the identifier of the middleware, else a random one."""
    app = Flask(__name__)
    assert re.fullmatch("[0-9a-f]{32}", adapter.request_id())
    assert adapter.request_id() != adapter.request_id()
    with request_context(app=app):
        assert re.fullmatch("[0-9a-f]{32}", adapter.request_id())
    seen = []

    @app.route("/")
    def _view() -> str:
        """Keep the identifier."""
        seen.append(adapter.request_id())
        return ""

    wsgi_app = app.wsgi_app
    app.wsgi_app = RequestIdMiddleware(  # type: ignore[method-assign]
        wsgi_app, trust_incoming=True
    )
    Client(app).get("/", headers={"X-Request-ID": "abc-1"})
    Client(app).get("/", headers={"X-Request-ID": "a b"})
    assert seen[0] == "abc-1"
    assert len(seen[1]) == 32

    app.wsgi_app = RequestIdMiddleware(wsgi_app)  # type: ignore[method-assign]
    Client(app).get("/", headers={"X-Request-ID": "abc-1"})
    assert re.fullmatch("[0-9a-f]{32}", seen[2])


def test_inbound_from_request() -> None:
    """Inbound requests are built from the current request, without credentials."""
    app = Flask(__name__)

    @app.route("/tasks/<task_id>", methods=["POST"])
    def view(task_id: str) -> str:
        """Unused view; only its URL rule matters."""
        return "ok"

    _add_route(
        app,
        Route("POST", "/tasks/{task_id}", "/tasks/<task_id>", None, None, {}, view),
    )
    with request_context(
        "/tasks/abc?view=FULL&view=BASIC&x=1",
        method="post",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer t",
            "Cookie": "c=1",
            "X-Request-ID": "r",
        },
        app=app,
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

    with request_context("/other", method="POST", app=app):
        inbound = adapter.inbound_from_request(None, route="/other")
    assert inbound.route == "/other"
    with request_context("/other", method="POST", app=app):
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
    with request_context(headers={"Authorization": "Bearer tok"}, app=app):
        principal = adapter.current_principal()
    assert principal.subject == "a"
    assert principal.subject_type == "user"
    assert principal.issuer == "i"
    assert principal.groups == ("admin",)
    assert principal.claims == {"sub": "a", "iss": "i"}
    assert principal.token == "tok"  # noqa: S105
    monkeypatch.setattr(adapter, "get_current_claims", lambda: {"iss": 1})
    with request_context(headers={"Authorization": "Basic x"}, app=app):
        principal = adapter.current_principal()
    assert principal.token is None
    assert principal.issuer is None
    monkeypatch.setattr(adapter, "get_current_claims", lambda: {})
    with request_context(headers={"Authorization": "Bearer unchecked"}, app=app):
        assert adapter.current_principal().token is None


def test_repeated_headers() -> None:
    """Repeated header fields are combined, in order."""
    app = Flask(__name__)
    with request_context(
        "/tasks", method="POST", headers=[("Via", "1.1 a"), ("via", "1.1 b")], app=app
    ):
        inbound = adapter.inbound_from_request(None, route="/tasks")
    assert inbound.headers["Via"] == "1.1 a, 1.1 b"


@pytest.mark.parametrize(
    "query",
    [
        b"a=1&a=2&b",
        b"a=%zz&b=%41+c&c=%",
        b"x=%ff%fe&y=\xc3\xa4&z=%C3%A4%ff%C3",
        b"a;b=1&c=",
        b"=v&&k=%26%3D",
        b"q=%2B+%20",
        b"p[]=1&p[]=2#x",
        b"%41=1",
    ],
)
def test_query_as_werkzeug(query: bytes) -> None:
    """Query parameters are those that Werkzeug parses, in order."""
    app = Flask(__name__)
    # As WSGI servers give query strings: decoded as Latin-1
    environ = EnvironBuilder("/tasks").get_environ()
    environ["QUERY_STRING"] = query.decode("latin-1")
    with app.request_context(environ):
        expected = {key: request.args.getlist(key) for key in request.args}
        inbound = adapter.inbound_from_request(None, route="/tasks")
    assert inbound.query == expected


def test_query_invalid_utf8() -> None:
    """Bytes that aren't UTF-8 stay percent-encoded."""
    app = Flask(__name__)
    environ = EnvironBuilder("/tasks").get_environ()
    environ["QUERY_STRING"] = "a=\xff%C3%A4"
    with app.request_context(environ):
        inbound = adapter.inbound_from_request(None, route="/tasks")
    assert inbound.query == {"a": ["%FF\u00e4"]}


def test_header_names_and_media_type() -> None:
    """Header names are those of WSGI; the media type has no parameters."""
    app = Flask(__name__)
    with request_context(
        "/tasks",
        method="POST",
        headers=[
            ("content-type", "Application/JSON; charset=utf-8"),
            ("x-b3-traceid", "1"),
        ],
        app=app,
    ):
        expected = list(dict.fromkeys(name for name, _ in request.headers.items()))
        inbound = adapter.inbound_from_request(None, route="/tasks")
    assert list(inbound.headers) == expected
    assert "X-B3-Traceid" in expected
    assert inbound.media_type == "application/json"
    with request_context("/tasks", app=app):
        assert adapter.inbound_from_request(None).media_type == ""


def test_problem() -> None:
    """Gateway errors become problem responses."""
    cnx_app = create_connexion_app(Config())
    with request_context(app=cnx_app):
        response = adapter.problem(LoopDetected("passed already"))
    assert response.status_code == 508
    body = json.loads(response.get_data())
    assert body["title"] == "Loop Detected"
    assert body["detail"] == "passed already"
    assert body["problem_type"].endswith("#loop-detected")
    rejection = PipelineRejected(Rejected("no", 403, title="Image Not Allowed"), "s")
    with request_context(app=cnx_app):
        response = adapter.problem(rejection)
    assert response.status_code == 403
    assert json.loads(response.get_data())["title"] == "Image Not Allowed"


class TestPrincipalIdentity:
    """The principal matches the identity of access control (unmocked)."""

    def test_user(self) -> None:
        """Users are identified by their subject, with group roles."""
        app = create_app()
        with as_caller(app, ALICE):
            principal = adapter.current_principal()
        assert principal.subject == "alice"
        assert principal.subject_type == "user"
        assert principal.acting_for is None
        assert principal.groups == ("group:devs",)

    def test_client_mapping(self) -> None:
        """Service clients get their client ID and mapped role."""
        app = create_app(client_mapping={"protes": "admin"})
        with as_caller(app, PROTES):
            principal = adapter.current_principal()
        assert principal.subject == "protes"
        assert principal.subject_type == "client"
        assert principal.groups == ("admin",)

    def test_g_rules(self) -> None:
        """Roles from global role rules count, also transitively."""
        app = create_app()
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
        app = create_app()
        with as_caller(app, CONQUEST, headers={HEADER: "alice"}):
            principal = adapter.current_principal()
        assert principal.subject == "conquest"
        assert principal.subject_type == "client"
        assert principal.acting_for == "alice"
        with as_caller(app, PROTES, headers={HEADER: "bob"}), pytest.raises(Forbidden):
            adapter.current_principal()
        # ignored for users (ownership helpers reject it)
        with as_caller(app, ALICE, headers={HEADER: "bob"}):
            assert adapter.current_principal().acting_for is None

    def test_without_access_control(self) -> None:
        """Without access control, roles come from group claims only."""
        app = create_app()
        _get_app_config(app).security.access_control.enabled = False
        del app.config["CASBIN_MODEL"]
        with as_caller(app, CAROL):
            principal = adapter.current_principal()
        assert principal.subject == "carol"
        assert principal.groups == ("admin", f"group:{ADMINS}")
        with as_caller(app, None):
            assert adapter.current_principal() == Principal()
