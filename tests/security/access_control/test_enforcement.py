"""Tests for config-driven access control enforcement and helpers."""

from copy import deepcopy
from importlib.resources import path as resource_path
from pathlib import Path
from typing import ClassVar
from unittest import mock

import mongomock
import pytest
import yaml
from casbin import Enforcer
from connexion.exceptions import BadRequestProblem, Forbidden, Unauthorized
from connexion.resolver import Resolution, Resolver
from flask import Flask, request
from flask.globals import request_ctx

from foca.api.register_openapi import register_openapi
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
    _is_within_path,
    get_enforcer,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.register_access_control import (
    register_permission_specs,
)

DB_NAME = "access_control_db"
COLLECTION_NAME = "policy_rules"
ADMIN_ENTITLEMENT = "urn:example:group:admins"
GROUP = "urn:geant:example.org:group:team#example.org"
ISSUER = "https://login.example.org/oidc/"
THIS_MODULE = __name__

# Bearer tokens mapped to the claims of their (fake) validated JWTs
TOKENS: dict[str, dict] = {
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


def mock_token_info(token: str) -> dict:
    """Return token info or raise as FOCA's `validate_token()` does."""
    if token not in TOKENS:
        raise Unauthorized("JWT could not be validated")
    return {"jwt": token, "claims": TOKENS[token]}


def controller(*args, **kwargs) -> str:
    """Generic controller for the test API."""
    return "ok"


# Operations need distinct identifiers
list_items = create_item = get_item = update_item = delete_item = get_open = (
    get_item_permissions
) = get_ref_item = get_optional = controller


def update_item_permissions(id, *args, **kwargs) -> str:
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


def function_resolver(operation_id: str):
    """Callable operation resolver, as accepted by Connexion."""
    return controller


class DuckTypedResolver:
    """Operation resolver not inheriting from `Resolver`, as accepted by
    Connexion."""

    def resolve(self, operation) -> Resolution:
        return Resolution(controller, operation.operation_id)


def _operation(operation_id: str, **extra) -> dict:
    """Build an OpenAPI 3 Operation object."""
    operation = {
        "operationId": f"{THIS_MODULE}.{operation_id}",
        "responses": {
            "200": {
                "description": "OK",
                "content": {
                    "application/json": {"schema": {"type": "string"}}
                },
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
            "get": _operation(
                "get_optional", security=[{}, {"bearerAuth": []}]
            ),
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
    with resource_path(
        ACCESS_CONTROL_BASE_PATH, RESOURCE_ROLE_MODEL_FILE
    ) as _path:
        return str(_path)


def _access_control_config(**kwargs) -> AccessControlConfig:
    params = {
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
    connexion: dict | None = None,
):
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
        specs=[SpecConfig(path=spec_file, connexion=connexion)],
        enforce_access_control=access_control.enforce,
    )
    with mock.patch("foca.security.auth.validate_token", new=mock_token_info):
        cnx_app = register_permission_specs(
            app=cnx_app,
            access_control_config=access_control,
        )
    collection = mongomock.MongoClient()[DB_NAME][COLLECTION_NAME]
    config.db.dbs[DB_NAME].collections[  # type: ignore[union-attr,index]
        COLLECTION_NAME
    ].client = collection
    adapter = Adapter(collection=collection)
    for ptype, rule in POLICIES:
        adapter.save_policy_line(ptype=ptype, rule=rule)
    cnx_app.app.config["CASBIN_MODEL"] = access_control.model
    cnx_app.app.config["casbin_adapter"] = adapter
    return cnx_app.app


def _headers(user: str | None) -> dict:
    return {} if user is None else {"Authorization": f"Bearer {user}"}


@pytest.fixture
def app(tmp_path):
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
    def test_enforce(self, app, method, path, user, status):
        response = getattr(app.test_client(), method)(
            path, headers=_headers(user)
        )
        assert response.status_code == status

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
    def test_enforce_subpaths(self, app, method, path, user, status):
        response = getattr(app.test_client(), method)(
            path, headers=_headers(user)
        )
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
        self, tmp_path, method, path, user, status
    ):
        app = _create_app(
            tmp_path,
            _access_control_config(resource_roles_cover_subpaths=False),
        )
        response = getattr(app.test_client(), method)(
            path, headers=_headers(user)
        )
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
        self, app, method, path, user, status
    ):
        response = getattr(app.test_client(), method)(
            path, headers=_headers(user)
        )
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
        self, tmp_path, resolver, method, path, user, status
    ):
        app = _create_app(
            tmp_path,
            _access_control_config(),
            connexion={"resolver": resolver},
        )
        response = getattr(app.test_client(), method)(
            path, headers=_headers(user)
        )
        assert response.status_code == status

    def test_enforce_disabled(self, tmp_path):
        app = _create_app(tmp_path, _access_control_config(enforce=False))
        response = app.test_client().delete(
            "/items/1", headers=_headers("dave")
        )
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
        self, tmp_path, method, path, user, status
    ):
        app = _create_app(tmp_path, _access_control_config(group_mapping=None))
        response = getattr(app.test_client(), method)(
            path, headers=_headers(user)
        )
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
        self, tmp_path, method, path, user, status
    ):
        app = _create_app(
            tmp_path, _access_control_config(keep_group_subjects=True)
        )
        adapter = app.config["casbin_adapter"]
        adapter.save_policy_line(
            "g2", ["group:devs", "maintainer", "/items/2"]
        )
        adapter.save_policy_line("g2", ["user:devs", "owner", "/items/2"])
        response = getattr(app.test_client(), method)(
            path, headers=_headers(user)
        )
        assert response.status_code == status

    def test_enforce_group_subjects_disabled(self, tmp_path):
        app = _create_app(tmp_path, _access_control_config())
        adapter = app.config["casbin_adapter"]
        adapter.save_policy_line(
            "g2", ["group:devs", "maintainer", "/items/2"]
        )
        response = app.test_client().get("/items/2", headers=_headers("henry"))
        assert response.status_code == 403


