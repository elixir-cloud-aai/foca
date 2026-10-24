"""Tests for access control of service clients (client-credentials tokens)."""

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import path as resource_path
from typing import Any

import mongomock
import pytest
from connexion.exceptions import BadRequestProblem, Forbidden, Unauthorized
from flask import Flask
from flask.globals import request_ctx

from foca.models.config import Config
from foca.security.access_control import (
    add_resource_role,
    get_current_client,
    get_current_subject,
    get_on_behalf_of_user,
    is_permitted,
    list_resource_roles,
)
from foca.security.access_control.access_control_server import (
    _get_visibility_filter,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.enforcement import (
    _get_current_policy_subjects,
    locked_enforcer,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter

ISSUER = "https://login.example.org/oidc/"
HEADER = "X-On-Behalf-Of"
# Claims of validated tokens
CONQUEST = {"iss": ISSUER, "sub": "conquest", "client_id": "conquest"}
PROTES = {"iss": ISSUER, "sub": "protes", "client_id": "protes"}
OTHER = {"iss": ISSUER, "sub": "other", "client_id": "other"}
ALICE = {"iss": ISSUER, "sub": "alice", "client_id": "conquest", "groups": ["devs"]}
# User whose identifier equals a service client's
USER_CONQUEST = {"iss": ISSUER, "sub": "conquest", "client_id": "krini"}


@pytest.fixture
def app() -> Flask:
    """App with service clients and in-memory policies."""
    app = Flask(__name__)
    with resource_path(ACCESS_CONTROL_BASE_PATH, RESOURCE_ROLE_MODEL_FILE) as model:
        model_path = str(model)
    app.config.foca = Config.model_validate(  # type: ignore[attr-defined]  # dynamic attribute
        {
            "security": {
                "auth": {
                    "issuers": [ISSUER],
                    "audience": ["foca"],
                    "service_clients": [
                        {"client_id": "conquest", "on_behalf_of": True},
                        {"client_id": "protes"},
                    ],
                    "on_behalf_of_header": HEADER,
                },
                "access_control": {
                    "enabled": True,
                    "model": model_path,
                    "claim_groups": "groups",
                    "client_mapping": {"conquest": "submitter"},
                },
            }
        }
    )
    app.config["CASBIN_MODEL"] = model_path
    app.config["casbin_adapter"] = Adapter(
        collection=mongomock.MongoClient()["access_control_db"]["policy_rules"]
    )
    with app.app_context(), locked_enforcer() as enforcer:
        enforcer.add_policy("submitter", "/tasks", "POST")
        enforcer.add_policy("client:protes", "/tasks", "GET")
        enforcer.add_policy("owner", "/tasks/:id", "GET")
        # rules for users whose identifiers equal clients' do not apply
        enforcer.add_policy("user:conquest", "/tasks/:id", "DELETE")
    return app


@contextmanager
def request(
    app: Flask,
    claims: dict[str, Any] | None,
    path: str = "/tasks",
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> Iterator[None]:
    """Request context with token info as set by Connexion."""
    with app.test_request_context(path, method=method, headers=headers):
        request_ctx.connexion_context = (  # type: ignore[attr-defined]  # set by Connexion
            {} if claims is None else {"token_info": {"claims": claims}}
        )
        yield


class TestSubjects:
    """Tests for subjects of service clients in policies."""

    @pytest.mark.parametrize(
        "claims,subjects",
        [
            (CONQUEST, ["client:conquest", "submitter"]),
            (PROTES, ["client:protes"]),
            (OTHER, ["user:other"]),
            (ALICE, ["user:alice", "group:devs"]),
            (USER_CONQUEST, ["user:conquest"]),
        ],
    )
    def test_policy_subjects(
        self, app: Flask, claims: dict[str, Any], subjects: list[str]
    ) -> None:
        """Service clients are `client:` subjects, with mapped roles."""
        with request(app, claims):
            assert _get_current_policy_subjects() == subjects

    def test_current_client(self, app: Flask) -> None:
        """The service client of the current request, if any."""
        with request(app, CONQUEST):
            assert get_current_client() == "conquest"
            # the subject is the token's identity claim, as before
            assert get_current_subject() == "conquest"
        for claims in (ALICE, OTHER, USER_CONQUEST, None):
            with request(app, claims):
                assert get_current_client() is None

    def test_no_request(self, app: Flask) -> None:
        """No service client outside of requests."""
        with app.app_context():
            assert get_current_client() is None

    @pytest.mark.parametrize(
        "claims,method,path,allowed",
        [
            # mapped role
            (CONQUEST, "POST", "/tasks", True),
            (CONQUEST, "GET", "/tasks", False),
            # rule for the client subject
            (PROTES, "GET", "/tasks", True),
            (PROTES, "POST", "/tasks", False),
            # user tokens of a service client don't get its roles
            (ALICE, "POST", "/tasks", False),
            # rules for users don't apply to clients, and vice versa
            (CONQUEST, "DELETE", "/tasks/1", False),
            (USER_CONQUEST, "DELETE", "/tasks/1", True),
            (USER_CONQUEST, "POST", "/tasks", False),
        ],
    )
    def test_enforced(
        self,
        app: Flask,
        claims: dict[str, Any],
        method: str,
        path: str,
        allowed: bool,
    ) -> None:
        """Policies apply to service clients via mapped roles and subjects."""
        with request(app, claims, path=path, method=method):
            assert is_permitted() is allowed

    def test_identity_claim_of_users(self, app: Flask) -> None:
        """Service tokens need not have the identity claim of users."""
        conf = app.config.foca.security.access_control  # type: ignore[attr-defined]  # dynamic attribute
        conf.claim_identity = "email"
        with request(app, CONQUEST, method="POST"):
            assert is_permitted() is True
        with request(app, ALICE, method="POST"), pytest.raises(Unauthorized):
            is_permitted()

    @pytest.mark.parametrize(
        "claims,subjects",
        [
            (CONQUEST, ["client:conquest", "submitter", "*"]),
            (USER_CONQUEST, ["user:conquest", "*"]),
        ],
    )
    def test_visibility_filter(
        self, app: Flask, claims: dict[str, Any], subjects: list[str]
    ) -> None:
        """Self-service permissions are filtered by the policy subjects."""
        conf = app.config.foca.security.access_control  # type: ignore[attr-defined]  # dynamic attribute
        conf.self_service = True
        with request(app, claims, path="/permissions"):
            assert _get_visibility_filter() == {"v0": {"$in": subjects}}

    def test_resource_role(self, app: Flask) -> None:
        """Clients can be granted resource-scoped roles."""
        with request(app, PROTES, path="/tasks/1"):
            assert is_permitted() is False
            add_resource_role("protes", "owner", "/tasks/1", subject_type="client")
            assert is_permitted() is True
            assert list_resource_roles("/tasks/1", subject_type="client") == [
                ("protes", "owner")
            ]
            assert list_resource_roles("/tasks/1", subject_type="user") == []
        with request(app, {"sub": "protes"}, path="/tasks/1"):
            assert is_permitted() is False

    def test_client_role_rejected(self, app: Flask) -> None:
        """Clients cannot be granted as roles."""
        with request(app, ALICE), pytest.raises(ValueError, match="client:"):
            add_resource_role("alice", "client:conquest", "/tasks/1")


class TestOnBehalfOf:
    """Tests for service clients naming the users they act for."""

    def test_header(self, app: Flask) -> None:
        """Allowed service clients may name users in the configured header."""
        with request(app, CONQUEST, headers={HEADER: "alice"}):
            assert get_on_behalf_of_user() == "alice"

    def test_value(self, app: Flask) -> None:
        """Users may also be named by the app, e.g., from a request field."""
        with request(app, CONQUEST, headers={HEADER: "bob"}):
            assert get_on_behalf_of_user("alice") == "alice"

    @pytest.mark.parametrize("claims", [CONQUEST, PROTES, ALICE, USER_CONQUEST])
    def test_none(self, app: Flask, claims: dict[str, Any]) -> None:
        """Without a named user, no user is returned."""
        with request(app, claims):
            assert get_on_behalf_of_user() is None

    @pytest.mark.parametrize("claims", [PROTES, ALICE, USER_CONQUEST, OTHER])
    def test_not_allowed(self, app: Flask, claims: dict[str, Any]) -> None:
        """Other callers may not name users."""
        with request(app, claims, headers={HEADER: "alice"}):
            with pytest.raises(Forbidden):
                get_on_behalf_of_user()
            with pytest.raises(Forbidden):
                get_on_behalf_of_user("alice")

    @pytest.mark.parametrize("user", ["", " ", " alice", "alice\t"])
    def test_empty(self, app: Flask, user: str) -> None:
        """Named users must not be empty or have surrounding whitespace."""
        with request(app, CONQUEST, headers={HEADER: user}):
            with pytest.raises(BadRequestProblem):
                get_on_behalf_of_user()
            with pytest.raises(BadRequestProblem):
                get_on_behalf_of_user(user)

    def test_not_string(self, app: Flask) -> None:
        """Named users passed by the app must be strings."""
        with request(app, CONQUEST), pytest.raises(BadRequestProblem):
            get_on_behalf_of_user(123)  # type: ignore[arg-type]  # invalid on purpose

    def test_no_request(self, app: Flask) -> None:
        """No user is named outside of requests."""
        with app.app_context():
            assert get_on_behalf_of_user() is None

    def test_header_not_configured(self, app: Flask) -> None:
        """Without a configured header, headers are ignored."""
        app.config.foca.security.auth.on_behalf_of_header = None  # type: ignore[attr-defined]  # dynamic attribute
        with request(app, ALICE, headers={HEADER: "bob"}):
            assert get_on_behalf_of_user() is None
