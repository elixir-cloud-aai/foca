"""Tests for the controllers managing roles on owned resources."""

from copy import deepcopy
from importlib.resources import path as resource_path
from pathlib import Path
from typing import Any
from unittest import mock

import mongomock
import pytest
import yaml
from flask import Flask
from flask.globals import request_ctx
from werkzeug.exceptions import BadRequest, NotFound
from werkzeug.test import TestResponse

from foca.api.register_openapi import register_openapi
from foca.config.access import get_app_config
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, SpecConfig
from foca.security.access_control import (
    ResourceRole,
    SubjectType,
    list_roles,
    ownership,
    ownership_server,
    remove_resource,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.auth import _unauthorized

ISSUER = "https://login.example.org/oidc/"
ADMINS = "urn:example:group:admins"
CONTROLLERS = ownership_server.__name__
TASK = "/api/tasks/1"
FILE = "/api/projects/x/files/a b"

# Bearer tokens mapped to the claims of their (fake) validated JWTs
TOKENS: dict[str, dict[str, Any]] = {
    "alice": {"iss": ISSUER, "sub": "alice", "groups": ["devs", "ops/team"]},
    "bob": {"iss": ISSUER, "sub": "bob"},
    "carol": {"iss": ISSUER, "sub": "carol", "groups": [ADMINS]},
}

POLICIES = [
    ("p", ["owner", "/api/tasks/:id/permissions", "GET"]),
    ("p", ["owner", "/api/tasks/:id/permissions/users/:user", "(PUT)|(DELETE)"]),
    ("p", ["owner", "/api/tasks/:id/permissions/groups/*", "(PUT)|(DELETE)"]),
    ("p", ["owner", "/api/projects/:p/files/:f/permissions", "GET"]),
    ("p", ["owner", "/api/projects/:p/files/:f/permissions/users/:u", "PUT"]),
    # reaches the controller of a misconfigured route
    ("p", ["*", "/api/permissions/users/:u", "PUT"]),
    ("g2", ["user:alice", "owner", TASK]),
    ("g2", ["user:alice", "owner", FILE]),
    # owner without group claim
    ("g2", ["user:bob", "owner", "/api/tasks/3"]),
]


def mock_token_info(token: str) -> dict[str, Any]:
    """Return token info or raise as FOCA's `validate_token()` does."""
    if token not in TOKENS:
        raise _unauthorized("JWT could not be validated")
    return {"jwt": token, "claims": TOKENS[token]}


# Operation identifiers must be unique, so further routes to the controllers
# need aliases, as in apps with several types of owned resources
getFileRoles = ownership_server.getResourceRoles
putFileUserRole = ownership_server.putUserRole
putUnidentifiedRole = ownership_server.putUserRole


def _operation(
    operation_id: str, module: str = CONTROLLERS, **extra: Any
) -> dict[str, Any]:
    """Build an OpenAPI 3 Operation object routed to a controller."""
    operation: dict[str, Any] = {
        "operationId": f"{module}.{operation_id}",
        "responses": {"200": {"description": "OK"}},
    }
    operation.update(extra)
    return operation


def _parameter(name: str, **schema: Any) -> dict[str, Any]:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "schema": {"type": "string", **schema},
    }


ROLE_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {"type": "object", "properties": {"role": {"type": "string"}}}
        }
    },
}

SPEC: dict[str, Any] = {
    "openapi": "3.0.2",
    "info": {"title": "Test API", "version": "1.0.0"},
    "servers": [{"url": "/api"}],
    "security": [{"bearerAuth": []}],
    "paths": {
        "/tasks/{taskId}/permissions": {
            "parameters": [_parameter("taskId")],
            "get": _operation("getResourceRoles"),
        },
        "/tasks/{taskId}/permissions/users/{userId}": {
            "parameters": [_parameter("taskId"), _parameter("userId")],
            "put": _operation("putUserRole", requestBody=ROLE_BODY),
            "delete": _operation("deleteUserRole"),
        },
        "/tasks/{taskId}/permissions/groups/{group}": {
            # groups may contain slashes
            "parameters": [_parameter("taskId"), _parameter("group", format="path")],
            "put": _operation("putGroupRole", requestBody=ROLE_BODY),
            "delete": _operation("deleteGroupRole"),
        },
        "/projects/{p}/files/{f}/permissions": {
            "parameters": [_parameter("p"), _parameter("f")],
            "get": _operation("getFileRoles", __name__),
        },
        "/projects/{p}/files/{f}/permissions/users/{u}": {
            "parameters": [_parameter("p"), _parameter("f"), _parameter("u")],
            "put": _operation("putFileUserRole", __name__, requestBody=ROLE_BODY),
        },
        # misconfigured: no path parameter identifies the resource
        "/permissions/users/{userId}": {
            "parameters": [_parameter("userId")],
            "put": _operation("putUnidentifiedRole", __name__, requestBody=ROLE_BODY),
        },
    },
    "components": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "x-bearerInfoFunc": f"{__name__}.mock_token_info",
            },
        },
    },
}


