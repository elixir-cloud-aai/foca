"""Tests for the registration of OpenAPI specifications with a Connexion app."""

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from connexion import App
from connexion.exceptions import InvalidSpecification
from pydantic import ValidationError
from yaml import YAMLError

from foca.api import register_openapi as module
from foca.api.register_openapi import (
    _connexion_add_api_kwargs,
    _iter_operations,
    register_openapi,
)
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, ConnexionOptions, SpecConfig

# Define mock data
DIR = Path(__file__).parents[1].resolve() / "test_files"
PATH_SPECS_2_YAML_ORIGINAL = DIR / "openapi_2_petstore.original.yaml"
PATH_SPECS_2_JSON_ORIGINAL = DIR / "openapi_2_petstore.original.json"
PATH_SPECS_2_YAML_ADDITION = DIR / "openapi_2_petstore.addition.yaml"
PATH_SPECS_3_YAML_ORIGINAL = DIR / "openapi_3_petstore.original.yaml"
PATH_SPECS_3_PATHITEMPARAM_YAML_ORIGINAL = (
    DIR / "openapi_3_petstore_pathitemparam.original.yaml"
)
PATH_SPECS_INVALID_JSON = DIR / "invalid.json"
PATH_SPECS_INVALID_YAML = DIR / "invalid.openapi.yaml"
PATH_NOT_FOUND = DIR / "does/not/exist.yaml"
OPERATION_FIELDS_2 = {"x-swagger-router-controller": "controllers"}
OPERATION_FIELDS_2_NO_RESOLVE = {"x-swagger-router-controller": YAMLError}
OPERATION_FIELDS_3 = {"x-openapi-router-controller": "controllers"}
SECURITY_FIELDS_2 = {"x-apikeyInfoFunc": "controllers.validate_token"}
SECURITY_FIELDS_3 = {"x-bearerInfoFunc": "controllers.validate_token"}
APPEND = {
    "info": {
        "version": "1.0.0",
        "title": "Swagger Petstore",
        "license": {"name": "MIT"},
    }
}
CONNEXION_CONFIG: dict[str, Any] = {
    "strict_validation": True,
    "validate_responses": False,
    "options": {
        "swagger_ui": True,
        "serve_spec": True,
    },
}
SPEC_CONFIG_2: dict[str, Any] = {
    "path": PATH_SPECS_2_YAML_ORIGINAL,
    "append": [APPEND],
    "add_operation_fields": OPERATION_FIELDS_2,
    "add_security_fields": SECURITY_FIELDS_2,
    "disable_auth": False,
    "connexion": CONNEXION_CONFIG,
}
SPEC_CONFIG_3: dict[str, Any] = {
    "path": PATH_SPECS_3_YAML_ORIGINAL,
    "append": [APPEND],
    "add_operation_fields": OPERATION_FIELDS_3,
    "add_security_fields": SECURITY_FIELDS_3,
    "disable_auth": False,
    "connexion": CONNEXION_CONFIG,
}
SPEC_CONFIG_3_PATHITEMPARAM: dict[str, Any] = {
    "path": PATH_SPECS_3_PATHITEMPARAM_YAML_ORIGINAL,
    "append": [APPEND],
    "add_operation_fields": OPERATION_FIELDS_3,
    "add_security_fields": SECURITY_FIELDS_3,
    "disable_auth": False,
    "connexion": CONNEXION_CONFIG,
}
SPEC_CONFIG_2_JSON = deepcopy(SPEC_CONFIG_2)
SPEC_CONFIG_2_JSON["path"] = PATH_SPECS_2_JSON_ORIGINAL
SPEC_CONFIG_2_LIST = deepcopy(SPEC_CONFIG_2)
SPEC_CONFIG_2_LIST["path"] = [PATH_SPECS_2_YAML_ORIGINAL]
SPEC_CONFIG_2_MULTI = deepcopy(SPEC_CONFIG_2_LIST)
SPEC_CONFIG_2_MULTI["path"].append(PATH_SPECS_2_YAML_ADDITION)
SPEC_CONFIG_2_DISABLE_AUTH = deepcopy(SPEC_CONFIG_2)
SPEC_CONFIG_2_DISABLE_AUTH["disable_auth"] = True
SPEC_CONFIG_3_DISABLE_AUTH = deepcopy(SPEC_CONFIG_3)
SPEC_CONFIG_3_DISABLE_AUTH["disable_auth"] = True


