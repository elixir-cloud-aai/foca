"""Tests for the public helpers for paths and decisions on other paths."""

import logging
from collections.abc import Iterator
from importlib.resources import path as resource_path
from pathlib import Path
from typing import Any
from unittest import mock

import mongomock
import pytest
import yaml
from flask import Flask, g, jsonify
from pydantic import ValidationError

import foca as foca_module
from foca.api.register_openapi import register_openapi
from foca.api.routes import Route, _add_route
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (
    AccessControlConfig,
    AuthConfig,
    Config,
    MongoConfig,
    SecurityConfig,
    ServiceClientConfig,
    SpecConfig,
)
from foca.security.access_control import is_permitted_for
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.register_access_control import (
    _expand_base_path,
    register_access_control,
)
from foca.security.auth import _unauthorized
from foca.utils import urls
from foca.utils.urls import (
    _RegisteredApi,
    get_api_base_path,
    get_resource_path,
)

DEFAULT_PERMISSIONS_SPEC = (
    Path(foca_module.__file__).parent
    / "security"
    / "access_control"
    / "api"
    / "access-control-specs.yaml"
)
DB_NAME = "access_control_db"
COLLECTION_NAME = "policy_rules"
ISSUER = "https://login.example.org/oidc/"
THIS_MODULE = __name__
ADMIN_GROUP = "urn:example:admins"

TOKENS: dict[str, dict[str, Any]] = {
    "alice": {"sub": "alice", "iss": ISSUER},
    "bob": {"sub": "bob", "iss": ISSUER},
    "admin": {"sub": "root", "iss": ISSUER, "groups": [ADMIN_GROUP]},
    "client": {"sub": "svc", "client_id": "svc", "iss": ISSUER},
    "no-subject": {"iss": ISSUER},
}


def mock_token_info(token: str) -> dict[str, Any]:
    """Return token info or raise as FOCA's `validate_token()` does."""
    if token not in TOKENS:
        raise _unauthorized("JWT could not be validated")
    return {"jwt": token, "claims": TOKENS[token]}


def controller(*args: Any, **kwargs: Any) -> Any:
    """Controller evaluating the check that the test asks for."""
    result = g.get("check", _no_check)()
    return jsonify(result)


def _no_check() -> None:
    """Default of `controller`."""


# Operations need distinct identifiers
list_pets = get_pet = delete_pet = get_open = get_public = other_list = controller
get_other = list_second = get_ref = get_unvalidated = controller


def _operation(operation_id: str, **extra: Any) -> dict[str, Any]:
    """Build an OpenAPI 3 Operation object."""
    return {
        "operationId": f"{THIS_MODULE}.{operation_id}",
        "responses": {
            "200": {"description": "OK", "content": {"application/json": {}}}
        },
        **extra,
    }


ID_PARAMETER = {
    "name": "id",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
}


