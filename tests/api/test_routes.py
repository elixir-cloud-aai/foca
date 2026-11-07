"""Tests for `foca.api.routes`."""

from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import pytest
from connexion import App
from connexion.resolver import Resolver
from flask import Flask, request
from werkzeug.routing import Map

from foca.api.register_openapi import register_openapi
from foca.api.routes import _install_route_table, get_routes, match
from foca.api.service_info import _routes_to_controller
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import SpecConfig
from foca.security.access_control.enforcement import enforce_access_control
from foca.security.access_control.ownership_server import (
    _check_routes_enforced,
    _require_enforcement,
)
from foca.testing import request_context

SERVICE_INFO = "foca.api.service_info.getServiceInfo"
OWNERSHIP = "foca.security.access_control.ownership_server"


def view_a() -> str:
    """Serve an operation."""
    return "a"


def view_b() -> str:
    """Serve an operation."""
    return "b"


def view_c() -> str:
    """Serve an operation."""
    return "c"


def view_d() -> str:
    """Serve an operation."""
    return "d"


def view_e() -> str:
    """Serve an operation."""
    return "e"


class _OtherResolver(Resolver):
    """Resolver of operations without identifiers to `view_e()`."""

    def resolve_operation_id(self, operation: Any) -> str:
        """Resolve the identifier of an operation.

        Args:
            operation: Connexion operation.

        Returns:
            Its identifier, or that of `view_e()` if it has none.
        """
        return operation.operation_id or f"{__name__}.view_e"


def _operation(operation_id: str, **fields: Any) -> dict[str, Any]:
    """Build an Operation Object.

    Args:
        operation_id: Identifier of the operation.
        **fields: Other fields, with underscores for hyphens.

    Returns:
        Operation Object.
    """
    return {
        "operationId": operation_id,
        "responses": {"200": {"description": "OK"}},
        **{key.replace("_", "-"): value for key, value in fields.items()},
    }


def _parameter(name: str, schema_type: str = "string") -> dict[str, Any]:
    """Build a path parameter.

    Args:
        name: Name of the parameter.
        schema_type: Type of the parameter.

    Returns:
        Parameter Object.
    """
    return {
        "name": name,
        "in": "path",
        "required": True,
        "schema": {"type": schema_type},
    }


def _spec(base_path: str = "/api/v1") -> dict[str, Any]:
    """Build a specification.

    Args:
        base_path: Base path of the API.

    Returns:
        OpenAPI 3 specification.
    """
    return {
        "openapi": "3.0.3",
        "info": {"title": "Test", "version": "1"},
        "servers": [{"url": base_path}],
        "paths": {
            "/items": {
                "get": _operation(f"{__name__}.view_a", x_foca_marker="m"),
                "options": _operation(f"{__name__}.view_b"),
            },
            "/items/{item-id}/{count}": {
                "parameters": [_parameter("item-id"), _parameter("count", "integer")],
                "post": _operation(f"{__name__}.view_c"),
            },
            "/slash/": {"get": _operation(f"{__name__}.view_d")},
            "/other": {"get": _operation(f"{__name__}.view_e")},
        },
    }


@pytest.fixture
def cnx_app() -> App:
    """Create a Connexion app with an API."""
    app = create_connexion_app()
    app.add_api(_spec())
    return app