class TestPermissionsApi:
    """Tests for securing the permissions API."""

    url = "/admin/access-control/permissions"
    payload: ClassVar[dict] = {
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
    def test_post_permission(self, app, user, status):
        response = app.test_client().post(
            self.url, json=self.payload, headers=_headers(user)
        )
        assert response.status_code == status

    def test_get_permissions_unauthorized(self, app):
        response = app.test_client().get(self.url)
        assert response.status_code == 401

    def test_permissions_api_enforced_if_enforce_disabled(self, tmp_path):
        app = _create_app(tmp_path, _access_control_config(enforce=False))
        response = app.test_client().post(
            self.url, json=self.payload, headers=_headers("dave")
        )
        assert response.status_code == 403

    def test_post_permission_applied(self, app):
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
    def test_invalid_permission(self, app, method, payload):
        url = self.url if method == "post" else f"{self.url}/abc"
        response = getattr(app.test_client(), method)(
            url, json=payload, headers=_headers("carol")
        )
        assert response.status_code == 400
        collection = app.config["casbin_adapter"]._collection
        assert collection.count_documents({}) == len(POLICIES)

    def test_get_incomplete_permissions(self, app):
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
        assert len(response.json["results"]) == len(POLICIES) + 1
        response = client.get(f"{self.url}/abc", headers=_headers("carol"))
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "path",
        ["/admin/access-control/ui/", "/admin/access-control/openapi.json"],
    )
    def test_ui_and_spec_not_served_by_default(self, app, path):
        assert app.test_client().get(path).status_code == 404

    def test_ui_and_spec_served(self, tmp_path):
        app = _create_app(
            tmp_path,
            _access_control_config(api_swagger_ui=True, api_serve_spec=True),
        )
        client = app.test_client()
        assert client.get("/admin/access-control/ui/").status_code == 200
        assert (
            client.get("/admin/access-control/openapi.json").status_code == 200
        )


