"""Tests for the controllers managing roles on owned resources."""

from collections.abc import Callable
from copy import deepcopy
from importlib.resources import path as resource_path
from pathlib import Path
from typing import Any
from unittest import mock

import mongomock
import pytest
import yaml
from flask import Flask
from werkzeug.test import TestResponse

from foca.api.register_openapi import register_openapi
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, SpecConfig
from foca.security.access_control import (
    ResourceRole,
    list_roles,
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
    ("g2", ["user:alice", "owner", TASK]),
    ("g2", ["user:alice", "owner", FILE]),
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

SPEC = {
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


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    """App with the test API and in-memory policies."""
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(yaml.safe_dump(deepcopy(SPEC)))
    with resource_path(ACCESS_CONTROL_BASE_PATH, RESOURCE_ROLE_MODEL_FILE) as model:
        model_path = str(model)
    config = Config.model_validate(
        {
            "security": {
                "auth": {"issuers": [ISSUER], "audience": ["foca"]},
                "access_control": {
                    "enabled": True,
                    "enforce": True,
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
        specs=[SpecConfig.model_validate({"path": spec_file})],
        enforce_access_control=True,
    )
    adapter = Adapter(
        collection=mongomock.MongoClient()["access_control_db"]["policy_rules"]
    )
    for ptype, rule in POLICIES:
        adapter.save_policy_line(ptype=ptype, rule=rule)
    cnx_app.app.config["CASBIN_MODEL"] = model_path
    cnx_app.app.config["casbin_adapter"] = adapter
    return cnx_app.app


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

    def test_deleted_meanwhile(self, app: Flask) -> None:
        """Roles granted while the resource is deleted are revoked again."""
        grant_role = ownership_server.grant_role

        def delete_then_grant(*args: Any, **kwargs: Any) -> list[str]:
            # the resource is deleted after the controller checked it
            remove_resource(TASK)
            return grant_role(*args, **kwargs)

        path = f"{TASK}/permissions/users/bob"
        with mock.patch.object(ownership_server, "grant_role", delete_then_grant):
            assert _call(app, "put", path, "alice", "owner").status_code == 404
        assert _roles(app) == []

    def test_owner_granted_again(self, app: Flask) -> None:
        """Owners may be granted the owner role again."""
        path = f"{TASK}/permissions/users/alice"
        assert _call(app, "put", path, "alice", "owner").status_code == 200
        assert _roles(app) == [ResourceRole("user", "alice", "owner")]

    def test_misconfigured_route(self, app: Flask) -> None:
        """Routes must identify the resource by a path parameter."""
        with (
            app.test_request_context("/api/permissions/users/bob", method="PUT"),
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

    def test_admin(self, app: Flask) -> None:
        """Admins grant roles to any group, and owners revoke them."""
        path = f"{TASK}/permissions/groups/auditors"
        assert _call(app, "put", path, "carol", "maintainer").status_code == 200
        assert _call(app, "delete", path, "alice").status_code == 200


@pytest.mark.parametrize(
    "controller",
    [
        ownership_server.getResourceRoles,
        ownership_server.putUserRole,
        ownership_server.deleteUserRole,
        ownership_server.putGroupRole,
        ownership_server.deleteGroupRole,
    ],
)
def test_traffic_logged(controller: Callable[..., Any]) -> None:
    """Controllers log traffic as the permissions API does."""
    assert hasattr(controller, "__wrapped__")