class TestGetRoutes:
    """Tests for `get_routes()`."""

    def test_no_table(self) -> None:
        """Apps without routes have none."""
        assert get_routes(Flask(__name__)) == []

    def test_fields(self, cnx_app: App) -> None:
        """Operations are recorded with their base paths."""
        routes = {(r.method, r.path): r for r in get_routes(cnx_app.app)}
        route = routes[("POST", "/api/v1/items/{item-id}/{count}")]
        assert route.rule == "/api/v1/items/<item_id>/<int:count>"
        assert route.base_path == "/api/v1"
        assert route.operation_id == f"{__name__}.view_c"
        assert route.function is view_c
        assert routes[("GET", "/api/v1/items")].extensions == {"x-foca-marker": "m"}

    def test_operation_id_resolved(self) -> None:
        """Operations without identifiers go by the resolved ones."""
        spec = _spec()
        del spec["paths"]["/other"]["get"]["operationId"]
        app = create_connexion_app()
        app.add_api(spec, resolver=_OtherResolver())
        routes = {(r.method, r.path): r for r in get_routes(app.app)}
        assert routes[("GET", "/api/v1/other")].operation_id == f"{__name__}.view_e"
        assert routes[("GET", "/api/v1/items")].operation_id == f"{__name__}.view_a"

    def test_security(self) -> None:
        """Effective security requirements are recorded."""
        spec = _spec()
        spec["security"] = [{"key": []}]
        spec["components"] = {
            "securitySchemes": {
                "key": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Key",
                    "x-apikeyInfoFunc": f"{__name__}.view_a",
                }
            }
        }
        app = create_connexion_app()
        app.add_api(spec)
        assert get_routes(app.app)[0].security == [{"key": []}]

    @pytest.mark.parametrize("base_path", ["/", "/api/v1", "/api/v1/"])
    def test_same_as_flask(self, base_path: str) -> None:
        """Rules and methods are those of Flask, whatever the base path."""
        app = create_connexion_app()
        app.add_api(_spec(base_path))
        flask_rules = {
            (rule.rule, method)
            for rule in app.app.url_map.iter_rules()
            if "view_" in rule.endpoint
            for method in rule.methods or ()
            if method not in ("HEAD", "OPTIONS")
        } | {(r.rule, "OPTIONS") for r in get_routes(app.app) if r.method == "OPTIONS"}
        assert {(r.rule, r.method) for r in get_routes(app.app)} == flask_rules

    def test_unresolved_operation(self) -> None:
        """Operations that can't be resolved are routed to Connexion's handler."""
        spec = _spec()
        spec["paths"]["/other"]["get"]["operationId"] = "no.such.function"
        app = create_connexion_app()
        app.add_api(spec, resolver_error=501)
        routes = {route.path: route for route in get_routes(app.app)}
        assert routes["/api/v1/other"].function is not view_e


class TestMatch:
    """Tests for `match()`."""

    def test_current_app(self, cnx_app: App) -> None:
        """The current app is the default."""
        with cnx_app.app.app_context():
            found = match("/api/v1/items", "GET")
        assert found is not None
        assert found.route.path == "/api/v1/items"
        assert found.params == {}

    def test_params(self, cnx_app: App) -> None:
        """Path parameters are converted to their types, with the names of the rule."""
        found = match("/api/v1/items/a b/3", "post", cnx_app.app)
        assert found is not None
        assert found.params == {"item_id": "a b", "count": 3}

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/items", "GET"),
            ("/api/v1/items", "DELETE"),
            ("/api/v1/items", ""),
            ("api/v1/items", "GET"),
            ("/api/v1/items/a/not-a-number", "POST"),
            ("/api/v1/slash", "GET"),
            ("//api/v1/items", "GET"),
            ("/root/api/v1/items", "GET"),
        ],
    )
    def test_no_match(self, cnx_app: App, path: str, method: str) -> None:
        """Other paths and methods, and those that redirect, have no route."""
        assert match(path, method, cnx_app.app) is None

    def test_url_map_settings(self) -> None:
        """Settings of an app for slashes apply, as for its requests."""
        app = create_connexion_app()
        app.app.url_map.strict_slashes = False
        app.add_api(_spec())
        assert match("/api/v1/slash", "GET", app.app) is not None

    def test_no_table(self) -> None:
        """Apps without routes match nothing."""
        assert match("/", "GET", Flask(__name__)) is None

    def test_head_of_get(self, cnx_app: App) -> None:
        """`HEAD` finds the route of `GET`, like requests."""
        found = match("/api/v1/other", "HEAD", cnx_app.app)
        assert found is not None
        assert found.route.method == "GET"

    def test_automatic_options(self, cnx_app: App) -> None:
        """`OPTIONS` requests that answer by themselves reach no operation."""
        assert match("/api/v1/other", "OPTIONS", cnx_app.app) is None

    def test_root_path(self, cnx_app: App) -> None:
        """Request paths exclude the root path of the app."""
        flask_app = cnx_app.app
        with request_context("/api/v1/items", root_path="/root", app=flask_app):
            assert request.root_path == "/root"
            found = match(request.path, "GET")
        assert found is not None
        assert found.route.path == "/api/v1/items"

    @pytest.mark.parametrize(
        ("path", "script_name"),
        [("/api/v1/items/x/1", ""), ("/api/v1/items/a%20b/1", "/root")],
    )
    def test_same_as_request(self, cnx_app: App, path: str, script_name: str) -> None:
        """A request reaches the operation that `match()` finds."""
        with request_context(
            path,
            method="POST",
            root_path=script_name,
            app=cnx_app,
        ):
            found = match(request.path, request.method)
            assert found is not None
            assert request.url_rule is not None
            assert found.route.rule == request.url_rule.rule
            assert found.params == request.view_args


