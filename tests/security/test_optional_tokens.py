"""Tests for rejecting invalid tokens on operations with optional authentication."""

import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import jwt
import pytest
import yaml
from connexion.resolver import Resolution, Resolver
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, jsonify
from werkzeug import exceptions as wz
from werkzeug.datastructures import WWWAuthenticate as wz_challenge

from foca.api.register_openapi import register_openapi
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, SpecConfig
from foca.security import auth
from foca.security.access_control import get_current_subject
from foca.security.auth import InvalidTokenResolver, _reject_invalid_token

ISSUER = "https://idp.example.org"
DISCOVERY_URI = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER}/jwks"
THIS_MODULE = __name__
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "key-1"


def _token(**claims: Any) -> str:
    """Create signed access token with valid default claims."""
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "alice",
        "aud": "foca",
        "iat": now,
        "exp": now + 300,
        **claims,
    }
    return jwt.encode(
        payload, KEY, algorithm="RS256", headers={"typ": "at+jwt", "kid": KID}
    )


def controller(*args: Any, **kwargs: Any) -> Any:
    """Controller reporting the identified caller."""
    return jsonify(get_current_subject())


optional_first = empty_first = required = public = key = controller
custom = custom_again = both = controller


def validate_custom(token: str) -> dict[str, Any]:
    """Reject all tokens, as an app's own validation function may."""
    raise auth.Unauthorized("Not for this validation function")


def validate_key(key: str, required_scopes: Any = None) -> dict[str, Any]:
    """Accept all API keys."""
    return {"sub": "key-client"}