class TestPagination:
    """Tests for listing permissions page by page."""

    url = "/admin/access-control/permissions"

    def _get(self, app, user="carol", **params):
        return app.test_client().get(
            self.url, query_string=params, headers=_headers(user)
        )

    def test_default(self, app):
        response = self._get(app)
        assert response.status_code == 200
        assert response.json["pagination"] == {
            "page": 0,
            "page_size": 100,
            "total": len(POLICIES),
            "total_pages": 1,
        }
        assert [
            [permission["policy_type"], *permission["rule"].values()]
            for permission in response.json["results"]
        ] == [[ptype, *rule] for ptype, rule in POLICIES]

    def test_all_pages(self, app):
        ids = []
        for page in range(3):
            response = self._get(app, page_size=3, page=page)
            assert response.status_code == 200
            assert response.json["pagination"] == {
                "page": page,
                "page_size": 3,
                "total": len(POLICIES),
                "total_pages": 3,
            }
            ids.extend(p["id"] for p in response.json["results"])
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
    def test_out_of_range(self, app, params):
        assert self._get(app, **params).status_code == 400

    def test_empty(self, app):
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
    def self_service_app(self, tmp_path):
        return _create_app(tmp_path, _access_control_config(self_service=True))

    @staticmethod
    def _id(app, **query) -> str:
        collection = app.config["casbin_adapter"]._collection
        return collection.find_one(query)["id"]

    def _list(self, app, user, **params):
        response = app.test_client().get(
            self.url, query_string=params, headers=_headers(user)
        )
        assert response.status_code == 200
        return [
            [p["policy_type"], *p["rule"].values()]
            for p in response.json["results"]
        ]

    def test_disabled(self, app):
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
    def test_list(self, self_service_app, user, expected):
        assert self._list(self_service_app, user) == expected

    def test_list_groups(self, tmp_path):
        app = _create_app(
            tmp_path,
            _access_control_config(self_service=True, group_mapping=None),
        )
        assert self._list(app, "frank") == [
            ["p", "*", "/items", "POST"],
            ["p", "group:testers", "/items/:id", "GET"],
        ]

    def test_list_group_subjects(self, tmp_path):
        app = _create_app(
            tmp_path,
            _access_control_config(
                self_service=True, keep_group_subjects=True
            ),
        )
        assert self._list(app, "frank") == [
            ["p", "*", "/items", "POST"],
            ["p", "group:testers", "/items/:id", "GET"],
        ]

    def test_list_paginated(self, self_service_app):
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

    def test_get(self, self_service_app):
        client = self_service_app.test_client()
        own = self._id(self_service_app, v0="user:alice")
        other = self._id(self_service_app, v0="user:bob")
        response = client.get(f"{self.url}/{own}", headers=_headers("alice"))
        assert response.status_code == 200
        assert response.json["rule"]["v0"] == "user:alice"
        response = client.get(f"{self.url}/{other}", headers=_headers("alice"))
        assert response.status_code == 404
        response = client.get(f"{self.url}/{other}", headers=_headers("carol"))
        assert response.status_code == 200

    def test_unauthenticated(self, self_service_app):
        response = self_service_app.test_client().get(self.url)
        assert response.status_code == 401

    def test_writes_enforced(self, self_service_app):
        client = self_service_app.test_client()
        own = self._id(self_service_app, v0="user:alice")
        payload = {
            "policy_type": "g",
            "rule": {"v0": "user:alice", "v1": "admin"},
        }
        headers = _headers("alice")
        assert (
            client.post(self.url, json=payload, headers=headers).status_code
            == 403
        )
        url = f"{self.url}/{own}"
        assert (
            client.put(url, json=payload, headers=headers).status_code == 403
        )
        assert client.delete(url, headers=headers).status_code == 403

    @pytest.mark.parametrize("self_service", [False, True])
    def test_owners_manage_access(self, tmp_path, self_service):
        """Owners grant roles on their resources via app endpoints."""
        app = _create_app(
            tmp_path, _access_control_config(self_service=self_service)
        )
        client = app.test_client()
        assert (
            client.get("/items/1", headers=_headers("dave")).status_code == 403
        )
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
        assert (
            client.get("/items/1", headers=_headers("dave")).status_code == 200
        )
        assert (
            client.delete("/items/1", headers=_headers("dave")).status_code
            == 403
        )

    @pytest.mark.parametrize("keep_group_subjects", [False, True])
    def test_owners_grant_roles_to_groups(self, tmp_path, keep_group_subjects):
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
            response = getattr(client, method)(
                "/items/5", headers=_headers("henry")
            )
            assert response.status_code == expected
        assert (
            client.delete("/items/5", headers=_headers("henry")).status_code
            == 403
        )