def _spec(url: str | None, *paths: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
    """Build an OpenAPI 3 specification from its routes.

    Args:
        url: URL of its server, if any.
        paths: Path, HTTP method and Operation Object of each operation.

    Returns:
        The specification.
    """
    spec_paths: dict[str, Any] = {}
    for path, method, operation in paths:
        item = spec_paths.setdefault(path, {})
        if "{id}" in path:
            item["parameters"] = [ID_PARAMETER]
        item[method] = operation
    spec: dict[str, Any] = {
        "openapi": "3.0.2",
        "info": {"title": "Test API", "version": "1.0.0"},
        "security": [{"bearerAuth": []}],
        "paths": spec_paths,
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
    if url is not None:
        spec["servers"] = [{"url": url}]
    return spec


PETS_SPEC = _spec(
    "/api/v1",
    ("/pets", "get", _operation("list_pets")),
    ("/pets/{id}", "get", _operation("get_pet")),
    ("/pets/{id}", "delete", _operation("delete_pet")),
    ("/open", "get", _operation("get_open", **{"x-foca-access-control": False})),
    ("/public", "get", _operation("get_public", security=[{"bearerAuth": []}, {}])),
    ("/unvalidated", "get", _operation("get_unvalidated", security=[])),
)
OTHER_SPEC = _spec(
    "/other/v2",
    ("/others", "get", _operation("other_list")),
    ("/others/{id}", "get", _operation("get_other")),
)


def _create_app(
    tmp_path: Path,
    specs: list[dict[str, Any]],
    policies: list[list[str]] | dict[str, list[str]] | None = None,
    connexion: dict[str, Any] | None = None,
    **options: Any,
) -> Flask:
    """Create app with APIs, permissions API and in-memory policies.

    Args:
        tmp_path: Directory for the specifications.
        specs: API specifications.
        policies: Configured policy rules.
        connexion: Options for registering the specifications with Connexion.
        **options: Other settings of access control.

    Returns:
        Flask app.
    """
    with resource_path(ACCESS_CONTROL_BASE_PATH, RESOURCE_ROLE_MODEL_FILE) as path:
        model = str(path)
    access_control = AccessControlConfig(
        enabled=True,
        enforce=True,
        db_name=DB_NAME,
        collection_name=COLLECTION_NAME,
        model=model,
        claim_groups="groups",
        group_mapping={ADMIN_GROUP: "admin"},
        policies=policies,  # type: ignore[arg-type]  # validated
        **options,
    )
    config = Config(
        security=SecurityConfig(
            auth=AuthConfig(
                issuers=[ISSUER],
                audience=["foca"],
                service_clients=[ServiceClientConfig(client_id="svc")],
            ),
            access_control=access_control,
        ),
        db=MongoConfig(),
    )
    spec_configs = []
    for index, spec in enumerate(specs):
        spec_file = tmp_path / f"spec{index}.yaml"
        spec_file.write_text(yaml.safe_dump(spec))
        spec_configs.append(
            SpecConfig.model_validate({"path": spec_file, "connexion": connexion})
        )
    cnx_app = create_connexion_app(config)
    cnx_app = register_exception_handler(cnx_app)
    cnx_app = register_openapi(
        app=cnx_app,
        specs=spec_configs,
        enforce_access_control=True,
    )
    client: mongomock.MongoClient[dict[str, Any]] = mongomock.MongoClient()
    with (
        mock.patch(
            "foca.security.access_control.register_access_control.get_mongo_client",
            return_value=client,
        ),
        mock.patch("foca.security.auth.validate_token", new=mock_token_info),
    ):
        cnx_app = register_access_control(
            cnx_app=cnx_app,
            mongo_config=config.db,
            access_control_config=access_control,
        )
    return cnx_app.app


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Flask]:
    """App with one API and rules for alice."""
    yield _create_app(
        tmp_path,
        [PETS_SPEC],
        policies=[
            ["p", "user:alice", "/api/v1/pets/*", "GET"],
            ["p", "client:svc", "/api/v1/pets/1", "DELETE"],
        ],
    )


def _request(
    app: Flask, token: str | None, check: Any, path: str = "/api/v1/public"
) -> Any:
    """Evaluate a check in a controller, for a caller with a token.

    Args:
        app: Flask app.
        token: Bearer token; `None` for an anonymous caller.
        check: Function to evaluate.
        path: Path of the request.

    Returns:
        JSON response of the controller.
    """
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    original_preprocess = app.preprocess_request

    def _with_check() -> Any:
        g.check = check
        return original_preprocess()

    with mock.patch.object(app, "preprocess_request", _with_check):
        response = app.test_client().get(path, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


class TestIsPermittedFor:
    """Tests of `is_permitted_for()`."""

    @pytest.mark.parametrize(
        ("token", "path", "method", "expected"),
        [
            # permitted: rule for the user, also with lower-case method
            ("alice", "/api/v1/pets/1", "GET", True),
            ("alice", "/api/v1/pets/1", "get", True),
            # denied: no rule for the method or the user
            ("alice", "/api/v1/pets/1", "DELETE", False),
            ("bob", "/api/v1/pets/1", "GET", False),
            # admins may do everything in the model
            ("admin", "/api/v1/pets/1", "DELETE", True),
            # service clients are identified by their client
            ("client", "/api/v1/pets/1", "DELETE", True),
            ("client", "/api/v1/pets/2", "DELETE", False),
            # tokens without identity and anonymous callers have no decision
            ("no-subject", "/api/v1/pets/1", "GET", False),
            (None, "/api/v1/pets/1", "GET", False),
            # routes that don't enforce policies
            ("bob", "/api/v1/open", "GET", True),
            (None, "/api/v1/public", "GET", True),
            # no such route, method or path form
            ("alice", "/api/v1/nothing", "GET", False),
            ("alice", "/api/v1/pets/1", "POST", False),
            ("alice", "/api/v1/pets/1/", "GET", False),
        ],
    )
    def test_decisions(
        self,
        app: Flask,
        token: str | None,
        path: str,
        method: str,
        expected: bool,
    ) -> None:
        """Decisions follow the route and the policies, for the current caller."""
        assert _request(app, token, lambda: is_permitted_for(path, method)) is expected

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/api/v1/public", ""),
            ("api/v1/public", "GET"),
            ("//api/v1/public", "GET"),
        ],
    )
    def test_malformed(self, app: Flask, path: str, method: str) -> None:
        """Arguments that no request would have are not permitted."""
        with app.app_context():
            assert is_permitted_for(path, method) is False

    def test_public_operation_has_anonymous_caller(self, app: Flask) -> None:
        """Public operations don't validate tokens, so there is no caller."""
        result = _request(
            app,
            "alice",
            lambda: is_permitted_for("/api/v1/pets/1"),
            path="/api/v1/unvalidated",
        )
        assert result is False

    def test_catch_all_route(self, tmp_path: Path) -> None:
        """Connexion's route for authenticating all paths serves nothing."""
        app = _create_app(tmp_path, [PETS_SPEC], connexion={"auth_all_paths": True})
        with app.app_context():
            assert is_permitted_for("/api/v1/nothing") is False
            assert is_permitted_for("/api/v1/public") is True

    def test_denial_logged_as_debug(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No request was denied, so denials aren't logged as for requests."""
        logger = "foca.security.access_control.enforcement"
        with caplog.at_level(logging.DEBUG, logger=logger):
            result = _request(app, "bob", lambda: is_permitted_for("/api/v1/pets/1"))
        assert result is False
        levels = {
            record.levelno
            for record in caplog.records
            if record.name == logger and "bob" in record.getMessage()
        }
        assert levels == {logging.DEBUG}

    def test_automatic_options_permitted(self, app: Flask) -> None:
        """Flask answers these itself, so no policy applies."""
        assert (
            _request(app, "bob", lambda: is_permitted_for("/api/v1/pets/1", "OPTIONS"))
            is True
        )

    def test_default_method(self, app: Flask) -> None:
        """The method defaults to `GET`."""
        assert (
            _request(app, "alice", lambda: is_permitted_for("/api/v1/pets/1")) is True
        )

    def test_no_request(self, app: Flask) -> None:
        """Without a request, there is no caller."""
        with app.app_context():
            assert is_permitted_for("/api/v1/pets/1") is False
            assert is_permitted_for("/api/v1/public") is True

    def test_redirect(self, app: Flask) -> None:
        """Paths that Flask would redirect aren't permitted."""
        app.add_url_rule("/plain/", view_func=controller)
        _add_route(
            app,
            Route("GET", "/plain/", "/plain/", None, None, {}, controller),
        )
        with app.app_context():
            assert is_permitted_for("/plain") is False
            assert is_permitted_for("/plain/") is True

    def test_invalid_subject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Subjects that aren't valid rule values are denied."""
        app = _create_app(tmp_path, [PETS_SPEC])
        monkeypatch.setitem(TOKENS, "bad", {"sub": "a\nb", "iss": ISSUER})
        assert _request(app, "bad", lambda: is_permitted_for("/api/v1/pets/1")) is False

    def test_does_not_change_decision_of_request(self, app: Flask) -> None:
        """The decision is taken for the path passed, not the request's."""
        decisions = _request(
            app,
            "alice",
            lambda: [
                is_permitted_for("/api/v1/pets/1"),
                is_permitted_for("/api/v1/pets/1", "DELETE"),
            ],
            path="/api/v1/pets/1",
        )
        assert decisions == [True, False]


class TestGetApiBasePath:
    """Tests of `get_api_base_path()`."""

    def test_single_api_on_startup(self, tmp_path: Path) -> None:
        """Without a request, the only API of the app is used."""
        app = _create_app(tmp_path, [PETS_SPEC])
        with app.app_context():
            assert get_api_base_path() == "/api/v1"

    def test_request(self, tmp_path: Path) -> None:
        """During a request, it's the base path of the API handling it."""
        app = _create_app(tmp_path, [PETS_SPEC, OTHER_SPEC])
        assert _request(app, None, get_api_base_path) == "/api/v1"
        # another API; an enforced route, for a caller allowed by the model
        result = _request(app, "admin", get_api_base_path, path="/other/v2/others")
        assert result == "/other/v2"

    def test_several_apis_without_request(self, tmp_path: Path) -> None:
        """Without a request, several APIs need an operation to tell them apart."""
        app = _create_app(tmp_path, [PETS_SPEC, OTHER_SPEC])
        with (
            app.app_context(),
            pytest.raises(RuntimeError, match="several APIs"),
        ):
            get_api_base_path()

    def test_operation(self, tmp_path: Path) -> None:
        """Operations identify an API, also during requests to another one."""
        app = _create_app(tmp_path, [PETS_SPEC, OTHER_SPEC])
        with app.app_context():
            assert get_api_base_path(f"{THIS_MODULE}.get_other") == "/other/v2"
            assert get_api_base_path(f"{THIS_MODULE}.get_pet") == "/api/v1"
        assert (
            _request(app, None, lambda: get_api_base_path(f"{THIS_MODULE}.get_other"))
            == "/other/v2"
        )

    def test_operation_of_referenced_path_item(self, tmp_path: Path) -> None:
        """Operations of path items given via references count, too."""
        spec = _spec("/ref/v1")
        spec["paths"]["/ref"] = {"$ref": "#/x-path-items/ref"}
        spec["x-path-items"] = {"ref": {"get": _operation("get_ref")}}
        app = _create_app(tmp_path, [PETS_SPEC, spec])
        with app.app_context():
            assert get_api_base_path(f"{THIS_MODULE}.get_ref") == "/ref/v1"

    def test_operation_of_several_apis(self, tmp_path: Path) -> None:
        """An operation of several APIs needs a request to one of them."""
        operation = f"{THIS_MODULE}.get_other"
        second = _spec(
            "/second/v3",
            (
                "/others",
                "get",
                _operation(
                    "get_other", **{"x-foca-access-control": False}, security=[]
                ),
            ),
        )
        app = _create_app(tmp_path, [OTHER_SPEC, second, PETS_SPEC])

        def check() -> str:
            try:
                return get_api_base_path(operation)
            except RuntimeError as exc:
                return str(exc)

        with app.app_context():
            assert "Several APIs" in check()
        assert _request(app, None, check, path="/second/v3/others") == "/second/v3"
        assert "Several APIs" in _request(app, None, check)

    def test_unknown_operation(self, tmp_path: Path) -> None:
        """Unknown operations are errors."""
        app = _create_app(tmp_path, [PETS_SPEC])
        with app.app_context(), pytest.raises(LookupError, match="'nope'"):
            get_api_base_path("nope")

    def test_no_api(self, tmp_path: Path) -> None:
        """Apps without API specifications have no base path."""
        app = _create_app(tmp_path, [])
        with app.app_context(), pytest.raises(RuntimeError, match="no API"):
            get_api_base_path()

    def test_app_without_registered_apis(self) -> None:
        """Flask apps that FOCA didn't set up have no base path."""
        app = Flask(__name__)
        with app.app_context(), pytest.raises(RuntimeError, match="no API"):
            get_api_base_path()

    def test_root_api(self, tmp_path: Path) -> None:
        """An API at the root has an empty base path, so that paths can follow."""
        app = _create_app(tmp_path, [_spec("/", ("/pets", "get", PETS_OPERATION))])
        with app.app_context():
            assert get_api_base_path() == ""
            assert get_resource_path("pets") == "/pets"

    def test_spec_without_servers(self, tmp_path: Path) -> None:
        """Without servers, the API is at the root."""
        app = _create_app(tmp_path, [_spec(None, ("/pets", "get", PETS_OPERATION))])
        with app.app_context():
            assert get_api_base_path() == ""

    def test_absolute_server_url(self, tmp_path: Path) -> None:
        """Only the path of the server URL counts."""
        spec = _spec("https://api.example.org/a/b/", ("/pets", "get", PETS_OPERATION))
        app = _create_app(tmp_path, [spec])
        with app.app_context():
            assert get_api_base_path() == "/a/b"

    def test_permissions_api_request(self, tmp_path: Path) -> None:
        """The permissions API isn't an API of the app, but can handle requests."""
        app = _create_app(tmp_path, [PETS_SPEC])
        original = app.view_functions
        endpoint = next(
            rule.endpoint
            for rule in app.url_map.iter_rules()
            if rule.rule == "/admin/access-control/permissions"
            and "GET" in (rule.methods or set())
        )
        results = []
        wrapped = original[endpoint]

        def _spy(*args: Any, **kwargs: Any) -> Any:
            results.append(get_api_base_path())
            return wrapped(*args, **kwargs)

        original[endpoint] = _spy
        response = app.test_client().get(
            "/admin/access-control/permissions",
            headers={"Authorization": "Bearer admin"},
        )
        assert response.status_code == 200
        assert results == ["/admin/access-control"]

    @pytest.mark.parametrize("inner_operation", ["list_second", "get_other"])
    def test_nested_base_paths(self, tmp_path: Path, inner_operation: str) -> None:
        """Requests go by the API of their operation, not the longest base path.

        Also if both APIs have the operation.
        """
        other = _operation("get_other", **{"x-foca-access-control": False}, security=[])
        outer = _spec("/nest", ("/v2/others", "get", other))
        inner = _spec("/nest/v2", ("/pets", "get", _operation(inner_operation)))
        app = _create_app(tmp_path, [outer, inner])
        assert _request(app, None, get_api_base_path, path="/nest/v2/others") == "/nest"

    @pytest.mark.parametrize(
        ("base_path", "expected"),
        [(None, None), ("/a", "/a"), ("/a/b", "/a/b"), ("/other", None)],
    )
    def test_api_of_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        base_path: str | None,
        expected: str | None,
    ) -> None:
        """The API of a request is that of its route, if registered."""
        apis = [
            _RegisteredApi("/a", frozenset({"op_a"}), is_app_api=True),
            _RegisteredApi("/a/b", frozenset({"op_b"}), is_app_api=True),
        ]
        found = (
            None
            if base_path is None
            else mock.Mock(route=mock.Mock(base_path=base_path))
        )
        monkeypatch.setattr(urls, "_match_request", lambda: found)
        assert urls._get_request_base_path(apis) == expected

    def test_request_outside_apis(self, tmp_path: Path) -> None:
        """Routes outside the APIs fall back to the API of the app."""
        app = _create_app(tmp_path, [PETS_SPEC])
        app.add_url_rule("/plain", view_func=lambda: jsonify(get_api_base_path()))
        response = app.test_client().get("/plain")
        assert response.get_json() == "/api/v1"


