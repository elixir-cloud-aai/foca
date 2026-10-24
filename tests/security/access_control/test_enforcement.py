"""Tests for config-driven access control enforcement and helpers."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from importlib.resources import path as resource_path
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import mongomock
import pytest
import yaml
from casbin import Enforcer
from connexion.exceptions import BadRequestProblem, Forbidden, Unauthorized
from connexion.operations import AbstractOperation
from connexion.resolver import Resolution, Resolver
from flask import Flask, request
from flask.ctx import RequestContext
from flask.globals import request_ctx
from pydantic import ValidationError
from pymongo import MongoClient
from pymongo.collection import Collection
from werkzeug.test import TestResponse

from foca.api.register_openapi import register_openapi
from foca.config.access import get_app_config
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (
    AccessControlConfig,
    AuthConfig,
    CollectionConfig,
    Config,
    DBConfig,
    MongoConfig,
    SecurityConfig,
    SpecConfig,
)
from foca.security.access_control import (
    add_resource_role,
    enforce_access_control,
    enforcement,
    get_current_group_values,
    get_current_groups,
    get_current_subject,
    list_resource_roles,
    remove_resource,
    remove_resource_role,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_MODEL_FILE,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.enforcement import (
    SubjectType,
    locked_enforcer,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.register_access_control import (
    register_permission_specs,
)
from foca.security.auth import _unauthorized
from tests.mock_data import INVALID_TOKEN_CHALLENGE

DB_NAME = "access_control_db"
COLLECTION_NAME = "policy_rules"
ADMIN_ENTITLEMENT = "urn:example:group:admins"
GROUP = "urn:geant:example.org:group:team#example.org"
ISSUER = "https://login.example.org/oidc/"
THIS_MODULE = __name__

# Bearer tokens mapped to the claims of their (fake) validated JWTs
TOKENS: dict[str, dict[str, Any]] = {
    "alice": {"sub": "alice"},
    "bob": {"sub": "bob"},
    "carol": {"sub": "carol", "groups": [ADMIN_ENTITLEMENT]},
    "dave": {"sub": "dave"},
    "eve": {"sub": "eve", "groups": ["admin"]},
    "frank": {"sub": "frank", "groups": ["testers"]},
    "grace": {"sub": "grace", "groups": ["devs"]},
    "henry": {"sub": "henry", "groups": ["devs"]},
    # identifiers equal to role names
    "owner": {"sub": "owner"},
    "admin": {"sub": "admin"},
    "anonymous": {"iss": "https://example.org"},
}

POLICIES = [
    ("p", ["*", "/items", "POST"]),
    ("p", ["owner", "/items/:id", "(GET)|(PUT)|(DELETE)"]),
    ("p", ["maintainer", "/items/:id", "(GET)|(PUT)"]),
    ("p", ["group:testers", "/items/:id", "GET"]),
    ("g2", ["user:alice", "owner", "/items/1"]),
    ("g2", ["user:bob", "maintainer", "/items/1"]),
    # owners may manage access to their resources via a sub-path
    ("p", ["owner", "/items/:id/permissions", "(GET)|(PUT)"]),
    # stored resource path with trailing slash
    ("g2", ["user:alice", "owner", "/items/3/"]),
]


def mock_token_info(token: str) -> dict[str, Any]:
    """Return token info or raise as FOCA's `validate_token()` does."""
    if token not in TOKENS:
        raise _unauthorized("JWT could not be validated")
    return {"jwt": token, "claims": TOKENS[token]}


def controller(*args: Any, **kwargs: Any) -> str:
    """Generic controller for the test API."""
    return "ok"


# Operations need distinct identifiers
list_items = create_item = get_item = update_item = delete_item = get_open = (
    get_item_permissions
) = get_ref_item = get_optional = controller


def update_item_permissions(id: str, *args: Any, **kwargs: Any) -> str:
    """Grant role on item, as apps let owners manage access."""
    body = request.get_json(silent=True)
    if body:
        subject_type = body.get("subject_type", "user")
        # only accept known subject types and grantable roles
        if subject_type not in {"user", "group"}:
            raise BadRequestProblem(detail="Invalid subject type")
        if body["role"] not in {"owner", "maintainer"}:
            raise BadRequestProblem(detail="Invalid role")
        # only let users grant roles to groups they are members of
        if (
            subject_type == "group"
            and body["subject"] not in get_current_group_values()
        ):
            raise Forbidden
        add_resource_role(
            body["subject"],
            body["role"],
            f"/items/{id}",
            subject_type=subject_type,
        )
    return "ok"


def function_resolver(operation_id: str) -> Callable[..., str]:
    """Callable operation resolver, as accepted by Connexion."""
    return controller


class DuckTypedResolver:
    """Operation resolver not inheriting from `Resolver`, as accepted by Connexion."""

    def resolve(self, operation: AbstractOperation) -> Resolution:
        """Resolve operations to the generic controller."""
        return Resolution(controller, operation.operation_id)


def _operation(operation_id: str, **extra: Any) -> dict[str, Any]:
    """Build an OpenAPI 3 Operation object."""
    operation: dict[str, Any] = {
        "operationId": f"{THIS_MODULE}.{operation_id}",
        "responses": {
            "200": {
                "description": "OK",
                "content": {"application/json": {"schema": {"type": "string"}}},
            },
        },
    }
    operation.update(extra)
    return operation


ID_PARAMETER = {
    "name": "id",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
}

SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "Test API", "version": "1.0.0"},
    "security": [{"bearerAuth": []}],
    "paths": {
        "/items": {
            "get": _operation("list_items", security=[]),
            "post": _operation("create_item"),
        },
        "/items/{id}": {
            "parameters": [ID_PARAMETER],
            "get": _operation("get_item"),
            "put": _operation("update_item"),
            "delete": _operation("delete_item"),
        },
        "/items/{id}/permissions": {
            "parameters": [ID_PARAMETER],
            "get": _operation("get_item_permissions"),
            "put": _operation("update_item_permissions"),
        },
        "/open": {
            "get": _operation("get_open", **{"x-foca-access-control": False}),
        },
        # path item defined via reference
        "/refs/{id}": {"$ref": "#/x-path-items/ref-item"},
        # optional authentication
        "/optional": {
            "get": _operation("get_optional", security=[{}, {"bearerAuth": []}]),
        },
    },
    "x-path-items": {
        "ref-item": {
            "parameters": [ID_PARAMETER],
            "get": _operation("get_ref_item"),
        },
    },
    "components": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "x-bearerInfoFunc": f"{THIS_MODULE}.mock_token_info",
            },
        },
    },
}


def _model_path() -> str:
    with resource_path(ACCESS_CONTROL_BASE_PATH, RESOURCE_ROLE_MODEL_FILE) as _path:
        return str(_path)


def _access_control_config(**kwargs: Any) -> AccessControlConfig:
    params: dict[str, Any] = {
        "db_name": DB_NAME,
        "collection_name": COLLECTION_NAME,
        "model": _model_path(),
        "claim_groups": "groups",
        "group_mapping": {ADMIN_ENTITLEMENT: "admin"},
        "enabled": True,
        "enforce": True,
    }
    params.update(kwargs)
    return AccessControlConfig(**params)


def _create_app(
    tmp_path: Path,
    access_control: AccessControlConfig,
    connexion: dict[str, Any] | None = None,
) -> Flask:
    """Create app with test API, permissions API and in-memory policies."""
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(yaml.safe_dump(deepcopy(SPEC)))
    config = Config(
        security=SecurityConfig(
            auth=AuthConfig(issuers=[ISSUER], audience=["foca"]),
            access_control=access_control,
        ),
        db=MongoConfig(
            dbs={
                DB_NAME: DBConfig(
                    collections={COLLECTION_NAME: CollectionConfig()},
                )
            }
        ),
    )
    cnx_app = create_connexion_app(config)
    cnx_app = register_exception_handler(cnx_app)
    cnx_app = register_openapi(
        app=cnx_app,
        specs=[SpecConfig.model_validate({"path": spec_file, "connexion": connexion})],
        enforce_access_control=access_control.enforce,
    )
    with mock.patch("foca.security.auth.validate_token", new=mock_token_info):
        cnx_app = register_permission_specs(
            app=cnx_app,
            access_control_config=access_control,
        )
    collection: Collection[dict[str, Any]] = mongomock.MongoClient()[DB_NAME][
        COLLECTION_NAME
    ]
    config.db.dbs[DB_NAME].collections[  # type: ignore[union-attr,index]  # set in the config
        COLLECTION_NAME
    ].client = collection
    adapter = Adapter(collection=collection)
    for ptype, rule in POLICIES:
        adapter.save_policy_line(ptype=ptype, rule=rule)
    cnx_app.app.config["CASBIN_MODEL"] = access_control.model
    cnx_app.app.config["casbin_adapter"] = adapter
    return cnx_app.app