def _create_app(
    tmp_path: Path,
    spec: dict[str, Any] = SPEC,
    enforce: bool = True,
    disable_auth: bool = False,
) -> Flask:
    """Create an app with an API and in-memory policies."""
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(yaml.safe_dump(deepcopy(spec)))
    with resource_path(ACCESS_CONTROL_BASE_PATH, RESOURCE_ROLE_MODEL_FILE) as model:
        model_path = str(model)
    config = Config.model_validate(
        {
            "security": {
                "auth": {"issuers": [ISSUER], "audience": ["foca"]},
                "access_control": {
                    "enabled": True,
                    "enforce": enforce,
                    "model": model_path,
                    "claim_groups": "groups",
                    "group_mapping": {ADMINS: "admin"},
                    "keep_group_subjects": True,
                },
            }
        }
    )
    cnx_app = create_connexion_app(config)
    cnx_app = register_exception_handler(cnx_app)
    cnx_app = register_openapi(
        app=cnx_app,
        specs=[
            SpecConfig.model_validate({"path": spec_file, "disable_auth": disable_auth})
        ],
        enforce_access_control=enforce,
    )
    adapter = Adapter(
        collection=mongomock.MongoClient()["access_control_db"]["policy_rules"]
    )
    for ptype, rule in POLICIES:
        adapter.save_policy_line(ptype=ptype, rule=rule)
    cnx_app.app.config["CASBIN_MODEL"] = model_path
    cnx_app.app.config["casbin_adapter"] = adapter
    return cnx_app.app


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    """App with the test API and in-memory policies."""
    return _create_app(tmp_path)


def _call(
    app: Flask, method: str, path: str, user: str, role: str | None = None
) -> TestResponse:
    """Call the test API as a user, with a role in the body, if given."""
    kwargs: dict[str, Any] = {"headers": {"Authorization": f"Bearer {user}"}}
    if method == "put":
        kwargs["json"] = {} if role is None else {"role": role}
    response: TestResponse = getattr(app.test_client(), method)(path, **kwargs)
    return response


def _roles(app: Flask, resource: str = TASK) -> list[ResourceRole]:
    with app.app_context():
        return list_roles(resource)


class TestGetResourceRoles:
    """Tests for listing roles."""

    def test_list(self, app: Flask) -> None:
        """Owners list the roles on their resources."""
        response = _call(app, "get", f"{TASK}/permissions", "alice")
        assert response.status_code == 200
        assert response.json == [
            {"subject_type": "user", "subject": "alice", "role": "owner"}
        ]

    def test_not_owner(self, app: Flask) -> None:
        """Others are denied by the policies."""
        assert _call(app, "get", f"{TASK}/permissions", "bob").status_code == 403

    def test_not_found(self, app: Flask) -> None:
        """Resources without owners are not found."""
        response = _call(app, "get", "/api/tasks/2/permissions", "carol")
        assert response.status_code == 404

    def test_nested_resource(self, app: Flask) -> None:
        """Resources may be identified by several path parameters."""
        response = _call(app, "get", "/api/projects/x/files/a%20b/permissions", "alice")
        assert response.status_code == 200
        assert response.json == [
            {"subject_type": "user", "subject": "alice", "role": "owner"}
        ]