@pytest.mark.parametrize(
    "request_path,resource_path,expected",
    [
        ("/services/ABC", "/services/ABC", True),
        ("/services/ABC/permissions", "/services/ABC", True),
        ("/services/ABC/a/b", "/services/ABC", True),
        ("/services/ABC/", "/services/ABC", True),
        ("/services/ABC", "/services/ABC/", True),
        ("/services/ABC/permissions", "/services/ABC/", True),
        ("/services/ABCD", "/services/ABC", False),
        ("/services/AB", "/services/ABC", False),
        ("/services", "/services/ABC", False),
        ("/services/XYZ/permissions", "/services/ABC", False),
        # the root path only matches itself
        ("/", "/", True),
        ("/services/ABC", "/", False),
    ],
)
def test_is_within_path(request_path, resource_path, expected):
    assert _is_within_path(request_path, resource_path) is expected


@pytest.fixture
def helper_app():
    """Flask app with access control config and in-memory policies."""
    app = Flask(__name__)
    app.config.foca = Config(  # type: ignore[attr-defined]
        security=SecurityConfig(
            auth=AuthConfig(issuers=[ISSUER], audience=["foca"]),
            access_control=_access_control_config(),
        ),
    )
    adapter = Adapter(
        collection=mongomock.MongoClient()[DB_NAME][COLLECTION_NAME]
    )
    app.config["CASBIN_MODEL"] = _model_path()
    app.config["casbin_adapter"] = adapter
    return app


class TestCurrentSubject:
    """Tests for getting subject and groups from validated token claims."""

    def _context(self, app, token_info):
        ctx = app.test_request_context()
        ctx.push()
        request_ctx.connexion_context = (  # type: ignore[attr-defined]
            {} if token_info is None else {"token_info": token_info}
        )
        return ctx

    def test_subject(self, helper_app):
        ctx = self._context(helper_app, mock_token_info("carol"))
        try:
            assert get_current_subject() == "carol"
            assert get_current_groups() == ["admin"]
        finally:
            ctx.pop()

    def test_raw_claims(self, helper_app):
        ctx = self._context(helper_app, TOKENS["carol"])
        try:
            assert get_current_subject() == "carol"
        finally:
            ctx.pop()

    def test_string_group_claim(self, helper_app):
        ctx = self._context(
            helper_app, {"sub": "x", "groups": ADMIN_ENTITLEMENT}
        )
        try:
            assert get_current_groups() == ["admin"]
        finally:
            ctx.pop()

    def test_custom_identity_claim(self, helper_app):
        helper_app.config.foca.security.access_control.claim_identity = "id"
        ctx = self._context(helper_app, {"sub": "x", "id": "y"})
        try:
            assert get_current_subject() == "y"
        finally:
            ctx.pop()

    def test_identity_claim_from_auth_config(self, helper_app):
        helper_app.config.foca.security.auth.claim_identity = "email"
        ctx = self._context(helper_app, {"sub": "x", "email": "y"})
        try:
            assert get_current_subject() == "y"
        finally:
            ctx.pop()

    def test_identity_claim_overrides_auth_config(self, helper_app):
        helper_app.config.foca.security.auth.claim_identity = "email"
        helper_app.config.foca.security.access_control.claim_identity = "sub"
        ctx = self._context(helper_app, {"sub": "x", "email": "y"})
        try:
            assert get_current_subject() == "x"
        finally:
            ctx.pop()

    def test_invalid_group_claim(self, helper_app):
        ctx = self._context(helper_app, {"sub": "x", "groups": {"a": 1}})
        try:
            assert get_current_groups() == []
        finally:
            ctx.pop()

    def test_groups_without_mapping(self, helper_app):
        helper_app.config.foca.security.access_control.group_mapping = None
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
    def test_group_values(self, helper_app, values, expected):
        ctx = self._context(helper_app, {"sub": "x", "groups": values})
        try:
            assert get_current_group_values() == expected
        finally:
            ctx.pop()

    def test_group_values_without_claim(self, helper_app):
        ctx = self._context(helper_app, {"sub": "x"})
        try:
            assert get_current_group_values() == []
            assert get_current_groups() == []
        finally:
            ctx.pop()

    @pytest.mark.parametrize("group_mapping", [None, {"a": "maintainer"}])
    def test_group_subjects(self, helper_app, group_mapping):
        conf = helper_app.config.foca.security.access_control
        conf.keep_group_subjects = True
        if group_mapping is None:
            conf.group_mapping = None
        else:
            conf.group_mapping.update(group_mapping)
        ctx = self._context(
            helper_app,
            {"sub": "x", "groups": [ADMIN_ENTITLEMENT, "a", "b", "a"]},
        )
        try:
            mapped = [] if group_mapping is None else ["admin", "maintainer"]
            assert get_current_groups() == [
                *mapped,
                f"group:{ADMIN_ENTITLEMENT}",
                "group:a",
                "group:b",
            ]
        finally:
            ctx.pop()

    def test_mapped_groups_only(self, helper_app):
        ctx = self._context(
            helper_app, {"sub": "x", "groups": [ADMIN_ENTITLEMENT, "a"]}
        )
        try:
            assert get_current_groups() == ["admin"]
            assert get_current_group_values() == [ADMIN_ENTITLEMENT, "a"]
        finally:
            ctx.pop()

    def test_no_token_info(self, helper_app):
        ctx = self._context(helper_app, None)
        try:
            assert get_current_subject() is None
            assert get_current_groups() == []
            assert get_current_group_values() == []
        finally:
            ctx.pop()

    def test_outside_connexion(self, helper_app):
        with helper_app.test_request_context():
            assert get_current_subject() is None
            assert get_current_groups() == []

    def test_outside_request(self, helper_app):
        with helper_app.app_context():
            assert get_current_subject() is None
            assert get_current_groups() == []

    def test_invalid_connexion_context(self, helper_app):
        """Errors in claim retrieval are raised, not taken as no claims."""
        ctx = helper_app.test_request_context()
        ctx.push()
        request_ctx.connexion_context = "invalid"  # type: ignore[attr-defined]
        try:
            with pytest.raises(AttributeError):
                get_current_subject()
        finally:
            ctx.pop()

    def test_no_group_claim_configured(self, helper_app):
        helper_app.config.foca.security.access_control.claim_groups = None
        ctx = self._context(helper_app, mock_token_info("carol"))
        try:
            assert get_current_groups() == []
            assert get_current_group_values() == []
        finally:
            ctx.pop()