def _operation(name: str, security: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Build an OpenAPI 3 Operation object with security requirements."""
    operation: dict[str, Any] = {
        "operationId": f"{THIS_MODULE}.{name}",
        "responses": {
            "200": {"description": "OK", "content": {"application/json": {}}}
        },
    }
    if security is not None:
        operation["security"] = security
    return operation


SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "Test API", "version": "1.0.0"},
    "paths": {
        "/optional": {
            "get": _operation("optional_first", [{"bearerAuth": []}, {}]),
        },
        "/empty-first": {
            "get": _operation("empty_first", [{}, {"bearerAuth": []}]),
        },
        "/required": {"get": _operation("required", [{"bearerAuth": []}])},
        "/public": {"get": _operation("public", [])},
        "/key": {"get": _operation("key", [{"apiKey": []}, {}])},
        "/both": {
            "get": _operation("both", [{"goodKey": []}, {"bearerAuth": []}, {}]),
        },
        "/custom": {"get": _operation("custom", [{"customAuth": []}, {}])},
        "/custom-again": {"get": _operation("custom_again", [{"customAuth": []}, {}])},
    },
    "components": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "x-bearerInfoFunc": "foca.security.auth.validate_token",
            },
            "customAuth": {
                "type": "http",
                "scheme": "bearer",
                "x-bearerInfoFunc": f"{THIS_MODULE}.validate_custom",
            },
            "goodKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Key",
                "x-apikeyInfoFunc": f"{THIS_MODULE}.validate_key",
            },
            "apiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Key",
                "x-apikeyInfoFunc": f"{THIS_MODULE}.validate_custom",
            },
        },
    },
}


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """Mock identity provider; returns mock of `requests.get()`."""
    auth._oidc_configs.clear()
    auth._jwk_sets.clear()
    auth._failed_fetches.clear()
    jwk = {
        **jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True),
        "kid": KID,
        "use": "sig",
    }
    responses = {
        DISCOVERY_URI: {"issuer": ISSUER, "jwks_uri": JWKS_URI},
        JWKS_URI: {"keys": [jwk]},
    }

    def _get(url: str, **kwargs: Any) -> MagicMock:
        response = MagicMock(status_code=200)
        response.json.return_value = responses[url]
        return response

    get = MagicMock(side_effect=_get)
    monkeypatch.setattr(auth.requests, "get", get)
    yield get
    auth._oidc_configs.clear()
    auth._jwk_sets.clear()
    auth._failed_fetches.clear()


def _app(tmp_path: Path, reject: bool) -> Flask:
    """Create app with the test API.

    Args:
        tmp_path: Directory for the specification.
        reject: Value of `security.auth.reject_invalid_tokens`.

    Returns:
        Flask app.
    """
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(yaml.safe_dump(SPEC))
    config = Config.model_validate(
        {
            "security": {
                "auth": {
                    "issuers": [ISSUER],
                    "audience": ["foca"],
                    "reject_invalid_tokens": reject,
                }
            }
        }
    )
    cnx_app = register_exception_handler(create_connexion_app(config))
    return register_openapi(app=cnx_app, specs=[SpecConfig(path=spec_file)]).app


def _get(app: Flask, path: str, header: str | None) -> Any:
    """Send a request with an `Authorization` header, if any."""
    headers = {} if header is None else {"Authorization": header}
    return app.test_client().get(path, headers=headers)


class TestOptionalTokens:
    """Tests of operations with optional authentication."""

    @pytest.mark.parametrize("path", ["/optional", "/empty-first"])
    @pytest.mark.parametrize("reject", [True, False])
    def test_no_token(
        self, idp: MagicMock, tmp_path: Path, reject: bool, path: str
    ) -> None:
        """Requests without a token stay anonymous."""
        response = _get(_app(tmp_path, reject), path, None)
        assert response.status_code == 200
        assert response.get_json() is None

    @pytest.mark.parametrize("path", ["/optional", "/empty-first"])
    @pytest.mark.parametrize("reject", [True, False])
    def test_other_scheme(
        self, idp: MagicMock, tmp_path: Path, reject: bool, path: str
    ) -> None:
        """Other schemes than `Bearer` aren't tokens."""
        response = _get(_app(tmp_path, reject), path, "Basic abc")
        assert response.status_code == 200
        assert response.get_json() is None

    @pytest.mark.parametrize("reject", [True, False])
    def test_valid(self, idp: MagicMock, tmp_path: Path, reject: bool) -> None:
        """Valid tokens identify the caller."""
        response = _get(_app(tmp_path, reject), "/optional", f"Bearer {_token()}")
        assert response.status_code == 200
        assert response.get_json() == "alice"

    def test_valid_empty_requirement_first(
        self, idp: MagicMock, tmp_path: Path
    ) -> None:
        """Valid tokens are accepted, but Connexion leaves the caller anonymous."""
        response = _get(_app(tmp_path, True), "/empty-first", f"Bearer {_token()}")
        assert response.status_code == 200
        assert response.get_json() is None

    def test_scheme_in_any_case(self, idp: MagicMock, tmp_path: Path) -> None:
        """The scheme is case-insensitive."""
        response = _get(_app(tmp_path, True), "/empty-first", "bearer not-a-jwt")
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "claims",
        [
            pytest.param({"exp": 1000, "iat": 500}, id="expired"),
            pytest.param({"aud": "other"}, id="wrong-audience"),
            pytest.param({"iss": "https://evil.example.org"}, id="untrusted-issuer"),
        ],
    )
    @pytest.mark.parametrize("path", ["/optional", "/empty-first"])
    def test_invalid_rejected(
        self, idp: MagicMock, tmp_path: Path, path: str, claims: dict[str, Any]
    ) -> None:
        """With the option, invalid tokens get a challenge."""
        response = _get(_app(tmp_path, True), path, f"Bearer {_token(**claims)}")
        assert response.status_code == 401
        assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]

    @pytest.mark.parametrize(
        "claims",
        [
            pytest.param({"exp": 1000, "iat": 500}, id="expired"),
            pytest.param({"aud": "other"}, id="wrong-audience"),
            pytest.param({"iss": "https://evil.example.org"}, id="untrusted-issuer"),
        ],
    )
    def test_invalid_anonymous_without_option(
        self, idp: MagicMock, tmp_path: Path, claims: dict[str, Any]
    ) -> None:
        """Without the option, invalid tokens are handled as anonymous."""
        response = _get(
            _app(tmp_path, False), "/optional", f"Bearer {_token(**claims)}"
        )
        assert response.status_code == 200
        assert response.get_json() is None

    @pytest.mark.parametrize("header", ["Bearer", "Bearer  ", "Bearer not-a-jwt"])
    def test_malformed_rejected(
        self, idp: MagicMock, tmp_path: Path, header: str
    ) -> None:
        """Empty and malformed tokens are invalid, too."""
        response = _get(_app(tmp_path, True), "/empty-first", header)
        assert response.status_code == 401

    def test_identity_provider_unavailable(
        self,
        idp: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Tokens that can't be validated don't break public operations."""
        idp.side_effect = auth.requests.ConnectionError("down")
        with caplog.at_level(logging.INFO, logger=auth.logger.name):
            response = _get(_app(tmp_path, True), "/optional", f"Bearer {_token()}")
        assert response.status_code == 200
        assert response.get_json() is None
        assert "handled as anonymous" in caplog.text

    def test_identity_provider_unavailable_empty_requirement_first(
        self, idp: MagicMock, tmp_path: Path
    ) -> None:
        """Also if only FOCA validates the token, outages aren't rejections."""
        idp.side_effect = auth.requests.ConnectionError("down")
        response = _get(_app(tmp_path, True), "/empty-first", f"Bearer {_token()}")
        assert response.status_code == 200

    def test_token_validated_once(self, idp: MagicMock, tmp_path: Path) -> None:
        """The rejection of Connexion's validation is used."""
        idp.side_effect = auth.requests.ConnectionError("down")
        app = _app(tmp_path, True)
        with mock.patch.object(auth, "_validate", wraps=auth._validate) as validate:
            assert _get(app, "/optional", f"Bearer {_token()}").status_code == 200
        assert validate.call_count == 1

    @pytest.mark.parametrize("path", ["/key", "/custom"])
    def test_other_validation_unchanged(
        self, idp: MagicMock, tmp_path: Path, path: str
    ) -> None:
        """Tokens of operations not using FOCA's validation aren't judged."""
        response = _get(_app(tmp_path, True), path, "Bearer opaque-for-others")
        assert response.status_code == 200
        assert not idp.called

    def test_invalid_rejected_with_other_scheme(
        self, idp: MagicMock, tmp_path: Path
    ) -> None:
        """Another accepted scheme doesn't excuse an invalid token."""
        client = _app(tmp_path, True).test_client()
        headers = {"X-Key": "key", "Authorization": "Bearer invalid"}
        assert client.get("/both", headers=headers).status_code == 401
        assert client.get("/both", headers={"X-Key": "key"}).status_code == 200

    def test_required_unchanged(self, idp: MagicMock, tmp_path: Path) -> None:
        """Operations that require tokens aren't changed."""
        app = _app(tmp_path, True)
        assert _get(app, "/required", None).status_code == 401
        assert _get(app, "/required", "Bearer not-a-jwt").status_code == 401
        assert _get(app, "/required", f"Bearer {_token()}").status_code == 200

    def test_public_unchanged(self, idp: MagicMock, tmp_path: Path) -> None:
        """Public operations don't look at tokens."""
        response = _get(_app(tmp_path, True), "/public", "Bearer not-a-jwt")
        assert response.status_code == 200
        assert not idp.called


def _operation_mock(security: list[dict[str, Any]]) -> MagicMock:
    """Build a Connexion operation with the security schemes of the test API."""
    operation = MagicMock()
    operation.security = security
    operation.security_schemes = SPEC["components"]["securitySchemes"]  # type: ignore[index]
    return operation


class TestOtherValidationFunctionWarning:
    """Tests of the warning that the option doesn't reach other functions."""

    @pytest.mark.parametrize(
        "function", ["app.check", "foca.security.auth.validate_token"]
    )
    def test_token_info_function(
        self, caplog: pytest.LogCaptureFixture, function: str
    ) -> None:
        """Functions of OAuth 2.0 schemes aren't reached, even FOCA's."""
        operation = _operation_mock([{"oauth": []}, {}])
        operation.security_schemes = {"oauth": {"x-tokenInfoFunc": function}}
        resolver = InvalidTokenResolver(lambda operation_id: controller)
        with caplog.at_level(logging.WARNING, logger=auth.logger.name):
            resolver.resolve(operation)
        assert f"by '{function}'" in caplog.text

    def test_warns_once_per_function(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Startup names the function once, for all of its operations."""
        with caplog.at_level(logging.WARNING, logger=auth.logger.name):
            _app(tmp_path, True)
        warnings = [
            record.getMessage()
            for record in caplog.records
            if "reject_invalid_tokens" in record.getMessage()
        ]
        assert len(warnings) == 1
        assert f"{THIS_MODULE}.validate_custom" in warnings[0]
        assert "foca.security.auth.validate_token in" in warnings[0]

    def test_option_off(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Without the option, nothing is missed."""
        with caplog.at_level(logging.WARNING, logger=auth.logger.name):
            _app(tmp_path, False)
        assert "reject_invalid_tokens" not in caplog.text

    @pytest.mark.parametrize(
        "security",
        [
            pytest.param([{"bearerAuth": []}, {}], id="validate_token"),
            pytest.param([{"customAuth": []}], id="required"),
            pytest.param([{"apiKey": []}, {}], id="api key"),
            pytest.param([{"customAuth": []}, {"bearerAuth": []}, {}], id="both"),
            pytest.param([], id="public"),
        ],
    )
    def test_no_warning(
        self, caplog: pytest.LogCaptureFixture, security: list[dict[str, Any]]
    ) -> None:
        """Other operations, which the option isn't about, don't warn."""
        resolver = InvalidTokenResolver(lambda operation_id: controller)
        with caplog.at_level(logging.WARNING, logger=auth.logger.name):
            resolver.resolve(_operation_mock(security))
        assert "reject_invalid_tokens" not in caplog.text


class TestInvalidTokenResolver:
    """Tests of the resolver."""

    def test_function_resolver(self) -> None:
        """Callables are wrapped, as by Connexion."""
        resolver = InvalidTokenResolver(lambda operation_id: controller)
        assert isinstance(resolver.resolver, Resolver)

    def test_resolver_object(self) -> None:
        """Other resolvers are used as they are."""

        class _DuckTyped:
            def resolve(self, operation: Any) -> Resolution:
                return Resolution(controller, "id")

        duck = _DuckTyped()
        resolver = InvalidTokenResolver(duck)
        assert resolver.resolver is duck


@pytest.mark.parametrize(
    ("exception", "kept"),
    [
        (
            wz.Unauthorized(
                www_authenticate=wz_challenge("bearer", {"error": "invalid_token"})
            ),
            False,
        ),
        (wz.Unauthorized(), True),
    ],
)
def test_raw_unauthorized_of_validation(exception: Any, kept: bool) -> None:
    """Raw `401` errors of token validation count like those of FOCA."""
    wrapped = _reject_invalid_token(lambda: "served")
    app = Flask(__name__)
    with (
        app.test_request_context(headers={"Authorization": "Bearer t"}),
        mock.patch("foca.security.auth.validate_token", side_effect=exception),
    ):
        if kept:
            assert wrapped() == "served"
        else:
            with pytest.raises(wz.Unauthorized):
                wrapped()
