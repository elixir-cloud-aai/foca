"""Tests of the optional endpoint `GET /me`."""

from pathlib import Path
from typing import Any
from unittest import mock

import mongomock
import pytest
import yaml
from flask import Flask
from pydantic import ValidationError

from foca.api.register_openapi import register_openapi
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (
    AccessControlConfig,
    AuthConfig,
    Config,
    ExceptionConfig,
    MongoConfig,
    SecurityConfig,
    ServiceClientConfig,
    SpecConfig,
)
from foca.security.access_control import register_access_control as module
from foca.security.access_control.enforcement import get_current_subject
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.me import import_me_hook
from foca.security.auth import InvalidTokenResolver
from foca.utils.urls import _register_api, get_resource_path

ISSUER = "https://login.example.org/oidc/"
ADMINS = "urn:example:admins"
TOKENS: dict[str, dict[str, Any]] = {
    "user": {"sub": "user", "iss": ISSUER, "groups": ["other"]},
    "admin": {"sub": "admin", "iss": ISSUER, "groups": [ADMINS]},
    "ruled": {"sub": "ruled", "iss": ISSUER},
    "service": {"sub": "conquest", "iss": ISSUER, "client_id": "conquest"},
    "nobody": {"iss": ISSUER},
}
NOT_A_HOOK = "not callable"


def hook_fields() -> dict[str, Any]:
    """Get fields of the app.

    Returns:
        A field of the app, and one named as a field of FOCA.
    """
    return {"services": ["a"], "admin": True}