class TestEnforceAccessControlDecorator:
    """Tests for using `enforce_access_control` on single controllers."""

    @staticmethod
    def _call(app, method, path, token_info):
        @enforce_access_control
        def decorated():
            return "ok"

        with app.test_request_context(path, method=method):
            request_ctx.connexion_context = (  # type: ignore[attr-defined]
                {} if token_info is None else {"token_info": token_info}
            )
            return decorated()

    def test_allowed(self, helper_app):
        with helper_app.app_context():
            get_enforcer().add_policy("user:alice", "/items", "GET")
        token_info = mock_token_info("alice")
        assert self._call(helper_app, "GET", "/items", token_info) == "ok"

    def test_denied(self, helper_app):
        token_info = mock_token_info("alice")
        with pytest.raises(Forbidden):
            self._call(helper_app, "DELETE", "/items", token_info)

    def test_no_subject(self, helper_app):
        with pytest.raises(Unauthorized):
            self._call(helper_app, "GET", "/items", None)

    @pytest.mark.parametrize(
        "subject", ["alice,/items,GET", "alice\n", "a" * 1024]
    )
    def test_invalid_subject(self, helper_app, subject):
        with helper_app.app_context():
            get_enforcer().add_policy("*", "/items", "GET")
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
    def test_actions_anchored(self, helper_app, pattern, method, expected):
        with helper_app.app_context():
            enforcer = get_enforcer()
            enforcer.add_policy("*", "/x", pattern)
            assert enforcer.enforce("user:alice", "/x", method) is expected