class TestUserRoles:
    """Tests for granting and revoking roles of users."""

    def test_grant_and_revoke(self, app: Flask) -> None:
        """Owners grant and revoke roles of users."""
        path = f"{TASK}/permissions/users/bob"
        response = _call(app, "put", path, "alice", "maintainer")
        assert response.status_code == 200
        assert response.json == {
            "subject_type": "user",
            "subject": "bob",
            "role": "maintainer",
        }
        assert ResourceRole("user", "bob", "maintainer") in _roles(app)
        response = _call(app, "delete", path, "alice")
        assert response.status_code == 200
        assert response.json == "bob"
        assert _roles(app) == [ResourceRole("user", "alice", "owner")]
        assert _call(app, "delete", path, "alice").status_code == 404

    def test_nested_resource(self, app: Flask) -> None:
        """Roles are granted on resources with several path parameters."""
        path = "/api/projects/x/files/a%20b/permissions/users/bob"
        assert _call(app, "put", path, "alice", "owner").status_code == 200
        assert ResourceRole("user", "bob", "owner") in _roles(app, FILE)

    @pytest.mark.parametrize("role", ["admin", None])
    def test_invalid_role(self, app: Flask, role: str | None) -> None:
        """Only configured roles are granted."""
        path = f"{TASK}/permissions/users/bob"
        assert _call(app, "put", path, "alice", role).status_code == 400
        assert _roles(app) == [ResourceRole("user", "alice", "owner")]

    def test_invalid_subject(self, app: Flask) -> None:
        """Subjects that cannot be stored in rules are rejected."""
        path = f"{TASK}/permissions/users/a,b"
        assert _call(app, "put", path, "alice", "owner").status_code == 400
        assert _call(app, "delete", path, "alice").status_code == 404

    def test_last_owner(self, app: Flask) -> None:
        """The last owner can't be downgraded or removed."""
        path = f"{TASK}/permissions/users/alice"
        assert _call(app, "put", path, "alice", "maintainer").status_code == 409
        assert _call(app, "delete", path, "alice").status_code == 409
        assert _roles(app) == [ResourceRole("user", "alice", "owner")]

    def test_not_found(self, app: Flask) -> None:
        """Roles are not granted on resources without owners."""
        path = "/api/tasks/2/permissions/users/bob"
        assert _call(app, "put", path, "carol", "owner").status_code == 404
        assert _call(app, "delete", path, "carol").status_code == 404
        assert _roles(app, "/api/tasks/2") == []

    @pytest.mark.parametrize("subject_type", ["user", "group"])
    def test_existence_checked(self, app: Flask, subject_type: SubjectType) -> None:
        """Roles granted on resources deleted meanwhile are revoked again.

        Apps pass existence checks via wrappers of the controllers.
        """
        put = getattr(ownership_server, f"put{subject_type.capitalize()}Role")
        path = f"{TASK}/permissions/{subject_type}s/devs"
        with app.test_request_context(path, method="PUT"):
            # token info, as set by Connexion
            request_ctx.connexion_context = {  # type: ignore[attr-defined]
                "token_info": mock_token_info("alice")
            }
            with pytest.raises(NotFound):
                put(body={"role": "maintainer"}, exists=lambda: False)
            assert ResourceRole(subject_type, "devs", "maintainer") not in _roles(app)
            put(body={"role": "maintainer"}, exists=lambda: True)
        assert _roles(app)[-1] == ResourceRole(subject_type, "devs", "maintainer")

    def test_existence_checked_on_restore(self, app: Flask) -> None:
        """Owners restored on resources deleted meanwhile are revoked again."""
        path = f"{TASK}/permissions/users/alice"
        with (
            app.test_request_context(path, method="DELETE"),
            mock.patch.object(ownership, "_check_other_owners"),
            pytest.raises(NotFound),
        ):
            ownership_server.deleteUserRole(exists=lambda: False)
        assert _roles(app) == []

    def test_failing_check_on_restore(self, app: Flask) -> None:
        """Restored owners are kept if checking the resource fails."""

        def fail() -> bool:
            raise RuntimeError("database not available")

        path = f"{TASK}/permissions/users/alice"
        with (
            app.test_request_context(path, method="DELETE"),
            mock.patch.object(ownership, "_check_other_owners"),
            pytest.raises(RuntimeError),
        ):
            ownership_server.deleteUserRole(exists=fail)
        assert _roles(app) == [ResourceRole("user", "alice", "owner")]

    def test_failing_check_on_repeated_grant(self, app: Flask) -> None:
        """Roles granted before are kept if checking the resource fails."""

        def fail() -> bool:
            raise RuntimeError("database not available")

        path = f"{TASK}/permissions/users/alice"
        with app.test_request_context(path, method="PUT"):
            # token info, as set by Connexion
            request_ctx.connexion_context = {  # type: ignore[attr-defined]
                "token_info": mock_token_info("alice")
            }
            with pytest.raises(RuntimeError):
                ownership_server.putUserRole(body={"role": "owner"}, exists=fail)
        assert _roles(app) == [ResourceRole("user", "alice", "owner")]

    def test_deleted_during_grant(self, app: Flask) -> None:
        """Resources deleted while granting a role are not found."""
        grant_role = ownership_server.grant_role

        def delete_then_grant(resource: str, *args: Any, **kwargs: Any) -> Any:
            with app.app_context():
                remove_resource(resource)
            return grant_role(resource, *args, **kwargs)

        path = f"{TASK}/permissions/users/bob"
        with mock.patch.object(ownership_server, "grant_role", delete_then_grant):
            assert _call(app, "put", path, "alice", "maintainer").status_code == 404
        assert _roles(app) == []

    @pytest.mark.parametrize("body", [None, [], "owner", {"role": 1}, {}])
    def test_invalid_body(self, body: Any) -> None:
        """Bodies must be objects with a role."""
        with pytest.raises(BadRequest):
            ownership_server._get_role(body)

    def test_owner_granted_again(self, app: Flask) -> None:
        """Owners may be granted the owner role again."""
        path = f"{TASK}/permissions/users/alice"
        assert _call(app, "put", path, "alice", "owner").status_code == 200
        assert _roles(app) == [ResourceRole("user", "alice", "owner")]