def _routes(app: App) -> set[str]:
    """Methods and paths of the routes registered with an app."""
    return {
        f"{method} {rule.rule}"
        for rule in app.app.url_map.iter_rules()
        for method in rule.methods or set()
        if method not in {"HEAD", "OPTIONS"}
    }


class TestRegisterOpenAPI:
    """Tests for `register_openapi()`."""

    def test_openapi_2_yaml(self) -> None:
        """Successfully register OpenAPI 2 YAML specs with Connexion app."""
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_2)]
        res = register_openapi(app=app, specs=spec_configs)
        assert _routes(res) >= {
            "GET /v1/pets",
            "POST /v1/pets",
            "GET /v1/pets/<petId>",
        }

    def test_openapi_3_yaml(self) -> None:
        """Successfully register OpenAPI 3 YAML specs with Connexion app."""
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_3)]
        res = register_openapi(app=app, specs=spec_configs)
        assert _routes(res) >= {
            "GET /v2/pets",
            "POST /v2/pets",
            "GET /v2/pets/<petId>",
        }

    def test_openapi_3_pathitemparam_yaml(self) -> None:
        """Successfully register OpenAPI 3 YAML specs with Connexion app.

        The specs have a `PathItem.parameters` field.
        """
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_3_PATHITEMPARAM)]
        res = register_openapi(app=app, specs=spec_configs)
        assert _routes(res) >= {
            "GET /v2/pets",
            "POST /v2/pets",
            "GET /v2/pets/<petId>",
        }

    def test_openapi_2_json(self) -> None:
        """Successfully register OpenAPI 2 JSON specs with Connexion app."""
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_2_JSON)]
        res = register_openapi(app=app, specs=spec_configs)
        assert _routes(res) >= {
            "GET /v1/pets",
            "POST /v1/pets",
            "GET /v1/pets/<petId>",
        }

    def test_openapi_2_json_and_3_yaml(self) -> None:
        """Successfully register OpenAPI2 JSON and OpenAPI3 YAML specs together.

        Both are registered with the Connexion app.
        """
        app = App(__name__)
        spec_configs = [
            SpecConfig(**SPEC_CONFIG_2_JSON),
            SpecConfig(**SPEC_CONFIG_3),
        ]
        res = register_openapi(app=app, specs=spec_configs)
        assert _routes(res) >= {
            "GET /v1/pets",
            "POST /v1/pets",
            "GET /v1/pets/<petId>",
        } | {"GET /v2/pets", "POST /v2/pets", "GET /v2/pets/<petId>"}

    def test_openapi_2_invalid(self) -> None:
        """Registration failing because of invalid OpenAPI 2 spec file."""
        app = App(__name__)
        spec_configs = [SpecConfig(path=PATH_SPECS_INVALID_YAML)]
        with pytest.raises(InvalidSpecification):
            register_openapi(app=app, specs=spec_configs)

    def test_openapi_2_json_invalid(self) -> None:
        """Registration failing because of invalid JSON spec file."""
        app = App(__name__)
        spec_configs = [SpecConfig(path=PATH_SPECS_INVALID_JSON)]
        with pytest.raises(ValueError):
            register_openapi(app=app, specs=spec_configs)

    def test_openapi_not_found(self) -> None:
        """Registration failing because spec file is unavailable."""
        app = App(__name__)
        spec_configs = [SpecConfig(path=PATH_NOT_FOUND)]
        with pytest.raises(OSError):
            register_openapi(app=app, specs=spec_configs)

    def test_openapi_2_list(self) -> None:
        """Successfully register OpenAPI 2 JSON specs with Connexion app.

        Specs provided as list.
        """
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_2_LIST)]
        res = register_openapi(app=app, specs=spec_configs)
        assert _routes(res) >= {
            "GET /v1/pets",
            "POST /v1/pets",
            "GET /v1/pets/<petId>",
        }

    def test_openapi_2_fragments(self) -> None:
        """Successfully register OpenAPI 2 JSON specs with Connexion app.

        Specs provided as multiple fragments.
        """
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_2_MULTI)]
        res = register_openapi(app=app, specs=spec_configs)
        assert _routes(res) >= {
            "GET /v1/pets",
            "POST /v1/pets",
            "GET /v1/pets/<petId>",
        } | {"PUT /v1/pets/<petId>"}

    def test_openapi_2_yaml_no_auth(self) -> None:
        """Successfully register OpenAPI 2 YAML specs with Connexion app.

        No security definitions/fields.
        """
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_2_DISABLE_AUTH)]
        res = register_openapi(app=app, specs=spec_configs)
        assert _routes(res) >= {
            "GET /v1/pets",
            "POST /v1/pets",
            "GET /v1/pets/<petId>",
        }

    def test_openapi_3_yaml_no_auth(self) -> None:
        """Successfully register OpenAPI 3 YAML specs with Connexion app.

        No security schemes/fields.
        """
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_3_DISABLE_AUTH)]
        res = register_openapi(app=app, specs=spec_configs)
        assert _routes(res) >= {
            "GET /v2/pets",
            "POST /v2/pets",
            "GET /v2/pets/<petId>",
        }