class TestLoadPolicy:
    """Tests for loading stored policy rules."""

    def test_extra_fields_ignored(self, helper_app):
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
        with helper_app.app_context():
            assert get_enforcer().get_policy() == [
                ["user:alice", "/items", "GET"]
            ]

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
            {"ptype": None, "v0": "user:a", "v1": "/items", "v2": "GET"},
            {"v0": "user:a", "v1": "/items", "v2": "GET"},
            {"ptype": "g2", "v0": "user:a", "v1": "owner"},
        ],
    )
    def test_invalid_rules_skipped(self, helper_app, caplog, document):
        collection = helper_app.config["casbin_adapter"]._collection
        collection.insert_one({"id": "invalid", **document})
        collection.insert_one(
            {"id": "2", "ptype": "p", "v0": "*", "v1": "/x", "v2": "GET"}
        )
        with helper_app.app_context():
            enforcer = get_enforcer()
            assert enforcer.get_policy() == [["*", "/x", "GET"]]
            assert enforcer.get_named_grouping_policy("g2") == []
            assert enforcer.enforce("user:a", "/x", "GET")
        assert "Skipping invalid policy rule 'invalid'" in caplog.text

    def test_duplicates_loaded_once(self, helper_app):
        adapter = helper_app.config["casbin_adapter"]
        for _ in range(2):
            adapter.save_policy_line("g", ["user:alice", "admin"])
        with helper_app.app_context():
            assert get_enforcer().get_grouping_policy() == [
                ["user:alice", "admin"]
            ]