@contextmanager
def _capture_logs(
    caplog: pytest.LogCaptureFixture, level: int = logging.WARNING
) -> Iterator[None]:
    """Capture records of the enforcement module from `level` up.

    Records are also captured if app creation (i.e., logging configuration)
    disabled the module's logger or propagation.
    """
    logger = enforcement.logger
    disabled, propagate = logger.disabled, logger.propagate
    logger.disabled, logger.propagate = False, True
    try:
        with caplog.at_level(level, logger=logger.name):
            yield
    finally:
        logger.disabled, logger.propagate = disabled, propagate


def _messages(
    caplog: pytest.LogCaptureFixture, level: int = logging.WARNING
) -> list[str]:
    """Messages of the captured records of the enforcement module.

    Only messages of records with exactly `level` are returned.
    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == enforcement.logger.name and record.levelno == level
    ]


def _headers(user: str | None) -> dict[str, str]:
    return {} if user is None else {"Authorization": f"Bearer {user}"}


def _json(response: TestResponse) -> Any:
    """JSON body of a response."""
    assert response.json is not None
    return response.json


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    """App with test API, permissions API and in-memory policies."""
    return _create_app(tmp_path, _access_control_config())


class TestEnforcement:
    """Tests for enforcing policies on an app's operations."""

    @pytest.mark.parametrize(
        "method,path,user,status",
        [
            # public operation (`security: []`) is not enforced
            ("get", "/items", None, 200),
            # authentication still comes first
            ("post", "/items", None, 401),
            ("delete", "/items/1", "unknown", 401),
            # any authenticated user may create
            ("post", "/items", "dave", 200),
            # owner has full access to own resource only
            ("get", "/items/1", "alice", 200),
            ("put", "/items/1", "alice", 200),
            ("delete", "/items/1", "alice", 200),
            ("delete", "/items/2", "alice", 403),
            # maintainer may read and update, but not delete
            ("get", "/items/1", "bob", 200),
            ("put", "/items/1", "bob", 200),
            ("delete", "/items/1", "bob", 403),
            # users without roles on a resource are denied
            ("get", "/items/1", "dave", 403),
            ("delete", "/items/1", "dave", 403),
            # admin role mapped from group claim may do everything
            ("delete", "/items/2", "carol", 200),
            # unmapped group claim values are not used as roles
            ("delete", "/items/2", "eve", 403),
            ("get", "/items/1", "frank", 403),
            # identifiers equal to role names do not grant these roles
            ("delete", "/items/1", "owner", 403),
            ("delete", "/items/2", "admin", 403),
            # token without identity claim
            ("post", "/items", "anonymous", 401),
            # operation opted out of enforcement
            ("get", "/open", "dave", 200),
        ],
    )
    def test_enforce(
        self, app: Flask, method: str, path: str, user: str | None, status: int
    ) -> None:
        """Policies are enforced on the app's operations."""
        response = getattr(app.test_client(), method)(path, headers=_headers(user))
        assert response.status_code == status

    @pytest.mark.parametrize(
        "user,challenge",
        [
            # no token
            (None, "Bearer"),
            # invalid token
            ("unknown", INVALID_TOKEN_CHALLENGE),
            # token without identity claim: logging in again would not help
            ("anonymous", "Bearer"),
        ],
    )
    def test_challenge(self, app: Flask, user: str | None, challenge: str) -> None:
        """`401` responses carry a Bearer challenge (RFC 6750, §3.1)."""
        response = app.test_client().post("/items", headers=_headers(user))
        assert response.status_code == 401
        assert response.headers.getlist("WWW-Authenticate") == [challenge]

    @pytest.mark.parametrize(
        "method,path,user,status",
        [
            # roles cover sub-paths of the resource they were granted on
            ("get", "/items/1/permissions", "alice", 200),
            ("put", "/items/1/permissions", "alice", 200),
            # ... and stored paths with trailing slashes
            ("get", "/items/3", "alice", 200),
            ("put", "/items/3/permissions", "alice", 200),
            # ... but not sibling resources sharing a prefix
            ("get", "/items/10", "alice", 403),
            ("put", "/items/10/permissions", "alice", 403),
            # ... nor sub-paths of other resources
            ("put", "/items/2/permissions", "alice", 403),
            # ... nor sub-paths without a `p` rule for the role
            ("get", "/items/1/permissions", "bob", 403),
            ("put", "/items/1/permissions", "bob", 403),
            # users without roles on the resource
            ("get", "/items/1/permissions", "dave", 403),
            # admins may still do everything
            ("put", "/items/2/permissions", "carol", 200),
        ],
    )
    def test_enforce_subpaths(
        self, app: Flask, method: str, path: str, user: str | None, status: int
    ) -> None:
        """Resource roles cover sub-paths of their resources."""
        response = getattr(app.test_client(), method)(path, headers=_headers(user))
        assert response.status_code == status

    @pytest.mark.parametrize(
        "method,path,user,status",
        [
            ("get", "/items/1", "alice", 200),
            ("get", "/items/1/permissions", "alice", 403),
            ("put", "/items/1/permissions", "alice", 403),
            ("get", "/items/3", "alice", 403),
        ],
    )
    def test_enforce_subpaths_disabled(
        self, tmp_path: Path, method: str, path: str, user: str | None, status: int
    ) -> None:
        """Resource roles cover only their resources, if configured."""
        app = _create_app(
            tmp_path,
            _access_control_config(resource_roles_cover_subpaths=False),
        )
        response = getattr(app.test_client(), method)(path, headers=_headers(user))
        assert response.status_code == status

    @pytest.mark.parametrize(
        "method,path,user,status",
        [
            # operations of path items defined via reference are enforced
            ("get", "/refs/1", "dave", 403),
            ("get", "/refs/1", "carol", 200),
            # operations with optional authentication are not enforced
            ("get", "/optional", None, 200),
            ("get", "/optional", "dave", 200),
        ],
    )
    def test_enforce_resolved_operations(
        self, app: Flask, method: str, path: str, user: str | None, status: int
    ) -> None:
        """Operations are enforced as resolved by Connexion."""
        response = getattr(app.test_client(), method)(path, headers=_headers(user))
        assert response.status_code == status

    @pytest.mark.parametrize(
        "resolver",
        [
            # callable resolving operation identifiers
            function_resolver,
            # resolver instance
            Resolver(function_resolver=function_resolver),
            # resolver object not inheriting from `Resolver`
            DuckTypedResolver(),
        ],
    )
    @pytest.mark.parametrize(
        "method,path,user,status",
        [
            ("delete", "/items/1", "alice", 200),
            ("delete", "/items/1", "dave", 403),
            ("get", "/items", None, 200),
        ],
    )
    def test_enforce_custom_resolver(
        self,
        tmp_path: Path,
        resolver: Callable[[str], Callable[..., str]] | Resolver | DuckTypedResolver,
        method: str,
        path: str,
        user: str | None,
        status: int,
    ) -> None:
        """Policies are enforced with custom operation resolvers."""
        app = _create_app(
            tmp_path,
            _access_control_config(),
            connexion={"resolver": resolver},
        )
        response = getattr(app.test_client(), method)(path, headers=_headers(user))
        assert response.status_code == status

    def test_enforce_disabled(self, tmp_path: Path) -> None:
        """Policies are not enforced on app operations, if disabled."""
        app = _create_app(tmp_path, _access_control_config(enforce=False))
        response = app.test_client().delete("/items/1", headers=_headers("dave"))
        assert response.status_code == 200

    @pytest.mark.parametrize(
        "method,path,user,status",
        [
            # raw group claim values are prefixed with `group:`
            ("get", "/items/1", "frank", 200),
            ("put", "/items/1", "frank", 403),
            # ... and so do not grant roles of the same name
            ("delete", "/items/2", "eve", 403),
            ("delete", "/items/2", "carol", 403),
        ],
    )
    def test_enforce_groups_without_mapping(
        self, tmp_path: Path, method: str, path: str, user: str | None, status: int
    ) -> None:
        """Without a mapping, group claim values are prefixed subjects."""
        app = _create_app(tmp_path, _access_control_config(group_mapping=None))
        response = getattr(app.test_client(), method)(path, headers=_headers(user))
        assert response.status_code == status

    @pytest.mark.parametrize(
        "method,path,user,status",
        [
            # group claim values are used as subjects despite mapping
            ("get", "/items/1", "frank", 200),
            ("put", "/items/1", "frank", 403),
            # ... including for resource-scoped roles granted to groups
            ("put", "/items/2", "henry", 200),
            ("put", "/items/1", "henry", 403),
            # ... but not roles granted to users of the same name
            ("delete", "/items/2", "henry", 403),
            # ... while mapped values still grant their roles
            ("delete", "/items/2", "carol", 200),
            # ... and values do not grant roles of the same name
            ("delete", "/items/2", "eve", 403),
        ],
    )
    def test_enforce_group_subjects(
        self, tmp_path: Path, method: str, path: str, user: str | None, status: int
    ) -> None:
        """Group claim values are kept as subjects, if configured."""
        app = _create_app(tmp_path, _access_control_config(keep_group_subjects=True))
        adapter = app.config["casbin_adapter"]
        adapter.save_policy_line("g2", ["group:devs", "maintainer", "/items/2"])
        adapter.save_policy_line("g2", ["user:devs", "owner", "/items/2"])
        response = getattr(app.test_client(), method)(path, headers=_headers(user))
        assert response.status_code == status

    def test_enforce_group_subjects_disabled(self, tmp_path: Path) -> None:
        """Group claim values are not subjects by default."""
        app = _create_app(tmp_path, _access_control_config())
        adapter = app.config["casbin_adapter"]
        adapter.save_policy_line("g2", ["group:devs", "maintainer", "/items/2"])
        response = app.test_client().get("/items/2", headers=_headers("henry"))
        assert response.status_code == 403

    def test_warn_subject_like_roles(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Stored roles that are subjects are warned about once."""
        app = _create_app(tmp_path, _access_control_config(subject_prefix="u."))
        adapter = app.config["casbin_adapter"]
        for ptype, rule in [
            ("g2", ["u.mallory", "group:auditors", "/items/1"]),
            ("g2", ["group:devs", "u.bob", "/items/2"]),
            ("g2", ["u.eve", "group:a\tb", "/items/2"]),
            # the prefix is not a pattern
            ("g2", ["u.eve", "uXbob", "/items/2"]),
            # roles of other subject types are ordinary roles
            ("g2", ["u.eve", "user:bob", "/items/2"]),
            # global roles are not checked
            ("g", ["u.eve", "group:auditors"]),
        ]:
            adapter.save_policy_line(ptype, rule)
        client = app.test_client()
        with _capture_logs(caplog):
            for _ in range(2):
                client.get("/items/1", headers=_headers("alice"))
        messages = _messages(caplog)
        # once, irrespective of the number of requests
        assert len(messages) == 3
        for values in [
            "'u.mallory', 'group:auditors', '/items/1'",
            "'group:devs', 'u.bob', '/items/2'",
            # control characters are escaped
            "'u.eve', 'group:a\\tb', '/items/2'",
        ]:
            assert sum(f"(g2, {values})" in message for message in messages) == 1
        assert all("remove_resource_role()" in message for message in messages)

    @pytest.mark.parametrize(
        "user,level", [("dave", logging.INFO), ("carol", logging.DEBUG)]
    )
    def test_decisions_logged_escaped(
        self, app: Flask, caplog: pytest.LogCaptureFixture, user: str | None, level: int
    ) -> None:
        """Request paths cannot forge log lines."""
        with _capture_logs(caplog, level=logging.DEBUG):
            app.test_client().get("/items/1%0Aforged%1B", headers=_headers(user))
        messages = _messages(caplog, level=level)
        assert len(messages) == 1
        assert "/items/1\\nforged\\x1b" in messages[0]
        assert "\n" not in messages[0]

    def test_no_warning_without_subject_like_roles(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No warnings are logged without subject-like roles."""
        with _capture_logs(caplog):
            response = app.test_client().get("/items/1", headers=_headers("alice"))
        assert response.status_code == 200
        assert _messages(caplog) == []


class TestPermissionsApi:
    """Tests for securing the permissions API."""

    url = "/admin/access-control/permissions"
    payload: ClassVar[dict[str, Any]] = {
        "policy_type": "p",
        "rule": {"v0": "dave", "v1": "/items/:id", "v2": "GET"},
    }

    @pytest.mark.parametrize(
        "user,status",
        [
            (None, 401),
            ("unknown", 401),
            ("dave", 403),
            ("alice", 403),
            ("eve", 403),
            ("admin", 403),
            ("carol", 200),
        ],
    )
    def test_post_permission(self, app: Flask, user: str | None, status: int) -> None:
        """Only admins may create permissions."""
        response = app.test_client().post(
            self.url, json=self.payload, headers=_headers(user)
        )
        assert response.status_code == status

    def test_get_permissions_unauthorized(self, app: Flask) -> None:
        """Listing permissions requires authentication."""
        response = app.test_client().get(self.url)
        assert response.status_code == 401

    def test_permissions_api_enforced_if_enforce_disabled(self, tmp_path: Path) -> None:
        """The permissions API is enforced irrespective of `enforce`."""
        app = _create_app(tmp_path, _access_control_config(enforce=False))
        response = app.test_client().post(
            self.url, json=self.payload, headers=_headers("dave")
        )
        assert response.status_code == 403

    def test_post_permission_applied(self, app: Flask) -> None:
        """Created permissions are enforced."""
        payload = deepcopy(self.payload)
        payload["rule"]["v0"] = "user:dave"
        response = app.test_client().post(
            self.url, json=payload, headers=_headers("carol")
        )
        assert response.status_code == 200
        response = app.test_client().get("/items/2", headers=_headers("dave"))
        assert response.status_code == 200

    @pytest.mark.parametrize(
        "payload",
        [
            # unknown policy type
            {
                "policy_type": "p2",
                "rule": {"v0": "a", "v1": "/b", "v2": "GET"},
            },
            {"policy_type": "x", "rule": {"v0": "a", "v1": "/b", "v2": "GET"}},
            # extra keys
            {
                "policy_type": "p",
                "rule": {"v0": "a", "v1": "/b", "v2": "GET", "v6": "x"},
            },
            {
                "policy_type": "p",
                "rule": {"v0": "a", "v1": "/b", "v2": "GET"},
                "rule_section": "p",
            },
            # missing values
            {"policy_type": "g2", "rule": {"v0": "user:a", "v1": "owner"}},
            # invalid values
            {
                "policy_type": "p",
                "rule": {"v0": "a,b", "v1": "/b", "v2": "GET"},
            },
            {"policy_type": "g", "rule": {"v0": "user:a\np", "v1": "admin"}},
            {"policy_type": "g", "rule": {"v0": "#user:a", "v1": "admin"}},
            # invalid action pattern
            {
                "policy_type": "p",
                "rule": {"v0": "a", "v1": "/b", "v2": "(GET"},
            },
        ],
    )
    @pytest.mark.parametrize("method", ["post", "put"])
    def test_invalid_permission(
        self, app: Flask, method: str, payload: dict[str, Any]
    ) -> None:
        """Invalid permissions are rejected and not stored."""
        url = self.url if method == "post" else f"{self.url}/abc"
        response = getattr(app.test_client(), method)(
            url, json=payload, headers=_headers("carol")
        )
        assert response.status_code == 400
        collection = app.config["casbin_adapter"]._collection
        assert collection.count_documents({}) == len(POLICIES)

    def test_get_incomplete_permissions(self, app: Flask) -> None:
        """Stored rules lacking fields are returned as far as possible."""
        collection = app.config["casbin_adapter"]._collection
        collection.insert_many(
            [
                {"ptype": "g", "v0": "user:x", "v1": "y"},
                {"id": "abc", "v0": "x"},
            ]
        )
        client = app.test_client()
        response = client.get(self.url, headers=_headers("carol"))
        assert response.status_code == 200
        assert len(_json(response)["results"]) == len(POLICIES) + 1
        response = client.get(f"{self.url}/abc", headers=_headers("carol"))
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "path",
        ["/admin/access-control/ui/", "/admin/access-control/openapi.json"],
    )
    def test_ui_and_spec_not_served_by_default(self, app: Flask, path: str) -> None:
        """UI and specification are not served by default."""
        assert app.test_client().get(path).status_code == 404

    def test_ui_and_spec_served(self, tmp_path: Path) -> None:
        """UI and specification are served, if configured."""
        app = _create_app(
            tmp_path,
            _access_control_config(api_swagger_ui=True, api_serve_spec=True),
        )
        client = app.test_client()
        assert client.get("/admin/access-control/ui/").status_code == 200
        assert client.get("/admin/access-control/openapi.json").status_code == 200

    def test_ui_served_with_spec(self, tmp_path: Path) -> None:
        """The specification is served with the UI, which needs it."""
        app = _create_app(tmp_path, _access_control_config(api_swagger_ui=True))
        client = app.test_client()
        assert client.get("/admin/access-control/ui/").status_code == 200
        assert client.get("/admin/access-control/openapi.json").status_code == 200

    def test_ui_without_spec_invalid(self) -> None:
        """The UI can't be served if serving the specification is disabled."""
        with pytest.raises(ValidationError, match="needs the specification"):
            AccessControlConfig(api_swagger_ui=True, api_serve_spec=False)


