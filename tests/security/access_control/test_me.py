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
    SecurityConfig,
    ServiceClientConfig,
    SpecConfig,
)
from foca.security.access_control import register_access_control as module
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.me import import_me_hook
from foca.security.auth import InvalidTokenResolver

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

    @pytest.mark.parametrize(
        "options", [{"me_path": "/whoami"}, {"me_hook": f"{__name__}.hook_fields"}]
    )
    def test_options_without_endpoint(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        options: dict[str, Any],
    ) -> None:
        """Options that have no effect are logged."""
        logger = module.logger
        disabled, propagate = logger.disabled, logger.propagate
        logger.disabled, logger.propagate = False, True
        try:
            _create_app(tmp_path, **options)
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

    def test_needs_access_control(self) -> None:
        """Without access control, the endpoint is refused."""
        with pytest.raises(ValidationError, match=r"require 'access_control\.enabled'"):
            SecurityConfig(access_control=AccessControlConfig(me_enabled=True))

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
            with pytest.raises(ValueError, match="me_path"):
                _create_app(tmp_path, spec, me_enabled=True)
        else:
            app = _create_app(tmp_path, spec, me_enabled=True)
            assert _get(app, "user").status_code == 200

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