def failing_hook() -> dict[str, Any]:
    """Fail.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError("Failed")


def hook_without_mapping() -> list[str]:
    """Get something that isn't a mapping.

    Returns:
        A list of numbers as text.
    """
    return ["1"]


def hook_for_caller() -> dict[str, Any]:
    """Get a field from the identity of the caller.

    Returns:
        A field with the subject.
    """
    return {"caller": get_current_subject()}


def hook_with_path() -> dict[str, Any]:
    """Get the path of a resource.

    Returns:
        A field with the path.
    """
    return {"path": get_resource_path("services")}


def _token_info(token: str) -> dict[str, Any]:
    """Get the information of a token, as FOCA's `validate_token()`.

    Args:
        token: Token, a key of `TOKENS`.

    Returns:
        Token information.
    """
    return {"jwt": token, "claims": TOKENS[token]}


def _create_app(
    tmp_path: Path, spec: dict[str, Any] | None = None, **options: Any
) -> Flask:
    """Create an app with `GET /me`.

    Args:
        tmp_path: Temporary directory.
        spec: Specification of an API of the app, if any.
        **options: Access control options.

    Returns:
        Flask app.
    """
    conf = AccessControlConfig(
        enabled=True,
        claim_groups="groups",
        group_mapping={ADMINS: "admin"},
        **options,
    )
    config = Config(
        security=SecurityConfig(
            auth=AuthConfig(
                issuers=[ISSUER],
                audience=["foca"],
                service_clients=[ServiceClientConfig(client_id="conquest")],
            ),
            access_control=conf,
        ),
    )
    cnx_app = register_exception_handler(create_connexion_app(config))
    if spec is not None:
        path = tmp_path / "spec.yaml"
        path.write_text(yaml.safe_dump(spec))
        cnx_app = register_openapi(
            cnx_app,
            specs=[SpecConfig.model_validate({"path": path, "disable_auth": True})],
        )
    with mock.patch("foca.security.auth.validate_token", new=_token_info):
        cnx_app = module.register_me_endpoint(cnx_app, conf)
    adapter = Adapter(mongomock.MongoClient().db.coll)
    adapter.save_policy_line(ptype="g", rule=["user:ruled", "admin"])
    cnx_app.app.config["CASBIN_MODEL"] = conf.model
    cnx_app.app.config["casbin_adapter"] = adapter
    return cnx_app.app


def _get(app: Flask, token: str, path: str = "/me", **headers: str) -> Any:
    """Call the endpoint with a token.

    Args:
        app: Flask app.
        token: Token.
        path: Path.
        **headers: Further headers.

    Returns:
        Response.
    """
    return app.test_client().get(
        path, headers={"Authorization": f"Bearer {token}", **headers}
    )


def _spec(servers: str, path: str = "/items") -> dict[str, Any]:
    """Get the specification of an API of the app.

    Args:
        servers: Base path.
        path: Path of its operation.

    Returns:
        Specification.
    """
    return {
        "openapi": "3.0.3",
        "info": {"title": "Test", "version": "1.0"},
        "servers": [{"url": servers}],
        "paths": {
            path: {
                "get": {
                    "operationId": f"{__name__}.hook_fields",
                    "responses": {"200": {"description": "Fine"}},
                }
            }
        },
    }


class TestResponse:
    """Tests of the responses."""

    def test_anonymous(self, tmp_path: Path) -> None:
        """Requests without a token are refused."""
        app = _create_app(tmp_path, me_enabled=True)
        assert app.test_client().get("/me").status_code == 401

    def test_user(self, tmp_path: Path) -> None:
        """Users learn who they are."""
        response = _get(_create_app(tmp_path, me_enabled=True), "user")
        assert response.status_code == 200
        assert response.json == {
            "subject": "user",
            "client": None,
            "groups": [],
            "admin": False,
            "admin_available": False,
        }

    def test_admin_from_group(self, tmp_path: Path) -> None:
        """Admins learn that they are, from their groups."""
        data = _get(_create_app(tmp_path, me_enabled=True), "admin").json
        assert data["groups"] == ["admin"]
        assert data["admin"] is True
        assert data["admin_available"] is True

    def test_admin_from_rule(self, tmp_path: Path) -> None:
        """Admins learn that they are, from role rules."""
        data = _get(_create_app(tmp_path, me_enabled=True), "ruled").json
        assert data["groups"] == []
        assert data["admin"] is True

    def test_opt_in(self, tmp_path: Path) -> None:
        """With opt-in, admins are told it's available, and when it's active."""
        app = _create_app(tmp_path, me_enabled=True, opt_in_roles=["admin"])
        data = _get(app, "admin").json
        assert data["admin"] is False
        assert data["admin_available"] is True
        data = _get(app, "admin", **{"X-Request-Roles": "admin"}).json
        assert data["admin"] is True

    def test_service_client(self, tmp_path: Path) -> None:
        """Service clients learn their client ID."""
        data = _get(_create_app(tmp_path, me_enabled=True), "service").json
        assert data["client"] == "conquest"
        assert data["subject"] == "conquest"
        assert data["admin"] is False

    def test_no_identity(self, tmp_path: Path) -> None:
        """Valid tokens without an identity get no identity."""
        response = _get(_create_app(tmp_path, me_enabled=True), "nobody")
        assert response.status_code == 200
        assert response.json["subject"] is None
        assert response.json["admin"] is False

    def test_hook(self, tmp_path: Path) -> None:
        """Apps can add fields, but not replace those of FOCA."""
        app = _create_app(tmp_path, me_enabled=True, me_hook=f"{__name__}.hook_fields")
        data = _get(app, "user").json
        assert data["services"] == ["a"]
        assert data["admin"] is False

    def test_hook_replaces_nothing(self, tmp_path: Path) -> None:
        """Fields of FOCA stay what they are, whatever the hook returns."""
        app = _create_app(tmp_path, me_enabled=True, me_hook=f"{__name__}.hook_fields")
        assert _get(app, "user").json["admin"] is False

    def test_hook_has_the_request(self, tmp_path: Path) -> None:
        """Hooks can use the helpers for the current request."""
        app = _create_app(
            tmp_path, me_enabled=True, me_hook=f"{__name__}.hook_for_caller"
        )
        assert _get(app, "user").json["caller"] == "user"

    @pytest.mark.parametrize("hook", ["failing_hook", "hook_without_mapping"])
    def test_hook_fails(self, tmp_path: Path, hook: str) -> None:
        """Hooks that fail lead to a server error, as problem details."""
        app = _create_app(tmp_path, me_enabled=True, me_hook=f"{__name__}.{hook}")
        response = _get(app, "user")
        assert response.status_code == 500
        assert response.content_type == "application/problem+json"

    def test_hook_has_path_helpers(self, tmp_path: Path) -> None:
        """The helpers for paths stay with the API of the app."""
        app = _create_app(
            tmp_path,
            _spec("/api"),
            me_enabled=True,
            me_hook=f"{__name__}.hook_with_path",
        )
        assert _get(app, "user").json["path"] == "/api/services"

    def test_no_identity_with_opt_in(self, tmp_path: Path) -> None:
        """Without an identity, being an admin isn't available."""
        app = _create_app(tmp_path, me_enabled=True, opt_in_roles=["admin"])
        TOKENS["nobody_admin"] = {"iss": ISSUER, "groups": [ADMINS]}
        try:
            assert _get(app, "nobody_admin").json["admin_available"] is False
        finally:
            del TOKENS["nobody_admin"]

    def test_other_path(self, tmp_path: Path) -> None:
        """The endpoint can be moved."""
        app = _create_app(tmp_path, me_enabled=True, me_path="/ga4gh/v1/me")
        assert _get(app, "user", path="/ga4gh/v1/me").status_code == 200
        assert _get(app, "user").status_code == 404