class TestPagination:
    """Tests for listing permissions page by page."""

    url = "/admin/access-control/permissions"

    def _get(self, app: Flask, user: str = "carol", **params: Any) -> TestResponse:
        return app.test_client().get(
            self.url, query_string=params, headers=_headers(user)
        )

    def test_default(self, app: Flask) -> None:
        """Permissions are listed on one page by default."""
        response = self._get(app)
        assert response.status_code == 200
        assert _json(response)["pagination"] == {
            "page": 0,
            "page_size": 100,
            "total": len(POLICIES),
            "total_pages": 1,
        }
        assert [
            [permission["policy_type"], *permission["rule"].values()]
            for permission in _json(response)["results"]
        ] == [[ptype, *rule] for ptype, rule in POLICIES]

    def test_all_pages(self, app: Flask) -> None:
        """All permissions are listed once across pages."""
        ids: list[str] = []
        for page in range(3):
            response = self._get(app, page_size=3, page=page)
            assert response.status_code == 200
            assert _json(response)["pagination"] == {
                "page": page,
                "page_size": 3,
                "total": len(POLICIES),
                "total_pages": 3,
            }
            ids.extend(p["id"] for p in _json(response)["results"])
        assert len(ids) == len(set(ids)) == len(POLICIES)

    @pytest.mark.parametrize(
        "params",
        [
            {"page": 1},
            {"page_size": 3, "page": 3},
            {"page": -1},
            {"page_size": 0},
            {"page_size": 1001},
            {"page_size": "x"},
        ],
    )
    def test_out_of_range(self, app: Flask, params: dict[str, Any]) -> None:
        """Pages and page sizes out of range are rejected."""
        assert self._get(app, **params).status_code == 400

    def test_empty(self, app: Flask) -> None:
        """Without permissions, only the first, empty page exists."""
        app.config["casbin_adapter"]._collection.delete_many({})
        response = self._get(app)
        assert response.status_code == 200
        assert response.json == {
            "results": [],
            "pagination": {
                "page": 0,
                "page_size": 100,
                "total": 0,
                "total_pages": 0,
            },
        }
        assert self._get(app, page=1).status_code == 400


