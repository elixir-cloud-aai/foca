"""Tests for config-driven access control enforcement and helpers."""

from copy import deepcopy
from importlib.resources import path as resource_path
from pathlib import Path
from typing import ClassVar
from unittest import mock

import mongomock
import pytest
import yaml
from connexion.exceptions import Unauthorized
from connexion.resolver import Resolution, Resolver
from flask import Flask
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
from foca.security.access_control.enforcement import _is_within_path
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.register_access_control import (
    register_permission_specs,
)

DB_NAME = "access_control_db"
COLLECTION_NAME = "policy_rules"
ADMIN_ENTITLEMENT = "urn:example:group:admins"
THIS_MODULE = __name__

# Bearer tokens mapped to the claims of their (fake) validated JWTs
TOKENS: dict[str, dict] = {
    "alice": {"sub": "alice"},
    "bob": {"sub": "bob"},
    "carol": {"sub": "carol", "groups": [ADMIN_ENTITLEMENT]},
    "dave": {"sub": "dave"},
    "eve": {"sub": "eve", "groups": ["admin"]},
    "frank": {"sub": "frank", "groups": ["testers"]},
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
) = update_item_permissions = get_ref_item = get_optional = controller


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
            auth=AuthConfig(),
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
    adapter = Adapter(
        uri="mongodb://localhost:12345/",
        dbname=DB_NAME,
        collection=COLLECTION_NAME,
    )
    adapter._collection = collection
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


class TestPermissionsApi:
    """Tests for securing the permissions API."""

    url = "/admin/access-control/permissions"
    payload: ClassVar[dict] = {
        "policy_type": "p",
        "rule": {"v0": "dave", "v1": "/items/:id"},
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
        security=SecurityConfig(access_control=_access_control_config()),
    )
    adapter = Adapter(
        uri="mongodb://localhost:12345/",
        dbname=DB_NAME,
        collection=COLLECTION_NAME,
    )
    adapter._collection = mongomock.MongoClient()[DB_NAME][COLLECTION_NAME]
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

    def test_no_token_info(self, helper_app):
        ctx = self._context(helper_app, None)
        try:
            assert get_current_subject() is None
            assert get_current_groups() == []
        finally:
            ctx.pop()

    def test_outside_connexion(self, helper_app):
        with helper_app.test_request_context():
            assert get_current_subject() is None
            assert get_current_groups() == []

    def test_no_group_claim_configured(self, helper_app):
        helper_app.config.foca.security.access_control.claim_groups = None
        ctx = self._context(helper_app, mock_token_info("carol"))
        try:
            assert get_current_groups() == []
        finally:
            ctx.pop()


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