class TestResourceRoles:
    """Tests for resource role helpers."""

    def test_add_list_remove(self, helper_app):
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
            assert remove_resource("/items/1")
            assert list_resource_roles("/items/1") == []
            assert list_resource_roles("/items/2") == [("bob", "owner")]
            assert not remove_resource("/items/1")

    def test_roles_persisted(self, helper_app):
        with helper_app.app_context():
            add_resource_role("alice", "owner", "/items/1")
            collection = helper_app.config["casbin_adapter"]._collection
            rule = collection.find_one({"ptype": "g2"})
            assert (rule["v0"], rule["v1"], rule["v2"]) == (
                "user:alice",
                "owner",
                "/items/1",
            )

    def test_list_unprefixed_subjects(self, helper_app):
        with helper_app.app_context():
            helper_app.config["casbin_adapter"].save_policy_line(
                ptype="g2", rule=["group:testers", "maintainer", "/items/1"]
            )
            add_resource_role("alice", "owner", "/items/1")
            assert list_resource_roles("/items/1") == [
                ("group:testers", "maintainer"),
                ("alice", "owner"),
            ]

    def test_add_remove_group_roles(self, helper_app):
        with helper_app.app_context():
            assert add_resource_role(
                GROUP, "owner", "/items/1", subject_type="group"
            )
            assert not add_resource_role(
                GROUP, "owner", "/items/1", subject_type="group"
            )
            assert get_enforcer().get_named_grouping_policy("g2") == [
                [f"group:{GROUP}", "owner", "/items/1"]
            ]
            assert remove_resource_role(
                GROUP, "owner", "/items/1", subject_type="group"
            )
            assert not remove_resource_role(
                GROUP, "owner", "/items/1", subject_type="group"
            )
            assert get_enforcer().get_named_grouping_policy("g2") == []

    def test_list_group_roles(self, helper_app):
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

    def test_users_and_groups_do_not_collide(self, helper_app):
        with helper_app.app_context():
            assert add_resource_role(
                GROUP, "owner", "/items/1", subject_type="group"
            )
            assert add_resource_role(GROUP, "maintainer", "/items/1")
            assert get_enforcer().get_named_grouping_policy("g2") == [
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
    def test_subject_like_roles(self, helper_app, role, subject_type):
        """Roles cannot be subjects, so that their roles are not passed on."""
        with helper_app.app_context():
            for helper in [add_resource_role, remove_resource_role]:
                with pytest.raises(ValueError, match="must not start with"):
                    helper(
                        "grace", role, "/items/5", subject_type=subject_type
                    )
            assert get_enforcer().get_named_grouping_policy("g2") == []

    def test_subject_like_roles_custom_prefix(self, helper_app):
        helper_app.config.foca.security.access_control.subject_prefix = "u/"
        with helper_app.app_context():
            with pytest.raises(ValueError):
                add_resource_role("grace", "u/alice", "/items/5")
            assert add_resource_role("grace", "user:alice", "/items/5")

    def test_subject_like_roles_not_chained(self, helper_app):
        """Users cannot get the rights of groups via resource roles."""
        with helper_app.app_context():
            enforcer = get_enforcer()
            enforcer.add_policy("group:auditors", "/items/:id", "GET")
            with pytest.raises(ValueError):
                add_resource_role("grace", "group:auditors", "/items/5")
            assert not get_enforcer().enforce("user:grace", "/items/5", "GET")

    def test_list_by_subject_type_skips_other_subjects(self, helper_app):
        with helper_app.app_context():
            helper_app.config["casbin_adapter"].save_policy_line(
                ptype="g2", rule=["maintainer", "owner", "/items/1"]
            )
            for subject_type in ["user", "group"]:
                assert (
                    list_resource_roles("/items/1", subject_type=subject_type)
                    == []
                )
            assert list_resource_roles("/items/1") == [("maintainer", "owner")]

    @pytest.mark.parametrize(
        "subject,subject_type",
        [
            ("devs", "role"),
            # `None` is not a subject type for granting or revoking roles
            ("devs", None),
            ("", "user"),
            ("", "group"),
            ("a,b", "group"),
            ("a\n", "group"),
            ("a" * 1024, "group"),
        ],
    )
    def test_invalid_subjects(self, helper_app, subject, subject_type):
        with helper_app.app_context():
            for helper in [add_resource_role, remove_resource_role]:
                with pytest.raises(ValueError):
                    helper(
                        subject,
                        "owner",
                        "/items/1",
                        subject_type=subject_type,  # type: ignore[arg-type]
                    )
            assert get_enforcer().get_named_grouping_policy("g2") == []

    def test_list_invalid_subject_type(self, helper_app):
        with helper_app.app_context(), pytest.raises(ValueError):
            list_resource_roles(
                "/items/1",
                subject_type="role",  # type: ignore[arg-type]
            )

    def test_group_value_starting_with_hash(self, helper_app):
        with helper_app.app_context():
            assert add_resource_role(
                "#devs", "owner", "/items/1", subject_type="group"
            )
            assert list_resource_roles("/items/1", subject_type="group") == [
                ("#devs", "owner")
            ]

    def test_custom_subject_prefix(self, helper_app):
        helper_app.config.foca.security.access_control.subject_prefix = "u/"
        with helper_app.app_context():
            add_resource_role("alice", "owner", "/items/1")
            collection = helper_app.config["casbin_adapter"]._collection
            assert collection.find_one({"ptype": "g2"})["v0"] == "u/alice"
            assert list_resource_roles("/items/1") == [("alice", "owner")]

    def test_custom_ptype(self, helper_app, tmp_path):
        model = tmp_path / "model.conf"
        model.write_text(
            Path(_model_path())
            .read_text()
            .replace("g2 = _, _, _", "g2 = _, _, _\ng3 = _, _, _")
        )
        helper_app.config["CASBIN_MODEL"] = str(model)
        with helper_app.app_context():
            assert add_resource_role("alice", "owner", "/items/1", ptype="g3")
            assert list_resource_roles("/items/1", ptype="g3") == [
                ("alice", "owner")
            ]
            assert remove_resource_role(
                "alice", "owner", "/items/1", ptype="g3"
            )
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
    def test_invalid_values(self, helper_app, args):
        with helper_app.app_context():
            for helper in [add_resource_role, remove_resource_role]:
                with pytest.raises(ValueError):
                    helper(*args)
            collection = helper_app.config["casbin_adapter"]._collection
            assert collection.count_documents({}) == 0

    def test_save_rule_invalid_for_model(self, helper_app):
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

    def test_save_invalid_rule(self, helper_app):
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
    def test_incompatible_model(self, helper_app, ptype, model_file):
        with resource_path(ACCESS_CONTROL_BASE_PATH, model_file) as _path:
            helper_app.config["CASBIN_MODEL"] = str(_path)
        with helper_app.app_context():
            for helper, args in [
                (add_resource_role, ("alice", "owner", "/items/1")),
                (remove_resource_role, ("alice", "owner", "/items/1")),
                (list_resource_roles, ("/items/1",)),
                (remove_resource, ("/items/1",)),
            ]:
                with pytest.raises(ValueError):
                    helper(*args, ptype=ptype)
            collection = helper_app.config["casbin_adapter"]._collection
            assert collection.count_documents({}) == 0