class TestSelfService:
    """Tests for subjects accessing the permissions that apply to them."""

    url = "/admin/access-control/permissions"

    @pytest.fixture
    def self_service_app(self, tmp_path: Path) -> Flask:
        """App with self-service access to permissions."""
        return _create_app(tmp_path, _access_control_config(self_service=True))

    @staticmethod
    def _id(app: Flask, **query: Any) -> str:
        collection = app.config["casbin_adapter"]._collection
        return collection.find_one(query)["id"]

    def _list(self, app: Flask, user: str, **params: Any) -> list[list[str]]:
        response = app.test_client().get(
            self.url, query_string=params, headers=_headers(user)
        )
        assert response.status_code == 200
        return [
            [p["policy_type"], *p["rule"].values()] for p in _json(response)["results"]
        ]

    def test_disabled(self, app: Flask) -> None:
        """Subjects cannot access their permissions by default."""
        client = app.test_client()
        rule_id = self._id(app, v0="user:alice")
        for url in [self.url, f"{self.url}/{rule_id}"]:
            response = client.get(url, headers=_headers("alice"))
            assert response.status_code == 403

    @pytest.mark.parametrize(
        "user,expected",
        [
            (
                "alice",
                [
                    ["p", "*", "/items", "POST"],
                    ["g2", "user:alice", "owner", "/items/1"],
                    ["g2", "user:alice", "owner", "/items/3/"],
                ],
            ),
            # groups without mapping do not apply
            ("frank", [["p", "*", "/items", "POST"]]),
            ("dave", [["p", "*", "/items", "POST"]]),
            ("carol", [[ptype, *rule] for ptype, rule in POLICIES]),
        ],
    )
    def test_list(
        self, self_service_app: Flask, user: str, expected: list[list[str]]
    ) -> None:
        """Subjects list the permissions that apply to them."""
        assert self._list(self_service_app, user) == expected

    def test_list_groups(self, tmp_path: Path) -> None:
        """Permissions of unmapped groups apply to their members."""
        app = _create_app(
            tmp_path,
            _access_control_config(self_service=True, group_mapping=None),
        )
        assert self._list(app, "frank") == [
            ["p", "*", "/items", "POST"],
            ["p", "group:testers", "/items/:id", "GET"],
        ]

    def test_list_group_subjects(self, tmp_path: Path) -> None:
        """Permissions of groups kept as subjects apply to their members."""
        app = _create_app(
            tmp_path,
            _access_control_config(self_service=True, keep_group_subjects=True),
        )
        assert self._list(app, "frank") == [
            ["p", "*", "/items", "POST"],
            ["p", "group:testers", "/items/:id", "GET"],
        ]

    def test_list_paginated(self, self_service_app: Flask) -> None:
        """Permissions that apply to subjects are listed page by page."""
        pages = [
            self._list(self_service_app, "alice", page_size=2, page=page)
            for page in range(2)
        ]
        assert pages == [
            [
                ["p", "*", "/items", "POST"],
                ["g2", "user:alice", "owner", "/items/1"],
            ],
            [["g2", "user:alice", "owner", "/items/3/"]],
        ]
        response = self_service_app.test_client().get(
            self.url,
            query_string={"page_size": 2, "page": 2},
            headers=_headers("alice"),
        )
        assert response.status_code == 400

    def test_get(self, self_service_app: Flask) -> None:
        """Subjects get only permissions that apply to them."""
        client = self_service_app.test_client()
        own = self._id(self_service_app, v0="user:alice")
        other = self._id(self_service_app, v0="user:bob")
        response = client.get(f"{self.url}/{own}", headers=_headers("alice"))
        assert response.status_code == 200
        assert _json(response)["rule"]["v0"] == "user:alice"
        response = client.get(f"{self.url}/{other}", headers=_headers("alice"))
        assert response.status_code == 404
        response = client.get(f"{self.url}/{other}", headers=_headers("carol"))
        assert response.status_code == 200

    def test_unauthenticated(self, self_service_app: Flask) -> None:
        """Self-service requires authentication."""
        response = self_service_app.test_client().get(self.url)
        assert response.status_code == 401

    def test_writes_enforced(self, self_service_app: Flask) -> None:
        """Subjects cannot change their permissions."""
        client = self_service_app.test_client()
        own = self._id(self_service_app, v0="user:alice")
        payload = {
            "policy_type": "g",
            "rule": {"v0": "user:alice", "v1": "admin"},
        }
        headers = _headers("alice")
        assert client.post(self.url, json=payload, headers=headers).status_code == 403
        url = f"{self.url}/{own}"
        assert client.put(url, json=payload, headers=headers).status_code == 403
        assert client.delete(url, headers=headers).status_code == 403

    @pytest.mark.parametrize("self_service", [False, True])
    def test_owners_manage_access(self, tmp_path: Path, self_service: bool) -> None:
        """Owners grant roles on their resources via app endpoints."""
        app = _create_app(tmp_path, _access_control_config(self_service=self_service))
        client = app.test_client()
        assert client.get("/items/1", headers=_headers("dave")).status_code == 403
        # only owners may manage access
        response = client.put(
            "/items/1/permissions",
            json={"subject": "dave", "role": "maintainer"},
            headers=_headers("bob"),
        )
        assert response.status_code == 403
        response = client.put(
            "/items/1/permissions",
            json={"subject": "dave", "role": "maintainer"},
            headers=_headers("alice"),
        )
        assert response.status_code == 200
        assert client.get("/items/1", headers=_headers("dave")).status_code == 200
        assert client.delete("/items/1", headers=_headers("dave")).status_code == 403

    @pytest.mark.parametrize("keep_group_subjects", [False, True])
    def test_owners_grant_roles_to_groups(
        self, tmp_path: Path, keep_group_subjects: bool
    ) -> None:
        """Owners grant roles on their resources to their groups."""
        app = _create_app(
            tmp_path,
            _access_control_config(keep_group_subjects=keep_group_subjects),
        )
        app.config["casbin_adapter"].save_policy_line(
            "g2", ["user:grace", "owner", "/items/5"]
        )
        client = app.test_client()
        url = "/items/5/permissions"
        headers = _headers("grace")
        # only to groups the owner is a member of
        for group in ["testers", "devs"]:
            response = client.put(
                url,
                json={
                    "subject": group,
                    "role": "maintainer",
                    "subject_type": "group",
                },
                headers=headers,
            )
            assert response.status_code == (200 if group == "devs" else 403)
        # only known subject types and grantable roles, so that, e.g.,
        # owners cannot make themselves members of other groups
        for payload in [
            {"subject": "devs", "role": "maintainer", "subject_type": "role"},
            {"subject": "grace", "role": "group:testers"},
            {"subject": "devs", "role": "admin", "subject_type": "group"},
        ]:
            response = client.put(url, json=payload, headers=headers)
            assert response.status_code == 400
        with app.app_context():
            assert list_resource_roles("/items/5", subject_type="group") == [
                ("devs", "maintainer")
            ]
            assert list_resource_roles("/items/5", subject_type="user") == [
                ("grace", "owner")
            ]
        # roles granted to groups apply to members if groups are subjects
        expected = 200 if keep_group_subjects else 403
        for method in ["get", "put"]:
            response = getattr(client, method)("/items/5", headers=_headers("henry"))
            assert response.status_code == expected
        assert client.delete("/items/5", headers=_headers("henry")).status_code == 403


@pytest.fixture
def helper_app() -> Flask:
    """Flask app with access control config and in-memory policies."""
    app = Flask(__name__)
    app.config.foca = Config(  # type: ignore[attr-defined]  # dynamic attribute
        security=SecurityConfig(
            auth=AuthConfig(issuers=[ISSUER], audience=["foca"]),
            access_control=_access_control_config(),
        ),
    )
    adapter = Adapter(collection=mongomock.MongoClient()[DB_NAME][COLLECTION_NAME])
    app.config["CASBIN_MODEL"] = _model_path()
    app.config["casbin_adapter"] = adapter
    return app