PETS_OPERATION = _operation("list_second")


class TestGetResourcePath:
    """Tests of `get_resource_path()`."""

    def test_segments(self, tmp_path: Path) -> None:
        """Segments follow the base path of the API."""
        app = _create_app(tmp_path, [PETS_SPEC])
        with app.app_context():
            assert get_resource_path("pets") == "/api/v1/pets"
            assert get_resource_path("pets", "abc") == "/api/v1/pets/abc"

    def test_decoded(self, tmp_path: Path) -> None:
        """Paths aren't encoded, as request paths in policies aren't."""
        app = _create_app(tmp_path, [PETS_SPEC])
        with app.app_context():
            assert get_resource_path("pets", "a b") == "/api/v1/pets/a b"

    def test_operation(self, tmp_path: Path) -> None:
        """The API may be identified by an operation."""
        app = _create_app(tmp_path, [PETS_SPEC, OTHER_SPEC])
        with app.app_context():
            assert (
                get_resource_path(
                    "others", "1", operation_id=f"{THIS_MODULE}.get_other"
                )
                == "/other/v2/others/1"
            )

    def test_matches_route(self, app: Flask) -> None:
        """The path is the one that policies and routes use."""
        path = _request(app, "alice", lambda: get_resource_path("pets", "1"))
        assert path == "/api/v1/pets/1"
        with app.app_context():
            assert is_permitted_for(get_resource_path("public")) is True

    @pytest.mark.parametrize(
        "segments",
        [
            (),
            ("",),
            ("pets", ""),
            ("pets/1",),
            (".",),
            ("pets", ".."),
            ("pets", "*"),
            ("pets", ":id"),
        ],
    )
    def test_invalid_segments(self, tmp_path: Path, segments: tuple[str, ...]) -> None:
        """Segments that would address other paths are errors."""
        app = _create_app(tmp_path, [PETS_SPEC])
        with app.app_context(), pytest.raises(ValueError, match="segment"):
            get_resource_path(*segments)