def _registered_spec(
    monkeypatch: pytest.MonkeyPatch, spec_config: dict[str, Any]
) -> dict[str, Any]:
    """Register spec and return the specification passed to `add_api()`."""
    specs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        App,
        "add_api",
        lambda self, specification, **kwargs: specs.append(specification),
    )
    register_openapi(app=App(__name__), specs=[SpecConfig(**spec_config)])
    assert len(specs) == 1
    return specs[0]


@pytest.mark.parametrize(
    "disable_auth,enforce,warned",
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_disable_auth_with_enforcement(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    disable_auth: bool,
    enforce: bool,
    warned: bool,
) -> None:
    """Disabling authentication while enforcing policies is warned about.

    Operations without security requirements are not enforced.
    """
    monkeypatch.setattr(App, "add_api", lambda self, **kwargs: None)
    monkeypatch.setattr(module.logger, "disabled", False)
    spec_config = {**SPEC_CONFIG_3, "disable_auth": disable_auth}
    with caplog.at_level(logging.WARNING, logger=module.logger.name):
        register_openapi(
            app=App(__name__),
            specs=[SpecConfig(**spec_config)],
            enforce_access_control=enforce,
        )
    assert ("'disable_auth'" in caplog.text) is warned


class TestModifiedSpecs:
    """Tests for the specifications passed to Connexion."""

    def test_disable_auth_path_item_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Security is removed from specs with path-level fields."""
        spec_config = deepcopy(SPEC_CONFIG_3_PATHITEMPARAM)
        spec_config["disable_auth"] = True
        spec = _registered_spec(monkeypatch, spec_config)
        assert "security" not in spec
        assert "securitySchemes" not in spec.get("components", {})
        operations = list(_iter_operations(spec))
        assert operations
        assert all("security" not in op for op in operations)
        # path-level parameters are kept
        assert any(
            isinstance(path_item.get("parameters"), list)
            for path_item in spec["paths"].values()
        )

    def test_add_operation_fields_path_item_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operation fields are only added to operations."""
        spec = _registered_spec(monkeypatch, SPEC_CONFIG_3_PATHITEMPARAM)
        operations = list(_iter_operations(spec))
        assert operations
        assert all(
            op["x-openapi-router-controller"] == "controllers" for op in operations
        )
        assert any(
            isinstance(path_item.get("parameters"), list)
            for path_item in spec["paths"].values()
        )

    def test_add_operation_fields_kept(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Fields set on operations are not replaced by operation fields."""
        spec_path = tmp_path / "addition.yaml"
        spec_path.write_text(
            "paths:\n"
            "  /pets:\n"
            "    get:\n"
            "      x-openapi-router-controller: other_controllers\n"
        )
        spec_config = deepcopy(SPEC_CONFIG_3)
        spec_config["path"] = [PATH_SPECS_3_YAML_ORIGINAL, spec_path]
        spec = _registered_spec(monkeypatch, spec_config)
        operations = spec["paths"]["/pets"]
        assert operations["get"]["x-openapi-router-controller"] == "other_controllers"
        assert operations["post"]["x-openapi-router-controller"] == ("controllers")


def test_iter_operations() -> None:
    """Only Operation Objects are yielded."""
    get, post = {"operationId": "a"}, {"operationId": "b"}
    spec = {
        "paths": {
            "/a": {
                "summary": "A",
                "description": "A",
                "parameters": [{"name": "x", "in": "query"}],
                "servers": [{"url": "/"}],
                "get": get,
                "x-extension": {"a": 1},
            },
            "/b": {"$ref": "#/x-path-items/b", "post": post},
            "/c": None,
        }
    }
    assert list(_iter_operations(spec)) == [get, post]
    assert list(_iter_operations({})) == []


class TestConnexionAddApiKwargs:
    """Tests for translating Connexion options to `add_api()` arguments."""

    def test_current_config(self) -> None:
        """Today's `connexion` config yields the same arguments."""
        assert (
            _connexion_add_api_kwargs(ConnexionOptions(**CONNEXION_CONFIG))
            == CONNEXION_CONFIG
        )

    def test_none(self) -> None:
        """Missing or default options yield no arguments."""
        assert _connexion_add_api_kwargs(None) == {}
        assert _connexion_add_api_kwargs(ConnexionOptions()) == {}

    def test_typed_options(self) -> None:
        """Typed UI options are passed via `options`, overriding it."""
        resolver = object()
        kwargs = _connexion_add_api_kwargs(
            ConnexionOptions(  # type: ignore[call-arg]  # extra fields allowed
                strict_validation=False,
                swagger_ui=False,
                serve_spec=True,
                swagger_ui_path="/docs",
                base_path="/api",
                resolver=resolver,
                options={"swagger_ui": True, "other": 1},
                resolver_error=501,
            )
        )
        assert kwargs == {
            "strict_validation": False,
            "base_path": "/api",
            "resolver": resolver,
            "resolver_error": 501,
            "options": {
                "swagger_ui": False,
                "serve_spec": True,
                "swagger_url": "/docs",
                "other": 1,
            },
        }
        assert kwargs["resolver"] is resolver

    def test_swagger_ui_path_precedence(self) -> None:
        """`swagger_ui_path` overrides Connexion's `swagger_url` option."""
        kwargs = _connexion_add_api_kwargs(
            ConnexionOptions(swagger_ui_path="/docs", options={"swagger_url": "/x"})
        )
        assert kwargs == {"options": {"swagger_url": "/docs"}}

    @pytest.mark.parametrize("path", ["", "/", "//"])
    def test_swagger_ui_path_invalid(self, path: str) -> None:
        """Empty or slash-only `swagger_ui_path` values are rejected."""
        with pytest.raises(ValidationError):
            ConnexionOptions(swagger_ui_path=path)

    @pytest.mark.parametrize("path", ["/docs", "docs", "/docs/"])
    def test_swagger_ui_path_served(self, path: str) -> None:
        """The Swagger UI is served at `swagger_ui_path` only.

        Connexion strips leading and trailing slashes of the path.
        """
        app = create_connexion_app(Config())
        app.add_api(
            {
                "openapi": "3.0.0",
                "info": {"title": "UI path test", "version": "1.0.0"},
                "servers": [{"url": "http://localhost/v1"}],
                "paths": {},
            },
            **_connexion_add_api_kwargs(ConnexionOptions(swagger_ui_path=path)),
        )
        client = app.app.test_client()
        response = client.get("/v1/docs/")
        assert response.status_code == 200
        assert 'url: "/v1/openapi.json"' in response.get_data(as_text=True)
        assert client.get("/v1/ui/").status_code == 404

    @pytest.mark.parametrize(
        "options",
        [
            {"swagger_ui": True, "serve_spec": False},
            {"swagger_ui": True, "options": {"serve_spec": False}},
            {"serve_spec": False, "options": {"swagger_ui": True}},
            {"options": {"swagger_ui": True, "serve_spec": False}},
        ],
    )
    def test_swagger_ui_without_spec_invalid(self, options: dict[str, Any]) -> None:
        """The Swagger UI can't be served without the specification."""
        with pytest.raises(ValidationError, match="needs the specification"):
            ConnexionOptions(**options)

    @pytest.mark.parametrize(
        "options",
        [{"serve_spec": False}, {"options": {"serve_spec": False}}],
    )
    def test_swagger_ui_disabled_without_spec(self, options: dict[str, Any]) -> None:
        """The Swagger UI is not served if the specification is not."""
        kwargs = _connexion_add_api_kwargs(ConnexionOptions(**options))
        assert kwargs == {"options": {"serve_spec": False, "swagger_ui": False}}
        app = create_connexion_app(Config())
        app.add_api(
            {
                "openapi": "3.0.0",
                "info": {"title": "UI test", "version": "1.0.0"},
                "servers": [{"url": "http://localhost/v1"}],
                "paths": {},
            },
            **kwargs,
        )
        client = app.app.test_client()
        assert client.get("/v1/ui/").status_code == 404
        assert client.get("/v1/openapi.json").status_code == 404

    def test_spec_config(self) -> None:
        """`SpecConfig.connexion` is parsed into typed options."""
        spec = SpecConfig(**SPEC_CONFIG_3)
        assert isinstance(spec.connexion, ConnexionOptions)
        assert spec.connexion.strict_validation is True