@pytest.fixture(
    params=["mongomock", pytest.param("mongodb", marks=pytest.mark.mongodb)]
)
def store_app(request: pytest.FixtureRequest, helper_app: Flask) -> Iterator[Flask]:
    """`helper_app`, with policies in memory or in a MongoDB server.

    The MongoDB server runs on `localhost:12345` (tests marked `mongodb`).
    """
    if request.param == "mongomock":
        yield helper_app
        return
    db_name = "foca_test_enforcement"
    client: MongoClient[dict[str, Any]] = MongoClient(
        "mongodb://localhost:12345", serverSelectionTimeoutMS=5000
    )
    client.drop_database(db_name)
    helper_app.config["casbin_adapter"] = Adapter(
        collection=client[db_name][COLLECTION_NAME]
    )
    yield helper_app
    client.drop_database(db_name)
    client.close()


class TestRemovingConfiguredRules:
    """Tests for removing resource roles next to configured rules."""

    RULE: ClassVar[list[str]] = ["user:alice", "owner", "/items/1"]

    @staticmethod
    def _documents(app: Flask) -> list[dict[str, Any]]:
        collection = app.config["casbin_adapter"]._collection
        return list(collection.find({}, projection={"_id": False}))

    def test_rule_marked_concurrently(
        self,
        store_app: Flask,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rule marked as configured after loading the policies is kept."""
        adapter = store_app.config["casbin_adapter"]
        adapter.save_policy_line("g2", self.RULE)
        remove_policy = adapter.remove_policy

        def mark_and_remove(*args: Any, **kwargs: Any) -> bool:
            adapter._collection.update_many({}, {"$set": {"configured": True}})
            return remove_policy(*args, **kwargs)

        monkeypatch.setattr(adapter, "remove_policy", mark_and_remove)
        with store_app.app_context(), _capture_logs(caplog):
            assert remove_resource_role("alice", "owner", "/items/1") is False
        assert [doc["configured"] for doc in self._documents(store_app)] == [True]
        assert len(_messages(caplog)) == 1

    def test_unmarked_copy_removed(
        self, store_app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unmarked copies of a configured rule are removed.

        The role still applies, though.
        """
        adapter = store_app.config["casbin_adapter"]
        adapter.save_policy_line("g2", self.RULE, configured=True)
        adapter.save_policy_line("g2", self.RULE)
        with store_app.app_context(), _capture_logs(caplog):
            assert remove_resource_role("alice", "owner", "/items/1") is False
        assert [doc.get("configured") for doc in self._documents(store_app)] == [True]
        assert len(_messages(caplog)) == 1

    @pytest.mark.parametrize(
        "document",
        [
            # extra value, skipped when loading policies
            {"v0": "user:a", "v1": "owner", "v2": "/items/1", "v3": "x"},
            # value that is not a string
            {"v0": 1, "v1": "owner", "v2": "/items/1"},
        ],
    )
    def test_invalid_rules_not_revoked(
        self, store_app: Flask, document: dict[str, Any]
    ) -> None:
        """Invalid rules on the resource are removed.

        They do not count as revoked roles, though.
        """
        collection = store_app.config["casbin_adapter"]._collection
        collection.insert_one({"ptype": "g2", **document})
        with store_app.app_context():
            assert remove_resource("/items/1") is False
        assert self._documents(store_app) == []

    def test_concurrent_removal(
        self, store_app: Flask, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rules removed by a concurrent call do not count as revoked."""
        adapter = store_app.config["casbin_adapter"]
        adapter.save_policy_line("g2", self.RULE)
        get_filtered_rules = adapter.get_filtered_rules

        def get_and_remove(*args: Any, configured: bool) -> list[list[str]]:
            rules = get_filtered_rules(*args, configured=configured)
            if not configured:
                # the concurrent call removes the rules first
                adapter._collection.delete_many({})
            return rules

        monkeypatch.setattr(adapter, "get_filtered_rules", get_and_remove)
        with store_app.app_context():
            assert remove_resource("/items/1") is False

    def test_remove_resource(self, store_app: Flask) -> None:
        """All copies of rules on a resource are removed at once."""
        adapter = store_app.config["casbin_adapter"]
        for _ in range(2):
            adapter.save_policy_line("g2", self.RULE)
        with store_app.app_context():
            assert remove_resource("/items/1") is True
            assert remove_resource("/items/1") is False
        assert self._documents(store_app) == []


class TestCurrentSubject:
    """Tests for getting subject and groups from validated token claims."""

    def _context(self, app: Flask, token_info: dict[str, Any] | None) -> RequestContext:
        ctx = app.test_request_context()
        ctx.push()
        request_ctx.connexion_context = (  # type: ignore[attr-defined]  # set by Connexion
            {} if token_info is None else {"token_info": token_info}
        )
        return ctx

    def test_subject(self, helper_app: Flask) -> None:
        """Subject and groups are taken from validated token info."""
        ctx = self._context(helper_app, mock_token_info("carol"))
        try:
            assert get_current_subject() == "carol"
            assert get_current_groups() == ["admin"]
        finally:
            ctx.pop()

    def test_raw_claims(self, helper_app: Flask) -> None:
        """Subject is taken from claims passed as token info."""
        ctx = self._context(helper_app, TOKENS["carol"])
        try:
            assert get_current_subject() == "carol"
        finally:
            ctx.pop()

    def test_string_group_claim(self, helper_app: Flask) -> None:
        """Group claims may be single strings."""
        ctx = self._context(helper_app, {"sub": "x", "groups": ADMIN_ENTITLEMENT})
        try:
            assert get_current_groups() == ["admin"]
        finally:
            ctx.pop()

    def test_custom_identity_claim(self, helper_app: Flask) -> None:
        """Subject is taken from the configured identity claim."""
        get_app_config(helper_app).security.access_control.claim_identity = "id"
        ctx = self._context(helper_app, {"sub": "x", "id": "y"})
        try:
            assert get_current_subject() == "y"
        finally:
            ctx.pop()

    def test_identity_claim_from_auth_config(self, helper_app: Flask) -> None:
        """Identity claim defaults to that of the auth configuration."""
        get_app_config(helper_app).security.auth.claim_identity = "email"
        ctx = self._context(helper_app, {"sub": "x", "email": "y"})
        try:
            assert get_current_subject() == "y"
        finally:
            ctx.pop()

    def test_identity_claim_overrides_auth_config(self, helper_app: Flask) -> None:
        """Configured identity claim overrides that of the auth configuration."""
        get_app_config(helper_app).security.auth.claim_identity = "email"
        get_app_config(helper_app).security.access_control.claim_identity = "sub"
        ctx = self._context(helper_app, {"sub": "x", "email": "y"})
        try:
            assert get_current_subject() == "x"
        finally:
            ctx.pop()

    def test_invalid_group_claim(self, helper_app: Flask) -> None:
        """Invalid group claims yield no groups."""
        ctx = self._context(helper_app, {"sub": "x", "groups": {"a": 1}})
        try:
            assert get_current_groups() == []
        finally:
            ctx.pop()

    def test_groups_without_mapping(self, helper_app: Flask) -> None:
        """Without a mapping, group claim values are prefixed."""
        get_app_config(helper_app).security.access_control.group_mapping = None
        ctx = self._context(helper_app, {"sub": "x", "groups": ["a", "b"]})
        try:
            assert get_current_groups() == ["group:a", "group:b"]
        finally:
            ctx.pop()

    @pytest.mark.parametrize(
        "values,expected",
        [
            ([ADMIN_ENTITLEMENT, "a", "a", 1], [ADMIN_ENTITLEMENT, "a", "1"]),
            ("a", ["a"]),
            (("a", "b"), ["a", "b"]),
            ([], []),
            ({"a": 1}, []),
            (None, []),
        ],
    )
    def test_group_values(
        self, helper_app: Flask, values: object, expected: list[str]
    ) -> None:
        """Group claim values are returned as unique strings, if valid."""
        ctx = self._context(helper_app, {"sub": "x", "groups": values})
        try:
            assert get_current_group_values() == expected
        finally:
            ctx.pop()

    def test_group_values_without_claim(self, helper_app: Flask) -> None:
        """Without a group claim, there are no groups."""
        ctx = self._context(helper_app, {"sub": "x"})
        try:
            assert get_current_group_values() == []
            assert get_current_groups() == []
        finally:
            ctx.pop()

    @pytest.mark.parametrize("group_mapping", [None, {"a": "auditor"}])
    def test_group_subjects(
        self, helper_app: Flask, group_mapping: dict[str, str] | None
    ) -> None:
        """Group claim values are kept as subjects, if configured."""
        conf = get_app_config(helper_app).security.access_control
        conf.keep_group_subjects = True
        if group_mapping is None:
            conf.group_mapping = None
        else:
            assert conf.group_mapping is not None
            conf.group_mapping.update(group_mapping)
        ctx = self._context(
            helper_app,
            {"sub": "x", "groups": [ADMIN_ENTITLEMENT, "a", "b", "a"]},
        )
        try:
            mapped = [] if group_mapping is None else ["admin", "auditor"]
            assert get_current_groups() == [
                *mapped,
                f"group:{ADMIN_ENTITLEMENT}",
                "group:a",
                "group:b",
            ]
        finally:
            ctx.pop()

    def test_mapped_groups_only(self, helper_app: Flask) -> None:
        """Only mapped group claim values are groups."""
        ctx = self._context(
            helper_app, {"sub": "x", "groups": [ADMIN_ENTITLEMENT, "a"]}
        )
        try:
            assert get_current_groups() == ["admin"]
            assert get_current_group_values() == [ADMIN_ENTITLEMENT, "a"]
        finally:
            ctx.pop()

    def test_no_token_info(self, helper_app: Flask) -> None:
        """Without token info, there are no subject and groups."""
        ctx = self._context(helper_app, None)
        try:
            assert get_current_subject() is None
            assert get_current_groups() == []
            assert get_current_group_values() == []
        finally:
            ctx.pop()

    def test_outside_connexion(self, helper_app: Flask) -> None:
        """Outside Connexion requests, there are no subject and groups."""
        with helper_app.test_request_context():
            assert get_current_subject() is None
            assert get_current_groups() == []

    def test_outside_request(self, helper_app: Flask) -> None:
        """Outside requests, there are no subject and groups."""
        with helper_app.app_context():
            assert get_current_subject() is None
            assert get_current_groups() == []

    def test_invalid_connexion_context(self, helper_app: Flask) -> None:
        """Errors in claim retrieval are raised, not taken as no claims."""
        ctx = helper_app.test_request_context()
        ctx.push()
        request_ctx.connexion_context = "invalid"  # type: ignore[attr-defined]  # set by Connexion
        try:
            with pytest.raises(AttributeError):
                get_current_subject()
        finally:
            ctx.pop()

    def test_no_group_claim_configured(self, helper_app: Flask) -> None:
        """Without a configured group claim, there are no groups."""
        get_app_config(helper_app).security.access_control.claim_groups = None
        ctx = self._context(helper_app, mock_token_info("carol"))
        try:
            assert get_current_groups() == []
            assert get_current_group_values() == []
        finally:
            ctx.pop()


class TestEnforceAccessControlDecorator:
    """Tests for using `enforce_access_control` on single controllers."""

    @staticmethod
    def _call(
        app: Flask, method: str, path: str, token_info: dict[str, Any] | None
    ) -> str:
        @enforce_access_control
        def decorated() -> str:
            return "ok"

        with app.test_request_context(path, method=method):
            request_ctx.connexion_context = (  # type: ignore[attr-defined]  # set by Connexion
                {} if token_info is None else {"token_info": token_info}
            )
            return decorated()

    def test_allowed(self, helper_app: Flask) -> None:
        """Allowed requests are passed to the controller."""
        with helper_app.app_context(), locked_enforcer() as enforcer:
            enforcer.add_policy("user:alice", "/items", "GET")
        token_info = mock_token_info("alice")
        assert self._call(helper_app, "GET", "/items", token_info) == "ok"

    def test_denied(self, helper_app: Flask) -> None:
        """Denied requests are forbidden."""
        token_info = mock_token_info("alice")
        with pytest.raises(Forbidden):
            self._call(helper_app, "DELETE", "/items", token_info)

    def test_no_subject(self, helper_app: Flask) -> None:
        """Requests without token info are unauthorized."""
        with pytest.raises(Unauthorized) as exc_info:
            self._call(helper_app, "GET", "/items", None)
        assert exc_info.value.get_headers()[1:] == [("WWW-Authenticate", "Bearer")]

    def test_no_subject_claim(self, helper_app: Flask) -> None:
        """Requests without identity claim are unauthorized."""
        with pytest.raises(Unauthorized) as exc_info:
            self._call(helper_app, "GET", "/items", {"iss": "x"})
        assert exc_info.value.get_headers()[1:] == [("WWW-Authenticate", "Bearer")]

    @pytest.mark.parametrize("subject", ["alice,/items,GET", "alice\n", "a" * 1024])
    def test_invalid_subject(self, helper_app: Flask, subject: str) -> None:
        """Requests with invalid subjects are forbidden."""
        with helper_app.app_context(), locked_enforcer() as enforcer:
            enforcer.add_policy("*", "/items", "GET")
        with pytest.raises(Forbidden):
            self._call(helper_app, "GET", "/items", {"sub": subject})


class TestActionMatching:
    """Tests for matching actions in the reference model."""

    @pytest.mark.parametrize(
        "pattern,method,expected",
        [
            ("GET", "GET", True),
            ("GET", "XGETX", False),
            ("GET", "GETX", False),
            ("GET", "XGET", False),
            ("(GET)|(POST)", "GET", True),
            ("(GET)|(POST)", "POST", True),
            ("(GET)|(POST)", "PUT", False),
            ("(GET)|(POST)", "GETPOST", False),
            ("GET|POST", "POST", True),
            ("GET|POST", "XPOST", False),
            (".*", "DELETE", True),
        ],
    )
    def test_actions_anchored(
        self, helper_app: Flask, pattern: str, method: str, expected: bool
    ) -> None:
        """Action patterns match whole HTTP methods."""
        with helper_app.app_context(), locked_enforcer() as enforcer:
            enforcer.add_policy("*", "/x", pattern)
            assert enforcer.enforce("user:alice", "/x", method) is expected


class TestLoadPolicy:
    """Tests for loading stored policy rules."""

    def test_extra_fields_ignored(self, helper_app: Flask) -> None:
        """Extra fields of stored rules are ignored."""
        collection = helper_app.config["casbin_adapter"]._collection
        collection.insert_one(
            {
                "id": "1",
                "ptype": "p",
                "v0": "user:alice",
                "v1": "/items",
                "v2": "GET",
                "v5": None,
                "foo": "bar",
                "v": "x",
            }
        )
        with helper_app.app_context(), locked_enforcer() as enforcer:
            assert enforcer.get_policy() == [["user:alice", "/items", "GET"]]

    @pytest.mark.parametrize(
        "document",
        [
            {"ptype": "p", "v0": "user:a,/x", "v1": "/items", "v2": "GET"},
            {"ptype": "p", "v0": "#user:a", "v1": "/items", "v2": "GET"},
            {"ptype": "p", "v0": "user:a", "v1": "/items"},
            {"ptype": "p", "v0": "user:a", "v1": "/items", "v3": "GET"},
            {"ptype": "p", "v0": "user:a", "v1": "/items", "v2": "(GET"},
            {"ptype": "p", "v0": "user:a", "v1": "/items", "v2": 1},
            {"ptype": "p9", "v0": "user:a", "v1": "/items", "v2": "GET"},
            {"ptype": "g9", "v0": "user:a", "v1": "owner"},
            {"ptype": None, "v0": "user:a", "v1": "/items", "v2": "GET"},
            {"v0": "user:a", "v1": "/items", "v2": "GET"},
            {"ptype": "g2", "v0": "user:a", "v1": "owner"},
        ],
    )
    def test_invalid_rules_skipped(
        self,
        helper_app: Flask,
        caplog: pytest.LogCaptureFixture,
        document: dict[str, Any],
    ) -> None:
        """Invalid stored rules are skipped and logged."""
        collection = helper_app.config["casbin_adapter"]._collection
        collection.insert_one({"id": "invalid", **document})
        collection.insert_one(
            {"id": "2", "ptype": "p", "v0": "*", "v1": "/x", "v2": "GET"}
        )
        with helper_app.app_context(), locked_enforcer() as enforcer:
            assert enforcer.get_policy() == [["*", "/x", "GET"]]
            assert enforcer.get_named_grouping_policy("g2") == []
            assert enforcer.enforce("user:a", "/x", "GET")
        assert "Skipping invalid policy rule 'invalid'" in caplog.text

    def test_enforcer_loads_duplicates_once(self, helper_app: Flask) -> None:
        """Rules stored twice via the app's adapter are enforced once."""
        adapter = helper_app.config["casbin_adapter"]
        for _ in range(2):
            adapter.save_policy_line("g", ["user:alice", "admin"])
        with helper_app.app_context(), locked_enforcer() as enforcer:
            assert enforcer.get_grouping_policy() == [["user:alice", "admin"]]


class TestResourceRoles:
    """Tests for resource role helpers."""

    def test_add_list_remove(self, helper_app: Flask) -> None:
        """Resource roles are added, listed and removed."""
        with helper_app.app_context():
            assert add_resource_role("alice", "owner", "/items/1")
            assert add_resource_role("bob", "maintainer", "/items/1")
            assert add_resource_role("bob", "owner", "/items/2")
            # duplicates are not added
            assert not add_resource_role("alice", "owner", "/items/1")
            assert list_resource_roles("/items/1") == [
                ("alice", "owner"),
                ("bob", "maintainer"),
            ]
            assert remove_resource_role("bob", "maintainer", "/items/1")
            assert not remove_resource_role("bob", "maintainer", "/items/1")
            assert list_resource_roles("/items/1") == [("alice", "owner")]
            assert remove_resource("/items/1") is True
            assert list_resource_roles("/items/1") == []
            assert list_resource_roles("/items/2") == [("bob", "owner")]
            assert remove_resource("/items/1") is False

    def test_configured_rules_kept(
        self, helper_app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rules set via the configuration are not removed.

        They would be added again on the next start.
        """
        adapter = helper_app.config["casbin_adapter"]
        for subject, configured in [("user:alice", True), ("user:bob", False)]:
            adapter.save_policy_line(
                "g2", [subject, "owner", "/items/1"], configured=configured
            )
        with helper_app.app_context(), _capture_logs(caplog):
            assert remove_resource_role("alice", "owner", "/items/1") is False
            assert remove_resource("/items/1") is True
            assert list_resource_roles("/items/1") == [("alice", "owner")]
            assert remove_resource("/items/1") is False
        messages = _messages(caplog)
        assert len(messages) == 3
        assert all(
            "(g2, 'user:alice', 'owner', '/items/1')" in message
            and "access_control.policies" in message
            for message in messages
        )

    def test_remove_resource_unmarked_copy_of_configured_rule(
        self, helper_app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Removing an unmarked copy of a configured rule does not revoke the role.

        Hence, `False` is returned.
        """
        adapter = helper_app.config["casbin_adapter"]
        rule = ["user:alice", "owner", "/items/1"]
        adapter.save_policy_line("g2", rule, configured=True)
        adapter.save_policy_line("g2", rule)
        with helper_app.app_context(), _capture_logs(caplog):
            assert remove_resource("/items/1") is False
            assert list_resource_roles("/items/1") == [("alice", "owner")]
        assert adapter._collection.count_documents({}) == 1
        assert len(_messages(caplog)) == 1

    def test_remove_resource_does_not_load_policies(
        self, helper_app: Flask, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rules are removed without using (or loading) the enforcer."""
        with helper_app.app_context():
            add_resource_role("alice", "owner", "/items/1")
            monkeypatch.setattr(
                enforcement,
                "_get_enforcer_cache",
                mock.Mock(side_effect=AssertionError),
            )
            assert remove_resource("/items/1") is True

    def test_remove_resource_keeps_subpaths(self, helper_app: Flask) -> None:
        """Only rules on the resource path itself are removed."""
        with helper_app.app_context():
            add_resource_role("alice", "owner", "/items/1")
            add_resource_role("alice", "owner", "/items/1/files")
            assert remove_resource("/items/1") is True
            assert list_resource_roles("/items/1/files") == [("alice", "owner")]

    def test_roles_persisted(self, helper_app: Flask) -> None:
        """Resource roles are stored with prefixed subjects."""
        with helper_app.app_context():
            add_resource_role("alice", "owner", "/items/1")
            collection = helper_app.config["casbin_adapter"]._collection
            rule = collection.find_one({"ptype": "g2"})
            assert (rule["v0"], rule["v1"], rule["v2"]) == (
                "user:alice",
                "owner",
                "/items/1",
            )

    def test_list_unprefixed_subjects(self, helper_app: Flask) -> None:
        """Subjects without prefix are listed as is."""
        with helper_app.app_context():
            helper_app.config["casbin_adapter"].save_policy_line(
                ptype="g2", rule=["group:testers", "maintainer", "/items/1"]
            )
            add_resource_role("alice", "owner", "/items/1")
            assert list_resource_roles("/items/1") == [
                ("group:testers", "maintainer"),
                ("alice", "owner"),
            ]

    def test_add_remove_group_roles(self, helper_app: Flask) -> None:
        """Resource roles are granted to and revoked from groups."""
        with helper_app.app_context():
            assert add_resource_role(GROUP, "owner", "/items/1", subject_type="group")
            assert not add_resource_role(
                GROUP, "owner", "/items/1", subject_type="group"
            )
            with locked_enforcer() as enforcer:
                assert enforcer.get_named_grouping_policy("g2") == [
                    [f"group:{GROUP}", "owner", "/items/1"]
                ]
            assert remove_resource_role(
                GROUP, "owner", "/items/1", subject_type="group"
            )
            assert not remove_resource_role(
                GROUP, "owner", "/items/1", subject_type="group"
            )
            with locked_enforcer() as enforcer:
                assert enforcer.get_named_grouping_policy("g2") == []

    def test_list_group_roles(self, helper_app: Flask) -> None:
        """Resource roles are listed by subject type."""
        with helper_app.app_context():
            add_resource_role(GROUP, "owner", "/items/1", subject_type="group")
            add_resource_role("bob", "maintainer", "/items/1")
            assert list_resource_roles("/items/1", subject_type="group") == [
                (GROUP, "owner")
            ]
            assert list_resource_roles("/items/1", subject_type="user") == [
                ("bob", "maintainer")
            ]
            # without type, other subjects are listed as is
            assert list_resource_roles("/items/1") == [
                (f"group:{GROUP}", "owner"),
                ("bob", "maintainer"),
            ]
            assert remove_resource("/items/1")
            assert list_resource_roles("/items/1", subject_type="group") == []

    def test_users_and_groups_do_not_collide(self, helper_app: Flask) -> None:
        """Users and groups of the same name are distinct subjects."""
        with helper_app.app_context():
            assert add_resource_role(GROUP, "owner", "/items/1", subject_type="group")
            assert add_resource_role(GROUP, "maintainer", "/items/1")
            with locked_enforcer() as enforcer:
                assert enforcer.get_named_grouping_policy("g2") == [
                    [f"group:{GROUP}", "owner", "/items/1"],
                    [f"user:{GROUP}", "maintainer", "/items/1"],
                ]
            assert list_resource_roles("/items/1", subject_type="group") == [
                (GROUP, "owner")
            ]
            assert list_resource_roles("/items/1", subject_type="user") == [
                (GROUP, "maintainer")
            ]
            assert remove_resource_role(GROUP, "maintainer", "/items/1")
            assert list_resource_roles("/items/1", subject_type="group") == [
                (GROUP, "owner")
            ]

    @pytest.mark.parametrize(
        "role", ["group:auditors", "group:", "user:alice", "user:"]
    )
    @pytest.mark.parametrize("subject_type", ["user", "group"])
    def test_subject_like_roles(
        self, helper_app: Flask, role: str, subject_type: SubjectType
    ) -> None:
        """Roles cannot be subjects, so that their roles are not passed on.

        Existing rules with such roles can be revoked, though.
        """
        prefix = "user:" if subject_type == "user" else "group:"
        with helper_app.app_context():
            with pytest.raises(ValueError, match="must not start with"):
                add_resource_role("grace", role, "/items/5", subject_type=subject_type)
            with locked_enforcer() as enforcer:
                assert enforcer.get_named_grouping_policy("g2") == []
            helper_app.config["casbin_adapter"].save_policy_line(
                "g2", [f"{prefix}grace", role, "/items/5"]
            )
            assert remove_resource_role(
                "grace", role, "/items/5", subject_type=subject_type
            )
            with locked_enforcer() as enforcer:
                assert enforcer.get_named_grouping_policy("g2") == []

    def test_subject_like_roles_custom_prefix(self, helper_app: Flask) -> None:
        """Roles starting with a custom subject prefix are rejected, too."""
        get_app_config(helper_app).security.access_control.subject_prefix = "u/"
        with helper_app.app_context():
            for role in ["u/alice", "group:auditors"]:
                with pytest.raises(ValueError, match="must not start with"):
                    add_resource_role("grace", role, "/items/5")
                helper_app.config["casbin_adapter"].save_policy_line(
                    "g2", ["u/grace", role, "/items/5"]
                )
                assert remove_resource_role("grace", role, "/items/5")
            assert add_resource_role("grace", "user:alice", "/items/5")
            with locked_enforcer() as enforcer:
                assert enforcer.get_named_grouping_policy("g2") == [
                    ["u/grace", "user:alice", "/items/5"]
                ]

    @pytest.mark.parametrize(
        "group_mapping,keep_group_subjects,warned",
        [
            ({"a": "admin"}, False, True),
            ({"a": "admin"}, True, False),
            (None, False, False),
        ],
    )
    def test_group_role_without_group_subjects(
        self,
        helper_app: Flask,
        caplog: pytest.LogCaptureFixture,
        group_mapping: dict[str, str] | None,
        keep_group_subjects: bool,
        warned: bool,
    ) -> None:
        """Granting roles to groups that are not subjects is warned about."""
        conf = get_app_config(helper_app).security.access_control
        conf.group_mapping = group_mapping
        conf.keep_group_subjects = keep_group_subjects
        with helper_app.app_context(), caplog.at_level(logging.WARNING):
            assert add_resource_role("devs", "owner", "/items/1", subject_type="group")
            assert add_resource_role("devs", "maintainer", "/items/1")
        assert ("does not apply" in caplog.text) is warned

    def test_subject_like_roles_not_chained(self, helper_app: Flask) -> None:
        """Users cannot get the rights of groups via resource roles."""
        with helper_app.app_context():
            with locked_enforcer() as enforcer:
                enforcer.add_policy("group:auditors", "/items/:id", "GET")
            with pytest.raises(ValueError):
                add_resource_role("grace", "group:auditors", "/items/5")
            with locked_enforcer() as enforcer:
                assert not enforcer.enforce("user:grace", "/items/5", "GET")

    def test_list_by_subject_type_skips_other_subjects(self, helper_app: Flask) -> None:
        """Listing by subject type skips subjects of other types."""
        with helper_app.app_context():
            helper_app.config["casbin_adapter"].save_policy_line(
                ptype="g2", rule=["maintainer", "owner", "/items/1"]
            )
            subject_types: list[SubjectType] = ["user", "group"]
            for subject_type in subject_types:
                assert list_resource_roles("/items/1", subject_type=subject_type) == []
            assert list_resource_roles("/items/1") == [("maintainer", "owner")]

    @pytest.mark.parametrize(
        "subject,subject_type",
        [
            ("devs", "role"),
            # `None` is not a subject type for granting or revoking roles
            ("devs", None),
            ("a,b", "group"),
            ("a\n", "group"),
            ("a" * 1024, "group"),
        ],
    )
    def test_invalid_subjects(
        self, helper_app: Flask, subject: str, subject_type: str | None
    ) -> None:
        """Invalid subjects and subject types are rejected."""
        with helper_app.app_context():
            for helper in [add_resource_role, remove_resource_role]:
                with pytest.raises(ValueError):
                    helper(
                        subject,
                        "owner",
                        "/items/1",
                        subject_type=subject_type,  # type: ignore[arg-type]  # invalid on purpose
                    )
            with locked_enforcer() as enforcer:
                assert enforcer.get_named_grouping_policy("g2") == []

    @pytest.mark.parametrize(
        "subject_type,prefix", [("user", "user:"), ("group", "group:")]
    )
    def test_empty_subjects(
        self, helper_app: Flask, subject_type: SubjectType, prefix: str
    ) -> None:
        """Empty subjects cannot be granted roles, but can be revoked."""
        with helper_app.app_context():
            with pytest.raises(ValueError, match="must not be empty"):
                add_resource_role("", "owner", "/items/1", subject_type=subject_type)
            with locked_enforcer() as enforcer:
                assert enforcer.get_named_grouping_policy("g2") == []
            helper_app.config["casbin_adapter"].save_policy_line(
                "g2", [prefix, "owner", "/items/1"]
            )
            assert remove_resource_role(
                "", "owner", "/items/1", subject_type=subject_type
            )
            with locked_enforcer() as enforcer:
                assert enforcer.get_named_grouping_policy("g2") == []

    def test_duplicate_rules(self, helper_app: Flask) -> None:
        """Rules stored more than once are listed once and revoked completely.

        Such rules are stored, e.g., by concurrent requests.
        """
        adapter = helper_app.config["casbin_adapter"]
        for _ in range(2):
            adapter.save_policy_line("g2", ["user:alice", "owner", "/items/1"])
        adapter.save_policy_line("g2", ["user:bob", "owner", "/items/1"])
        with helper_app.app_context():
            for subject_type in [None, "user"]:
                assert list_resource_roles(
                    "/items/1",
                    subject_type=subject_type,  # type: ignore[arg-type]  # valid literals
                ) == [("alice", "owner"), ("bob", "owner")]
            assert remove_resource_role("alice", "owner", "/items/1")
            # policies are loaded anew, so a remaining copy would be listed
            with locked_enforcer() as enforcer:
                assert enforcer.get_named_grouping_policy("g2") == [
                    ["user:bob", "owner", "/items/1"]
                ]

    def test_list_invalid_subject_type(self, helper_app: Flask) -> None:
        """Invalid subject types are rejected for listing."""
        with helper_app.app_context(), pytest.raises(ValueError):
            list_resource_roles(
                "/items/1",
                subject_type="role",  # type: ignore[arg-type]  # invalid on purpose
            )

    def test_group_value_starting_with_hash(self, helper_app: Flask) -> None:
        """Group values may start with `#`."""
        with helper_app.app_context():
            assert add_resource_role("#devs", "owner", "/items/1", subject_type="group")
            assert list_resource_roles("/items/1", subject_type="group") == [
                ("#devs", "owner")
            ]

    def test_custom_subject_prefix(self, helper_app: Flask) -> None:
        """Users are stored with the configured subject prefix."""
        get_app_config(helper_app).security.access_control.subject_prefix = "u/"
        with helper_app.app_context():
            add_resource_role("alice", "owner", "/items/1")
            collection = helper_app.config["casbin_adapter"]._collection
            assert collection.find_one({"ptype": "g2"})["v0"] == "u/alice"
            assert list_resource_roles("/items/1") == [("alice", "owner")]

    def test_custom_ptype(self, helper_app: Flask, tmp_path: Path) -> None:
        """Resource roles may use other policy types."""
        model = tmp_path / "model.conf"
        model.write_text(
            Path(_model_path())
            .read_text()
            .replace("g2 = _, _, _", "g2 = _, _, _\ng3 = _, _, _")
        )
        helper_app.config["CASBIN_MODEL"] = str(model)
        with helper_app.app_context():
            assert add_resource_role("alice", "owner", "/items/1", ptype="g3")
            assert list_resource_roles("/items/1", ptype="g3") == [("alice", "owner")]
            assert remove_resource_role("alice", "owner", "/items/1", ptype="g3")
            assert not remove_resource("/items/1", ptype="g3")

    @pytest.mark.parametrize(
        "args",
        [
            ("alice,owner,/items/2", "owner", "/items/1"),
            ("alice\n", "owner", "/items/1"),
            ("alice", "#owner", "/items/1"),
            ("alice", "owner", "/items/1,/items/2"),
            ("alice", "", "/items/1"),
        ],
    )
    def test_invalid_values(
        self, helper_app: Flask, args: tuple[str, str, str]
    ) -> None:
        """Invalid values are rejected and not stored."""
        with helper_app.app_context():
            for helper in [add_resource_role, remove_resource_role]:
                with pytest.raises(ValueError):
                    helper(*args)
            collection = helper_app.config["casbin_adapter"]._collection
            assert collection.count_documents({}) == 0

    def test_save_rule_invalid_for_model(self, helper_app: Flask) -> None:
        """Rules invalid for the adapter's model are not stored."""
        adapter = Adapter(
            collection=mongomock.MongoClient()[DB_NAME][COLLECTION_NAME],
            model=_model_path(),
        )
        for ptype, rule in [
            ("p2", ["*", "/items", "GET"]),
            ("p", ["*", "/items"]),
            ("p", ["*", "/items", "(GET"]),
            ("g2", ["user:a", "owner"]),
        ]:
            with pytest.raises(ValueError):
                adapter.save_policy_line(ptype, rule)
        enforcer = Enforcer(_model_path(), adapter)
        with pytest.raises(ValueError):
            enforcer.add_policy("*", "/items", "(GET")
        assert adapter._collection.count_documents({}) == 0
        assert adapter.save_policy_line("p", ["*", "/items", "GET"])

    def test_save_invalid_rule(self, helper_app: Flask) -> None:
        """Invalid rules are not stored."""
        adapter = helper_app.config["casbin_adapter"]
        for ptype, rule in [
            ("g2", ["user:a,b", "owner", "/items/1"]),
            ("g2", ["user:a", None, "/items/1"]),
            ("x", ["user:a", "owner"]),
        ]:
            with pytest.raises(ValueError):
                adapter.save_policy_line(ptype, rule)
        assert adapter._collection.count_documents({}) == 0

    @pytest.mark.parametrize(
        "ptype,model_file",
        [
            # role definition without resource
            ("g", RESOURCE_ROLE_MODEL_FILE),
            # undefined role definition
            ("g3", RESOURCE_ROLE_MODEL_FILE),
            # `g2` defines object groups in default model
            ("g2", DEFAULT_MODEL_FILE),
        ],
    )
    def test_incompatible_model(
        self, helper_app: Flask, ptype: str, model_file: str
    ) -> None:
        """Helpers reject policy types without resource-scoped roles."""
        with resource_path(ACCESS_CONTROL_BASE_PATH, model_file) as _path:
            helper_app.config["CASBIN_MODEL"] = str(_path)
        with helper_app.app_context():
            helpers: list[tuple[Callable[..., Any], tuple[str, ...]]] = [
                (add_resource_role, ("alice", "owner", "/items/1")),
                (remove_resource_role, ("alice", "owner", "/items/1")),
                (list_resource_roles, ("/items/1",)),
                (remove_resource, ("/items/1",)),
            ]
            for helper, args in helpers:
                with pytest.raises(ValueError):
                    helper(*args, ptype=ptype)
            collection = helper_app.config["casbin_adapter"]._collection
            assert collection.count_documents({}) == 0