class TestRegistration:
    """Tests of the registration."""

    def test_disabled(self, tmp_path: Path) -> None:
        """The endpoint is off by default."""
        assert _get(_create_app(tmp_path), "user").status_code == 404

    def test_invalid_hook_without_endpoint(self, tmp_path: Path) -> None:
        """Hooks are checked if an app routes to the controller itself."""
        with pytest.raises(ValueError, match="could not be imported"):
            _create_app(tmp_path, me_hook="nowhere.at_all")

    def test_options_without_endpoint(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Options that have no effect are logged."""
        logger = module.logger
        disabled, propagate = logger.disabled, logger.propagate
        logger.disabled, logger.propagate = False, True
        try:
            _create_app(tmp_path, me_path="/whoami")
        finally:
            logger.disabled, logger.propagate = disabled, propagate
        assert "has no effect" in caplog.text

    @pytest.mark.parametrize(
        ("path", "message"),
        [
            ("nowhere.at_all", "could not be imported"),
            ("no_dots", "could not be imported"),
            (f"{__name__}.missing", "could not be imported"),
            (f"{__name__}.NOT_A_HOOK", "not callable"),
        ],
    )
    def test_invalid_hook(self, path: str, message: str) -> None:
        """Hooks that can't be used are refused."""
        with pytest.raises(ValueError, match=message):
            import_me_hook(path)

    def test_invalid_hook_on_startup(self, tmp_path: Path) -> None:
        """They are refused on startup."""
        with pytest.raises(ValueError, match="could not be imported"):
            _create_app(tmp_path, me_enabled=True, me_hook="nowhere.at_all")

    @pytest.mark.parametrize("path", ["me", "/me/", "/a/../me", "/m e", ""])
    def test_invalid_path(self, path: str) -> None:
        """Paths are checked."""
        with pytest.raises(ValidationError, match="'me_path' must be"):
            AccessControlConfig(me_path=path)

    @pytest.mark.parametrize(
        "options",
        [{"me_enabled": True}, {"me_hook": "a.b"}, {"me_path": "/whoami"}],
    )
    def test_needs_access_control(self, options: dict[str, Any]) -> None:
        """Without access control, the options are refused."""
        with pytest.raises(ValidationError, match=r"require 'access_control\.enabled'"):
            SecurityConfig(access_control=AccessControlConfig(**options))

    @pytest.mark.parametrize(
        ("servers", "paths"),
        [
            ([{"url": "/api"}], ["/items"]),
            ([{"url": "/"}], ["/items"]),
            ([{"url": "/"}], ["/me"]),
        ],
    )
    def test_with_api_of_app(
        self, tmp_path: Path, servers: list[dict[str, str]], paths: list[str]
    ) -> None:
        """APIs of the app at another base path are fine, others are refused."""
        spec = {
            "openapi": "3.0.3",
            "info": {"title": "Test", "version": "1.0"},
            "servers": servers,
            "paths": {
                path: {
                    "get": {
                        "operationId": f"{__name__}.hook_fields",
                        "responses": {"200": {"description": "Fine"}},
                    }
                }
                for path in paths
            },
        }
        if servers[0]["url"] == "/":
            with pytest.raises(ValueError, match="me_"):
                _create_app(tmp_path, spec, me_enabled=True)
        else:
            app = _create_app(tmp_path, spec, me_enabled=True)
            assert _get(app, "user").status_code == 200

    def test_custom_permissions_api_at_root(self, tmp_path: Path) -> None:
        """Other APIs at the base path are refused as well."""
        cnx_app = create_connexion_app(Config())
        _register_api(cnx_app.app, "/", [], is_app_api=False)
        with pytest.raises(ValueError, match="me_enabled"):
            module.register_me_endpoint(cnx_app, AccessControlConfig(me_enabled=True))

    def test_route_with_parameter(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Routes that the endpoint takes precedence over are logged."""
        logger = module.logger
        disabled, propagate = logger.disabled, logger.propagate
        logger.disabled, logger.propagate = False, True
        try:
            app = _create_app(
                tmp_path,
                _spec("/api", "/{id}"),
                me_enabled=True,
                me_path="/api/me",
            )
        finally:
            logger.disabled, logger.propagate = disabled, propagate
        assert "takes precedence" in caplog.text
        assert _get(app, "user", path="/api/me").json["subject"] == "user"

    def test_registered_with_access_control(self, tmp_path: Path) -> None:
        """Registering access control registers it, whatever the rules."""
        conf = AccessControlConfig(
            enabled=True,
            me_enabled=True,
            me_path="/api/me",
            policies=[["p", "member", "{base_path}/*", "GET"]],
        )
        config = Config(
            security=SecurityConfig(
                auth=AuthConfig(issuers=[ISSUER], audience=["foca"]),
                access_control=conf,
            ),
            db=MongoConfig(),
        )
        cnx_app = register_exception_handler(create_connexion_app(config))
        path = tmp_path / "spec.yaml"
        path.write_text(yaml.safe_dump(_spec("/api")))
        cnx_app = register_openapi(
            cnx_app,
            specs=[SpecConfig.model_validate({"path": path, "disable_auth": True})],
        )
        with (
            mock.patch(
                "foca.security.access_control.register_access_control.get_mongo_client",
                return_value=mongomock.MongoClient(),
            ),
            mock.patch("foca.security.auth.validate_token", new=_token_info),
        ):
            cnx_app = module.register_access_control(
                cnx_app=cnx_app,
                mongo_config=config.db,
                access_control_config=conf,
            )
        assert _get(cnx_app.app, "user", path="/api/me").status_code == 200

    def test_other_method_at_path(self, tmp_path: Path) -> None:
        """Routes at the path with other methods stay."""
        spec = _spec("/api")
        spec["paths"]["/items"]["post"] = spec["paths"]["/items"].pop("get")
        app = _create_app(tmp_path, spec, me_enabled=True, me_path="/api/items")
        assert _get(app, "user", path="/api/items").status_code == 200
        assert app.test_client().post("/api/items").status_code == 200

    def test_path_taken(self, tmp_path: Path) -> None:
        """Paths of routes of the app are refused."""
        spec = {
            "openapi": "3.0.3",
            "info": {"title": "Test", "version": "1.0"},
            "servers": [{"url": "/api"}],
            "paths": {
                "/items": {
                    "get": {
                        "operationId": f"{__name__}.hook_fields",
                        "responses": {"200": {"description": "Fine"}},
                    }
                }
            },
        }
        with pytest.raises(ValueError, match="taken by another route"):
            _create_app(tmp_path, spec, me_enabled=True, me_path="/api/items")

    def test_rejects_invalid_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invalid optional tokens are handled as for the app's APIs."""
        cnx_app = create_connexion_app(
            Config(
                security=SecurityConfig(
                    auth=AuthConfig(
                        issuers=[ISSUER],
                        audience=["foca"],
                        reject_invalid_tokens=True,
                    ),
                )
            )
        )
        calls = []

        def add_api(specification: Any, **kwargs: Any) -> Any:
            calls.append(kwargs)
            api = mock.Mock(base_path="")
            api.specification.raw = {"paths": {}}
            return api

        monkeypatch.setattr(cnx_app, "add_api", add_api)
        module.register_me_endpoint(cnx_app, AccessControlConfig(me_enabled=True))
        assert isinstance(calls[0]["resolver"], InvalidTokenResolver)

    def test_error_media_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The specification declares the configured media type of errors."""
        cnx_app = create_connexion_app(
            Config(exceptions=ExceptionConfig(media_type="application/json"))
        )
        specs = []

        def add_api(specification: Any, **kwargs: Any) -> Any:
            specs.append(specification)
            api = mock.Mock(base_path="")
            api.specification.raw = {"paths": {}}
            return api

        monkeypatch.setattr(cnx_app, "add_api", add_api)
        module.register_me_endpoint(cnx_app, AccessControlConfig(me_enabled=True))
        assert "application/problem+json" not in str(specs[0])