class TestRoutes:
    """Tests for taking resources from routes."""

    def test_misconfigured_route(self, app: Flask) -> None:
        """Routes must identify the resource by a path parameter."""
        path = "/api/permissions/users/bob"
        assert _call(app, "put", path, "alice", "owner").status_code == 500
        with (
            app.test_request_context(path, method="PUT"),
            pytest.raises(RuntimeError, match="no path parameter"),
        ):
            ownership_server._get_resource(with_subject=True)

    def test_unrouted_request(self, app: Flask) -> None:
        """Requests must be routed."""
        with (
            app.test_request_context("/unknown"),
            pytest.raises(RuntimeError, match="not routed"),
        ):
            ownership_server._get_resource(with_subject=False)


def putWrappedUserRole(**kwargs: Any) -> dict[str, str]:
    """Wrap a controller, as apps do to pass existence checks."""
    return ownership_server.putUserRole(**kwargs)


def _spec_with(path: str, method: str, **fields: Any) -> dict[str, Any]:
    """Copy the test API, adding fields to one of its operations."""
    spec = deepcopy(SPEC)
    spec["paths"][path][method].update(fields)
    return spec


USER_ROLE = "/tasks/{taskId}/permissions/users/{userId}"
USER_ROUTE = "PUT /api/tasks/<taskId>/permissions/users/<userId> (putUserRole)"


class TestEnforcementRequired:
    """Tests for refusing operations that do not enforce policies."""

    @pytest.mark.parametrize(
        "spec,enforce,disable_auth",
        [
            (SPEC, False, False),
            (SPEC, True, True),
            (_spec_with(USER_ROLE, "put", security=[]), True, False),
            (_spec_with(USER_ROLE, "put", security=[{}]), True, False),
            (
                _spec_with(USER_ROLE, "put", **{"x-foca-access-control": False}),
                True,
                False,
            ),
        ],
        ids=["not-enforced", "disable-auth", "no-security", "anonymous", "opt-out"],
    )
    def test_startup_refused(
        self, tmp_path: Path, spec: dict[str, Any], enforce: bool, disable_auth: bool
    ) -> None:
        """Apps routing unenforced operations to the controllers don't start."""
        with pytest.raises(ValueError, match="not enforced") as exc_info:
            _create_app(tmp_path, spec, enforce=enforce, disable_auth=disable_auth)
        message = str(exc_info.value)
        assert USER_ROUTE in message
        if spec is not SPEC:
            # only the unenforced operation is named
            assert "DELETE" not in message
            assert "getResourceRoles" not in message
        else:
            assert "GET /api/tasks/<taskId>/permissions (getResourceRoles)" in message
            assert "PUT /api/projects/<p>/files/<f>/permissions/users/<u>" in message

    def test_request_refused(self, tmp_path: Path) -> None:
        """Wrappers of the controllers refuse unenforced requests."""
        spec = deepcopy(SPEC)
        spec["paths"] = {
            USER_ROLE: {
                "parameters": [_parameter("taskId"), _parameter("userId")],
                "put": _operation(
                    "putWrappedUserRole", __name__, requestBody=ROLE_BODY, security=[]
                ),
            }
        }
        app = _create_app(tmp_path, spec)
        path = f"{TASK}/permissions/users/mallory"
        with mock.patch.object(ownership_server.logger, "error") as error:
            assert _call(app, "put", path, "mallory", "owner").status_code == 500
        assert "not enforced" in str(error.call_args)
        assert _roles(app) == [ResourceRole("user", "alice", "owner")]

    def test_enforced_wrapper(self, tmp_path: Path) -> None:
        """Wrappers of the controllers serve enforced requests."""
        spec = _spec_with(
            USER_ROLE, "put", operationId=f"{__name__}.putWrappedUserRole"
        )
        app = _create_app(tmp_path, spec)
        path = f"{TASK}/permissions/users/bob"
        assert _call(app, "put", path, "bob", "owner").status_code == 403
        assert _call(app, "put", path, "alice", "owner").status_code == 200
        assert ResourceRole("user", "bob", "owner") in _roles(app)

    def test_unrouted_call(self, app: Flask) -> None:
        """Calls outside of routed requests are refused."""
        with (
            app.test_request_context("/unknown", method="PUT"),
            pytest.raises(RuntimeError, match="not enforced"),
        ):
            ownership_server.putUserRole(body={"role": "owner"})