class TestBasePathPlaceholder:
    """Tests of `{base_path}` in configured policies."""

    def test_expanded(self, tmp_path: Path) -> None:
        """The placeholder is replaced by the base path of the API."""
        app = _create_app(
            tmp_path,
            [PETS_SPEC],
            policies=[
                ["p", "user:alice", "{base_path}/pets/*", "GET"],
                ["g2", "user:bob", "owner", "{base_path}/pets/1"],
            ],
        )
        adapter: Adapter = app.config["casbin_adapter"]
        stored = [
            [doc["ptype"], *[doc[f"v{i}"] for i in range(3) if f"v{i}" in doc]]
            for doc in adapter._collection.find({"configured": True})
        ]
        assert sorted(stored) == [
            ["g2", "user:bob", "owner", "/api/v1/pets/1"],
            ["p", "user:alice", "/api/v1/pets/*", "GET"],
        ]
        assert (
            _request(app, "alice", lambda: is_permitted_for("/api/v1/pets/1")) is True
        )

    def test_each_api(self, tmp_path: Path) -> None:
        """Rules with the placeholder apply to each API of the app."""
        app = _create_app(
            tmp_path,
            [PETS_SPEC, OTHER_SPEC],
            policies=[["p", "user:alice", "{base_path}/*", "GET"]],
        )
        adapter: Adapter = app.config["casbin_adapter"]
        stored = sorted(
            doc["v1"] for doc in adapter._collection.find({"configured": True})
        )
        assert stored == ["/api/v1/*", "/other/v2/*"]

    def test_permissions_api_not_covered(self, tmp_path: Path) -> None:
        """The permissions API is not covered by the placeholder."""
        app = _create_app(
            tmp_path,
            [PETS_SPEC],
            policies=[["p", "user:alice", "{base_path}/*", "GET"]],
        )
        assert (
            _request(app, "alice", lambda: is_permitted_for("/api/v1/pets/1")) is True
        )
        assert (
            _request(
                app,
                "alice",
                lambda: is_permitted_for("/admin/access-control/permissions"),
            )
            is False
        )

    def test_no_api(self, tmp_path: Path) -> None:
        """Rules with the placeholder need an API."""
        with pytest.raises(ValueError, match="base_path"):
            _create_app(
                tmp_path,
                [],
                policies=[["p", "user:alice", "{base_path}/*", "GET"]],
            )

    def test_rules_without_placeholder(self, tmp_path: Path) -> None:
        """Other rules are stored as they are, also without any API."""
        app = _create_app(tmp_path, [], policies=[["p", "user:alice", "/plain", "GET"]])
        adapter: Adapter = app.config["casbin_adapter"]
        stored = [doc["v1"] for doc in adapter._collection.find({"configured": True})]
        assert stored == ["/plain"]

    def test_root_api_catch_all_refused(self, tmp_path: Path) -> None:
        """Rules for an API at the root don't open up the permissions API."""
        spec = _spec("/", ("/pets", "get", PETS_OPERATION))
        with pytest.raises(ValueError, match="permissions API"):
            _create_app(
                tmp_path,
                [spec],
                policies=[["p", "*", "{base_path}/*", "(GET)|(POST)"]],
            )

    @pytest.mark.parametrize(
        "value",
        [
            "{base_path}/mgmt/*",
            "{base_path}/mgmt/:a",
            "{base_path}/mgmt/abc",
            "{base_path}/mgmt",
            "{base_path}/*",
        ],
    )
    def test_protected_paths_of_spec(self, value: str) -> None:
        """All paths of the permissions API are protected, and each item."""
        with pytest.raises(ValueError, match=r"permissions API \('/mgmt"):
            _expand_base_path(["p", "*", value, "GET"], [""], ["/mgmt", "/mgmt/{item}"])

    def test_root_api_specific_rule(self, tmp_path: Path) -> None:
        """Rules for paths of an API at the root are fine."""
        spec = _spec("/", ("/pets", "get", PETS_OPERATION))
        app = _create_app(
            tmp_path, [spec], policies=[["p", "*", "{base_path}/pets", "GET"]]
        )
        adapter: Adapter = app.config["casbin_adapter"]
        stored = [doc["v1"] for doc in adapter._collection.find({"configured": True})]
        assert stored == ["/pets"]

    def test_root_api_only_placeholder(self, tmp_path: Path) -> None:
        """A value of only the placeholder is the root of an API at the root."""
        spec = _spec("/", ("/pets", "get", PETS_OPERATION))
        app = _create_app(tmp_path, [spec], policies=[["p", "*", "{base_path}", "GET"]])
        adapter: Adapter = app.config["casbin_adapter"]
        stored = [doc["v1"] for doc in adapter._collection.find({"configured": True})]
        assert stored == ["/"]

    def test_no_api_named_rule(self, tmp_path: Path) -> None:
        """Errors name the rule, if rules are named."""
        with pytest.raises(ValueError, match=r"'my-rule'.*base_path"):
            _create_app(
                tmp_path,
                [],
                policies={"my-rule": ["p", "user:alice", "{base_path}/*", "GET"]},
            )

    def test_invalid_expanded_rule(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Expanded rules are validated, too."""
        caplog.set_level(logging.INFO)
        with pytest.raises(ValueError, match="values"):
            _create_app(tmp_path, [PETS_SPEC], policies=[["p", "{base_path}"]])


class TestPermissionsApiSwitch:
    """Tests of `api_enabled` and `api_base_path`."""

    def test_default(self, tmp_path: Path) -> None:
        """The permissions API is at its default base path."""
        app = _create_app(tmp_path, [PETS_SPEC])
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        assert "/admin/access-control/permissions" in rules

    def test_disabled(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Without the API, its paths are gone, and policies are still enforced."""
        caplog.set_level(logging.INFO)
        app = _create_app(
            tmp_path,
            [PETS_SPEC],
            policies=[["p", "user:alice", "/api/v1/pets/*", "GET"]],
            api_enabled=False,
            api_swagger_ui=True,
        )
        assert "Permission management API not registered" in caplog.text
        assert app.test_client().get("/admin/access-control/ui/").status_code == 404
        response = app.test_client().get(
            "/admin/access-control/permissions",
            headers={"Authorization": "Bearer admin"},
        )
        assert response.status_code == 404
        assert (
            _request(app, "alice", lambda: is_permitted_for("/api/v1/pets/1")) is True
        )
        assert _request(app, "bob", lambda: is_permitted_for("/api/v1/pets/1")) is False
        assert "api_specs" not in caplog.text

    def test_disabled_with_specs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A custom specification without the API is pointed out."""
        _create_app(tmp_path, [PETS_SPEC], api_enabled=False, api_specs="spec.yaml")
        assert "'security.access_control.api_specs' has no effect" in caplog.text

    def test_base_path(self, tmp_path: Path) -> None:
        """The base path moves the API, which is still enforced."""
        app = _create_app(tmp_path, [PETS_SPEC], api_base_path="/internal/roles")
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        assert "/internal/roles/permissions" in rules
        assert "/admin/access-control/permissions" not in rules
        assert (
            app.test_client()
            .get("/internal/roles/permissions", headers={"Authorization": "Bearer bob"})
            .status_code
            == 403
        )

    def test_disabled_with_self_service(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Self-service without the API is pointed out."""
        _create_app(tmp_path, [PETS_SPEC], api_enabled=False, self_service=True)
        assert "'security.access_control.self_service' has no effect" in caplog.text

    def test_base_path_of_app_api(self, tmp_path: Path) -> None:
        """The base path of one of the app's APIs is refused, naming the setting."""
        with pytest.raises(ValueError, match="api_base_path' must differ"):
            _create_app(tmp_path, [PETS_SPEC], api_base_path="/api/v1")

    def test_base_path_under_app_api(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A base path under one of the app's APIs is pointed out."""
        _create_app(tmp_path, [PETS_SPEC], api_base_path="/api/v1/admin")
        assert "overlaps with the base path of an API of the app" in caplog.text

    def test_base_path_above_app_api(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A base path above one of the app's APIs is pointed out."""
        _create_app(tmp_path, [PETS_SPEC], api_base_path="/api")
        assert "overlaps with the base path of an API of the app" in caplog.text

    def test_base_path_outside_app_api(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Neither other paths nor similar ones nor the root API are a conflict."""
        _create_app(
            tmp_path,
            [PETS_SPEC, _spec("/", ("/pets", "get", _operation("list_pets")))],
            api_base_path="/api/v1.admin",
        )
        assert "overlaps with" not in caplog.text

    @pytest.mark.parametrize(
        "rule",
        [
            ["p", "*", "/api/v1/*", "(GET)|(POST)"],
            ["p", "ops", "/api/v1/admin/permissions", "GET"],
            ["p", "ops", "/api/v1/admin/permissions/:id", "GET"],
            ["p", "*", "*", "(GET)|(POST)"],
            ["p", "*", "(/.*)", "GET"],
        ],
    )
    def test_literal_rule_matches_api(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, rule: list[str]
    ) -> None:
        """Literal rules matching the API are pointed out, but may be deliberate."""
        _create_app(
            tmp_path, [PETS_SPEC], policies=[rule], api_base_path="/api/v1/admin"
        )
        assert f"Policy rule {rule!r} matches the permissions API" in caplog.text

    def test_literal_rule_other_paths(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Other literal rules aren't."""
        _create_app(
            tmp_path,
            [PETS_SPEC],
            policies=[
                ["p", "*", "/api/v1/pets/*", "GET"],
                ["g", "user:alice", "ops"],
                # Admins have access anyway
                ["p", "admin", "/*", "GET"],
                # Gives no access by itself
                ["g2", "user:alice", "owner", "/admin/access-control/permissions"],
                # Not a valid pattern
                ["p", "ops", "/pets(", "GET"],
            ],
        )
        assert "matches the permissions API" not in caplog.text

    def test_rule_for_default_path(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rules for the default path of a moved API are pointed out."""
        rule = ["p", "ops", "/admin/access-control/permissions", "GET"]
        _create_app(
            tmp_path, [PETS_SPEC], policies=[rule], api_base_path="/internal/roles"
        )
        assert "names the default path of the permissions API" in caplog.text

    def test_rule_for_default_path_below(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Moved below the default path, rules may still apply."""
        rule = ["p", "ops", "/admin/access-control/*", "GET"]
        _create_app(
            tmp_path,
            [PETS_SPEC],
            policies=[rule],
            api_base_path="/admin/access-control/v2",
        )
        assert "names the default path" not in caplog.text

    @pytest.mark.parametrize(
        "options",
        [
            {},
            {"api_enabled": False},
            {"api_base_path": "/internal/roles", "api_enabled": False},
        ],
    )
    def test_rule_for_default_path_applies(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        options: dict[str, Any],
    ) -> None:
        """Without a moved API, there is nothing to point out."""
        rule = ["p", "ops", "/admin/access-control/permissions", "GET"]
        _create_app(tmp_path, [PETS_SPEC], policies=[rule], **options)
        assert "names the default path" not in caplog.text

    def test_rule_for_default_path_custom_spec(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A custom specification decides where the API is."""
        spec_file = tmp_path / "permissions.yaml"
        spec_file.write_text(DEFAULT_PERMISSIONS_SPEC.read_text())
        rule = ["p", "ops", "/admin/access-control/permissions", "GET"]
        _create_app(
            tmp_path,
            [PETS_SPEC],
            policies=[rule],
            api_specs=str(spec_file),
            api_base_path="/ignored",
        )
        assert "names the default path" not in caplog.text

    def test_base_path_protected(self, tmp_path: Path) -> None:
        """Rules with the placeholder must not match the moved API."""
        with pytest.raises(ValueError, match="permissions API"):
            _create_app(
                tmp_path,
                [_spec("/", ("/pets", "get", _operation("list_pets")))],
                policies=[["p", "*", "{base_path}/internal/*", "GET"]],
                api_base_path="/internal",
            )

    def test_custom_spec_wins(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The base path of a custom specification wins, with a warning."""
        spec = yaml.safe_load(DEFAULT_PERMISSIONS_SPEC.read_text())
        spec["servers"] = [{"url": "/custom"}]
        spec_file = tmp_path / "permissions.yaml"
        spec_file.write_text(yaml.safe_dump(spec))
        app = _create_app(
            tmp_path,
            [PETS_SPEC],
            api_specs=str(spec_file),
            api_base_path="/ignored",
        )
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        assert "/custom/permissions" in rules
        assert "/ignored/permissions" not in rules
        assert "'security.access_control.api_base_path' has no effect" in caplog.text

    def test_custom_spec_no_warning_by_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A custom specification alone isn't a conflict."""
        spec_file = tmp_path / "permissions.yaml"
        spec_file.write_text(DEFAULT_PERMISSIONS_SPEC.read_text())
        _create_app(tmp_path, [PETS_SPEC], api_specs=str(spec_file))
        assert "api_base_path" not in caplog.text

    @pytest.mark.parametrize(
        "value",
        ["", "/", "relative", "/trailing/", "//double", "/a/../b", "/a/.", "/a b"],
    )
    def test_invalid_base_path(self, value: str) -> None:
        """Base paths that can't serve as URL prefixes are refused."""
        with pytest.raises(ValidationError, match="api_base_path"):
            AccessControlConfig(api_base_path=value)

    def test_valid_base_path(self) -> None:
        """Paths of several segments are fine."""
        path = "/a-b/c_d/e.f"
        assert AccessControlConfig(api_base_path=path).api_base_path == path