class TestInstall:
    """Tests for `_install_route_table()`."""

    def test_plain_app(self) -> None:
        """Apps not made by FOCA's factory get a table on registering specs."""
        app = App(__name__)
        with mock.patch("foca.api.register_openapi.ConfigParser.merge_yaml") as parsed:
            parsed.return_value = _spec()
            register_openapi(app, [SpecConfig(path=Path("/spec.yaml"))])
        assert len(get_routes(app.app)) == 5

    def test_once(self, cnx_app: App) -> None:
        """Installing again doesn't record operations twice."""
        _install_route_table(cnx_app)
        cnx_app.add_api(_spec("/again"))
        assert len(get_routes(cnx_app.app)) == 10

    def test_stand_in(self) -> None:
        """Other objects are left alone."""
        stand_in = MagicMock()
        api_cls = stand_in.api_cls
        _install_route_table(stand_in)
        assert stand_in.api_cls is api_cls


class TestWithoutFlaskRoutes:
    """The checks don't depend on Flask's routes having been built."""

    def test_unenforced_ownership_route(self, cnx_app: App) -> None:
        """Unenforced routes to ownership controllers are still found."""
        spec = _spec("/permissions")
        spec["paths"] = {
            "/roles/{id}": {
                "parameters": [_parameter("id")],
                "get": _operation(f"{OWNERSHIP}.getResourceRoles"),
            }
        }
        cnx_app.add_api(spec)
        cnx_app.app.url_map = Map()
        with pytest.raises(ValueError, match=r"GET /permissions/roles/<id>"):
            _check_routes_enforced(cnx_app.app)

    def test_service_info_route(self, cnx_app: App) -> None:
        """Routes to the controller of the service info are still found."""
        spec = _spec("/info")
        spec["paths"] = {"/service-info": {"get": _operation(SERVICE_INFO)}}
        cnx_app.add_api(spec)
        cnx_app.app.url_map = Map()
        assert _routes_to_controller(cnx_app.app) == ["/info/service-info"]


class TestEnforcementMark:
    """The route found must have enforced policies for the request."""

    def test_route_outside_of_table_with_same_path(self, cnx_app: App) -> None:
        """A route the table doesn't know can't borrow the enforcement of another."""
        spec = _spec("/own")
        spec["paths"] = {
            "/{id}": {
                "parameters": [_parameter("id")],
                "get": _operation(f"{__name__}.view_a"),
            }
        }
        table_app = create_connexion_app()
        with mock.patch(f"{__name__}.view_a", enforce_access_control(view_a)):
            table_app.add_api(spec)
        guarded = _require_enforcement(view_b)
        flask_app = table_app.app
        flask_app.add_url_rule("/own/special", view_func=guarded)
        with (
            request_context("/own/special", app=flask_app),
            pytest.raises(RuntimeError, match="not enforced"),
        ):
            guarded()