class TestGroupRoles:
    """Tests for granting and revoking roles of groups."""

    def test_member(self, app: Flask) -> None:
        """Members grant and revoke roles of their groups, also with slashes."""
        path = f"{TASK}/permissions/groups/ops/team"
        response = _call(app, "put", path, "alice", "owner")
        assert response.status_code == 200
        assert response.json == {
            "subject_type": "group",
            "subject": "ops/team",
            "role": "owner",
        }
        assert ResourceRole("group", "ops/team", "owner") in _roles(app)
        response = _call(app, "delete", path, "alice")
        assert response.status_code == 200
        assert response.json == "ops/team"
        assert _roles(app) == [ResourceRole("user", "alice", "owner")]

    def test_not_member(self, app: Flask) -> None:
        """Owners may only grant roles to groups they are members of."""
        path = f"{TASK}/permissions/groups/auditors"
        assert _call(app, "put", path, "alice", "maintainer").status_code == 403
        assert _roles(app) == [ResourceRole("user", "alice", "owner")]

    def test_owner_without_groups(self, app: Flask) -> None:
        """Owners without group claim may not grant roles to groups."""
        path = "/api/tasks/3/permissions/groups/devs"
        assert _call(app, "put", path, "bob", "maintainer").status_code == 403

    def test_admin(self, app: Flask) -> None:
        """Admins grant roles to any group, and owners revoke them."""
        path = f"{TASK}/permissions/groups/auditors"
        assert _call(app, "put", path, "carol", "maintainer").status_code == 200
        assert _call(app, "delete", path, "alice").status_code == 200

    def test_errors(self, app: Flask) -> None:
        """Groups get the same errors as users."""
        path = f"{TASK}/permissions/groups/devs"
        assert _call(app, "delete", path, "alice").status_code == 404
        assert _call(app, "put", path, "alice", "admin").status_code == 400
        missing = "/api/tasks/2/permissions/groups/devs"
        assert _call(app, "put", missing, "carol", "owner").status_code == 404
        # the group is the last owner
        assert _call(app, "put", path, "alice", "owner").status_code == 200
        user = f"{TASK}/permissions/users/alice"
        assert _call(app, "delete", user, "alice").status_code == 200
        assert _call(app, "put", path, "alice", "maintainer").status_code == 409
        assert _call(app, "delete", path, "alice").status_code == 409


@pytest.mark.parametrize("log_traffic", [True, False])
@pytest.mark.parametrize(
    "method,path,role",
    [
        ("get", f"{TASK}/permissions", None),
        ("put", f"{TASK}/permissions/users/bob", "maintainer"),
        ("delete", f"{TASK}/permissions/users/bob", None),
        ("put", f"{TASK}/permissions/groups/devs", "maintainer"),
        ("delete", f"{TASK}/permissions/groups/devs", None),
    ],
)
def test_traffic_logged(
    app: Flask, log_traffic: bool, method: str, path: str, role: str | None
) -> None:
    """Controllers log traffic as the permissions API does, if enabled."""
    get_app_config(app).security.access_control.log_traffic = log_traffic
    with mock.patch("foca.utils.logging.logger") as logger:
        _call(app, method, path, "alice", role)
    messages = [str(call) for call in logger.log.call_args_list]
    assert any("Incoming request" in message for message in messages) is log_traffic
