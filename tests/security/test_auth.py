"""Tests for authentication module."""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar
from unittest.mock import MagicMock

import jwt
import pytest
from connexion.exceptions import Unauthorized
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from flask import Flask
from flask import request as auth_request
from pydantic import ValidationError

from foca.config import access
from foca.config.access import get_app_config, set_app_config
from foca.models.config import AuthConfig, Config, ServiceClientConfig
from foca.security import auth
from foca.security.auth import (
    log_auth_config_warnings,
    require_no_service_clients,
    validate_token,
)
from tests.mock_data import INVALID_TOKEN_CHALLENGE

ISSUER = "https://idp.example.org"
OTHER_ISSUER = "https://other-idp.example.org"
DISCOVERY_URI = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER}/jwks"
USERINFO_URI = f"{ISSUER}/userinfo"
KID = "key-1"
SUBJECT = "alice"
AUDIENCE = "foca"
CLIENT = "gateway"


def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


PRIVATE_KEY = _private_key()
OTHER_PRIVATE_KEY = _private_key()


def _public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    return {
        **jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
        "kid": kid,
        "use": "sig",
    }


def make_token(
    key: rsa.RSAPrivateKey | None = PRIVATE_KEY,
    kid: str | None = KID,
    algorithm: str = "RS256",
    typ: Any = "at+jwt",
    **claims: Any,
) -> str:
    """Create signed access token (RFC 9068) with valid default claims."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "scope": "openid",
    }
    payload.update(claims)
    payload = {key: val for key, val in payload.items() if val is not None}
    # PyJWT omits the `typ` header if it is `None`
    headers: dict[str, Any] = {"typ": typ}
    if kid is not None:
        headers["kid"] = kid
    # no key for unsigned tokens (algorithm `none`)
    return jwt.encode(
        payload,
        key,  # type: ignore[arg-type]
        algorithm=algorithm,
        headers=headers,
    )


class MockResponse:
    """Mock response of `requests.get()`."""

    def __init__(self, data: Any, status_code: int = 200) -> None:
        """Create mock response with JSON data and status code."""
        self.data = data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """Raise `HTTPError` for error status codes."""
        if self.status_code >= 400:
            raise auth.requests.HTTPError(str(self.status_code))

    def json(self) -> dict[str, Any]:
        """Return JSON data."""
        return self.data


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """Mock identity provider; returns mock of `requests.get()`."""
    auth._oidc_configs.clear()
    auth._jwk_sets.clear()
    auth._failed_fetches.clear()
    responses = {
        DISCOVERY_URI: MockResponse(
            {
                "issuer": ISSUER,
                "jwks_uri": JWKS_URI,
                "userinfo_endpoint": USERINFO_URI,
            }
        ),
        JWKS_URI: MockResponse({"keys": [_public_jwk(PRIVATE_KEY, KID)]}),
        USERINFO_URI: MockResponse({"sub": SUBJECT}),
    }
    get = MagicMock(side_effect=lambda url, **kwargs: responses[url])
    get.responses = responses
    monkeypatch.setattr(auth.requests, "get", get)
    yield get
    auth._oidc_configs.clear()
    auth._jwk_sets.clear()
    auth._failed_fetches.clear()


def urls(get: MagicMock) -> list[str]:
    """URLs requested via mock of `requests.get()`."""
    return [call.args[0] for call in get.call_args_list]


def make_app(**auth_conf: Any) -> Flask:
    """Create Flask app with FOCA config."""
    app = Flask(__name__)
    auth_conf = {"issuers": [ISSUER], "audience": [AUDIENCE], **auth_conf}
    app.config.foca = Config.model_validate(  # type: ignore[attr-defined]  # dynamic attribute
        {"security": {"auth": auth_conf}}
    )
    return app


@contextmanager
def raises_unauthorized(
    challenge: str = INVALID_TOKEN_CHALLENGE,
) -> Iterator[None]:
    """Assert that `Unauthorized` is raised with a Bearer challenge.

    The challenge is as per RFC 6750, §3.1, by default with error code
    `invalid_token`.
    """
    with pytest.raises(Unauthorized) as exc_info:
        yield
    headers = exc_info.value.get_headers()
    assert ("WWW-Authenticate", challenge) in headers


def validate(app: Flask, token: str) -> dict[str, Any]:
    """Validate token in a request context of `app`."""
    with app.test_request_context():
        return validate_token(token=token)


class TestValidateToken:
    """Tests for `validate_token()`."""

    def test_warnings_logged_once(
        self, idp: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settings are checked for warnings on the first validation."""
        calls: list[AuthConfig] = []
        monkeypatch.setattr(auth, "log_auth_config_warnings", calls.append)
        app = make_app()
        validate(app, make_token())
        validate(app, make_token())
        assert calls == [get_app_config(app).security.auth]

    def test_no_claims_logged(
        self, idp: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only issuer and subject are logged, not tokens or other claims."""
        messages: list[str] = []
        monkeypatch.setattr(
            auth.logger,
            "debug",
            lambda msg, *args: messages.append(msg % args),
        )
        token = make_token(email="alice@example.org", name="Alice A.")
        validate(make_app(), token)
        logged = " ".join(messages)
        assert f"Token issued by '{ISSUER}' validated for subject '{SUBJECT}'" in logged
        for value in [token, "alice@example.org", "Alice A.", "openid"]:
            assert value not in logged

    def test_success(self, idp: MagicMock) -> None:
        """Valid token is accepted; verified claims are returned."""
        res = validate(make_app(), make_token())
        assert res["user_id"] == SUBJECT
        assert res["claims"]["iss"] == ISSUER
        assert res["scope"] == "openid"
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI]
        for call in idp.call_args_list:
            assert call.kwargs["allow_redirects"] is False
            assert call.kwargs["timeout"] == 10

    def test_request_headers_unchanged(self, idp: MagicMock) -> None:
        """Claims are not added to request headers."""
        app = make_app()
        headers = {"Authorization": "Bearer x", "Content-Type": "text/plain"}
        with app.test_request_context(headers=headers):
            before = dict(auth_request.headers)
            validate_token(token=make_token())
            assert dict(auth_request.headers) == before
            assert "sub" not in auth_request.headers
            assert "user_id" not in auth_request.headers

    def test_untrusted_issuer(self, idp: MagicMock) -> None:
        """Tokens of untrusted issuers are rejected without requests."""
        token = make_token(iss="https://evil.example.org")
        with raises_unauthorized():
            validate(make_app(), token)
        idp.assert_not_called()

    def test_no_trusted_issuers(self, idp: MagicMock) -> None:
        """All tokens are rejected if no issuers are trusted."""
        with raises_unauthorized():
            validate(make_app(issuers=[]), make_token())
        idp.assert_not_called()

    def test_missing_issuer(self, idp: MagicMock) -> None:
        """Tokens without issuer are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(iss=None))
        idp.assert_not_called()

    def test_undecodable_token(self, idp: MagicMock) -> None:
        """Malformed tokens are rejected."""
        with raises_unauthorized():
            validate(make_app(), "not-a-token")

    def test_discovery_issuer_mismatch(self, idp: MagicMock) -> None:
        """Discovery documents must be issued for the token's issuer."""
        idp.responses[DISCOVERY_URI].data["issuer"] = "https://evil.org"
        with raises_unauthorized("Bearer"):
            validate(make_app(), make_token())
        assert JWKS_URI not in urls(idp)

    def test_discovery_unavailable(self, idp: MagicMock) -> None:
        """Tokens are rejected if the discovery document is unavailable."""
        idp.responses[DISCOVERY_URI] = MockResponse({}, status_code=500)
        with raises_unauthorized("Bearer"):
            validate(make_app(), make_token())

    def test_jwks_uri_missing(self, idp: MagicMock) -> None:
        """Tokens are rejected if the issuer announces no JWK set."""
        del idp.responses[DISCOVERY_URI].data["jwks_uri"]
        with raises_unauthorized("Bearer"):
            validate(make_app(), make_token())
        assert urls(idp) == [DISCOVERY_URI]

    @pytest.mark.parametrize(
        "jwks",
        [
            pytest.param({"keys": []}, id="no-keys"),
            pytest.param({"keys": "abc"}, id="keys-not-list"),
            pytest.param({"keys": [1]}, id="key-not-object"),
            pytest.param({"keys": [{"kty": "foo"}]}, id="key-unusable"),
            pytest.param({"keys": [{"kty": "oct", "k": 1}]}, id="key-material-number"),
            pytest.param(
                {"keys": [{"kty": "RSA", "n": "!!!", "e": "AQAB"}]},
                id="key-material-invalid",
            ),
            pytest.param(
                {"keys": [{"kty": "oct", "k": "AAAA", "alg": ["HS256"]}]},
                id="alg-not-string",
            ),
            pytest.param({}, id="empty-object"),
            pytest.param([], id="list"),
            pytest.param("keys", id="string"),
            pytest.param(None, id="null"),
        ],
    )
    def test_jwks_invalid(self, idp: MagicMock, jwks: Any) -> None:
        """Tokens are rejected if the JWK set is invalid or not a JSON object.

        Like failed fetches, invalid JWK sets are not fetched again within
        cooldown.
        """
        app = make_app()
        idp.responses[JWKS_URI] = MockResponse(jwks)
        for _ in range(3):
            with raises_unauthorized("Bearer"):
                validate(app, make_token())
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI]
        assert JWKS_URI in auth._failed_fetches

    def test_jwks_unavailable(self, idp: MagicMock) -> None:
        """Tokens are rejected if the JWK set is unavailable."""
        idp.responses[JWKS_URI] = MockResponse({}, status_code=500)
        with raises_unauthorized("Bearer"):
            validate(make_app(), make_token())

    @pytest.mark.parametrize("uri", [DISCOVERY_URI, JWKS_URI])
    def test_failed_fetch_cooldown(self, idp: MagicMock, uri: str) -> None:
        """Failed fetches are not repeated within cooldown."""
        app = make_app()
        ok = idp.responses[uri]
        idp.responses[uri] = MockResponse({}, status_code=503)
        for _ in range(3):
            with raises_unauthorized("Bearer"):
                validate(app, make_token())
        assert urls(idp).count(uri) == 1
        # fetched again after cooldown
        idp.responses[uri] = ok
        auth._failed_fetches[uri] -= auth.JWKS_REFETCH_COOLDOWN
        assert validate(app, make_token())["user_id"] == SUBJECT
        assert urls(idp).count(uri) == 2
        assert uri not in auth._failed_fetches

    def test_failed_refetch_cooldown(self, idp: MagicMock) -> None:
        """Failed refetches for unknown key IDs are not repeated within cooldown."""
        app = make_app()
        validate(app, make_token())
        idp.responses[JWKS_URI] = MockResponse({}, status_code=503)
        auth._jwk_sets[JWKS_URI] = (
            auth._jwk_sets[JWKS_URI][0] - auth.JWKS_REFETCH_COOLDOWN,
            auth._jwk_sets[JWKS_URI][1],
        )
        for _ in range(3):
            with raises_unauthorized("Bearer"):
                validate(app, make_token(kid="other-key"))
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI, JWKS_URI]
        # known keys are still accepted
        assert validate(app, make_token())["user_id"] == SUBJECT

    def test_userinfo_failures_not_throttled(self, idp: MagicMock) -> None:
        """Failed user info requests depend on the token, so are repeated."""
        app = make_app(validate_userinfo=True)
        idp.responses[USERINFO_URI] = MockResponse({}, status_code=401)
        for _ in range(2):
            with raises_unauthorized():
                validate(app, make_token())
        assert urls(idp).count(USERINFO_URI) == 2

    def test_cached(self, idp: MagicMock) -> None:
        """Discovery documents and JWK sets are cached."""
        app = make_app()
        validate(app, make_token())
        validate(app, make_token())
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI]

    def test_cache_expired(self, idp: MagicMock) -> None:
        """Discovery documents and JWK sets are refetched after TTL."""
        app = make_app(cache_ttl=0)
        validate(app, make_token())
        validate(app, make_token())
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI] * 2

    def test_key_rotation(self, idp: MagicMock) -> None:
        """JWK set is refetched once for unknown key IDs."""
        app = make_app()
        validate(app, make_token())
        idp.responses[JWKS_URI] = MockResponse(
            {"keys": [_public_jwk(OTHER_PRIVATE_KEY, "key-2")]}
        )
        auth._jwk_sets[JWKS_URI] = (
            auth._jwk_sets[JWKS_URI][0] - auth.JWKS_REFETCH_COOLDOWN,
            auth._jwk_sets[JWKS_URI][1],
        )
        res = validate(app, make_token(key=OTHER_PRIVATE_KEY, kid="key-2"))
        assert res["user_id"] == SUBJECT
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI, JWKS_URI]

    def test_unknown_key_id_cooldown(self, idp: MagicMock) -> None:
        """JWK set is not refetched for unknown key IDs within cooldown."""
        app = make_app()
        validate(app, make_token())
        with raises_unauthorized():
            validate(app, make_token(kid="other-key"))
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI]

    def test_forged_signature(self, idp: MagicMock) -> None:
        """Tokens signed with other keys are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(key=OTHER_PRIVATE_KEY))

    def test_unsigned_token(self, idp: MagicMock) -> None:
        """Unsigned tokens are rejected."""
        token = make_token(key=None, algorithm="none")
        with raises_unauthorized():
            validate(make_app(), token)

    def test_no_key_id(self, idp: MagicMock) -> None:
        """Tokens without key ID are verified with all signing keys."""
        idp.responses[JWKS_URI] = MockResponse(
            {
                "keys": [
                    _public_jwk(OTHER_PRIVATE_KEY, "key-2"),
                    _public_jwk(PRIVATE_KEY, KID),
                ]
            }
        )
        res = validate(make_app(), make_token(kid=None))
        assert res["user_id"] == SUBJECT

    def test_no_key_id_mixed_key_types(self, idp: MagicMock) -> None:
        """Signing keys incompatible with the token's algorithm are skipped."""
        ec_key = ec.generate_private_key(ec.SECP256R1())
        idp.responses[JWKS_URI] = MockResponse(
            {
                "keys": [
                    {
                        **jwt.algorithms.ECAlgorithm.to_jwk(
                            ec_key.public_key(), as_dict=True
                        ),
                        "kid": "ec-key",
                        "use": "sig",
                    },
                    _public_jwk(PRIVATE_KEY, KID),
                ]
            }
        )
        res = validate(make_app(), make_token(kid=None))
        assert res["user_id"] == SUBJECT

    def test_algorithm_not_allowed(self, idp: MagicMock) -> None:
        """Tokens signed with algorithms that are not allowed are rejected."""
        app = make_app(algorithms=["RS512"])
        with raises_unauthorized():
            validate(app, make_token(kid=None))

    def test_no_key_id_forged(self, idp: MagicMock) -> None:
        """Tokens without key ID signed with other keys are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(key=OTHER_PRIVATE_KEY, kid=None))

    def test_expired(self, idp: MagicMock) -> None:
        """Expired tokens are rejected."""
        token = make_token(exp=int(time.time()) - 60)
        with raises_unauthorized():
            validate(make_app(), token)

    def test_expired_allowed(self, idp: MagicMock) -> None:
        """Expired tokens are accepted if configured."""
        token = make_token(exp=int(time.time()) - 60)
        res = validate(make_app(allow_expired=True), token)
        assert res["user_id"] == SUBJECT

    @pytest.mark.parametrize("claim", ["exp", "iat", "sub"])
    def test_missing_required_claim(self, idp: MagicMock, claim: str) -> None:
        """Tokens without required claims are rejected."""
        with raises_unauthorized():
            claims: dict[str, Any] = {claim: None}
            validate(make_app(), make_token(**claims))

    def test_custom_identity_claim(self, idp: MagicMock) -> None:
        """Identity is taken from the configured claim."""
        app = make_app(claim_identity="email")
        res = validate(app, make_token(email="alice@example.org"))
        assert res["user_id"] == "alice@example.org"

    def test_audience(self, idp: MagicMock) -> None:
        """Tokens for any of the configured audiences are accepted."""
        app = make_app(audience=["other", AUDIENCE])
        res = validate(app, make_token(aud=[AUDIENCE, "more"]))
        assert res["user_id"] == SUBJECT

    def test_issuer_audience(self, idp: MagicMock) -> None:
        """Tokens are validated with the audience of their issuer."""
        app = make_app(issuers=[{"issuer": ISSUER, "audience": ["own"]}], audience=None)
        assert validate(app, make_token(aud="own"))["user_id"] == SUBJECT
        with raises_unauthorized():
            validate(app, make_token())
        # falls back to the global audience
        app = make_app(issuers=[{"issuer": ISSUER}])
        assert validate(app, make_token())["user_id"] == SUBJECT

    def test_audience_mismatch(self, idp: MagicMock) -> None:
        """Tokens for other audiences are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(aud="other"))

    def test_audience_missing(self, idp: MagicMock) -> None:
        """Tokens without audience are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(aud=None))

    @pytest.mark.parametrize("audience", [None, [AUDIENCE]])
    def test_audience_not_verified(
        self, idp: MagicMock, audience: list[str] | None
    ) -> None:
        """Audiences are not validated if disabled explicitly."""
        app = make_app(audience=audience, verify_audience=False)
        res = validate(app, make_token(aud="other"))
        assert res["user_id"] == SUBJECT

    @pytest.mark.parametrize(
        "typ", ["at+jwt", "application/at+jwt", "AT+JWT", "Application/AT+JWT"]
    )
    def test_token_type(self, idp: MagicMock, typ: str) -> None:
        """Access tokens (RFC 9068) of either `typ` are accepted."""
        res = validate(make_app(), make_token(typ=typ))
        assert res["user_id"] == SUBJECT

    @pytest.mark.parametrize("typ", ["JWT", "id_token+jwt", None, 1, ["at+jwt"]])
    def test_token_type_rejected(self, idp: MagicMock, typ: Any) -> None:
        """Tokens of other or no type (e.g., ID tokens) are rejected.

        They are rejected without requests to the identity provider.
        """
        with raises_unauthorized():
            validate(make_app(), make_token(typ=typ))
        idp.assert_not_called()

    @pytest.mark.parametrize("token_types", [["JWT"], [" jwt "]])
    def test_token_type_configured(
        self, idp: MagicMock, token_types: list[str]
    ) -> None:
        """Only tokens of the configured types are accepted.

        Configured types are compared case-insensitively, ignoring surrounding
        whitespace.
        """
        app = make_app(token_types=token_types)
        assert validate(app, make_token(typ="JWT"))["user_id"] == SUBJECT
        with raises_unauthorized():
            validate(app, make_token(typ="at+jwt"))

    @pytest.mark.parametrize("typ", ["JWT", None])
    def test_token_type_not_verified(self, idp: MagicMock, typ: str | None) -> None:
        """Token types are not validated if disabled explicitly."""
        app = make_app(verify_token_type=False)
        res = validate(app, make_token(typ=typ))
        assert res["user_id"] == SUBJECT

    @pytest.mark.parametrize("claim", ["client_id", "azp"])
    def test_clients(self, idp: MagicMock, claim: str) -> None:
        """Tokens of any of the configured clients are accepted.

        The client is identified by the `client_id` claim (RFC 9068) or the
        `azp` claim (OpenID Connect).
        """
        app = make_app(clients=["other", CLIENT])
        claims: dict[str, Any] = {claim: CLIENT}
        res = validate(app, make_token(**claims))
        assert res["user_id"] == SUBJECT

    def test_clients_client_id_precedence(self, idp: MagicMock) -> None:
        """The `client_id` claim takes precedence over the `azp` claim."""
        app = make_app(clients=[CLIENT])
        res = validate(app, make_token(client_id=CLIENT, azp="other"))
        assert res["user_id"] == SUBJECT
        with raises_unauthorized():
            validate(app, make_token(client_id="other", azp=CLIENT))

    @pytest.mark.parametrize(
        "claims",
        [
            {"client_id": "other"},
            {"azp": "other"},
            {"client_id": "other", "azp": "other"},
            {"client_id": [CLIENT]},
            # no fallback to `azp` for invalid `client_id`
            {"client_id": [CLIENT], "azp": CLIENT},
            {"client_id": "", "azp": CLIENT},
            {},
        ],
    )
    def test_clients_rejected(self, idp: MagicMock, claims: dict[str, Any]) -> None:
        """Tokens of other clients or without client claims are rejected."""
        app = make_app(clients=[CLIENT])
        with raises_unauthorized():
            validate(app, make_token(**claims))

    def test_clients_after_signature(
        self, idp: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Client claims are only checked after signature verification."""
        checks: list[dict[str, Any]] = []
        verify_client = auth._verify_client

        def record(claims: dict[str, Any], conf: AuthConfig) -> None:
            checks.append(claims)
            verify_client(claims, conf)

        monkeypatch.setattr(auth, "_verify_client", record)
        app = make_app(clients=[CLIENT])
        with raises_unauthorized():
            validate(app, make_token(key=OTHER_PRIVATE_KEY, client_id=CLIENT))
        assert checks == []
        validate(app, make_token(client_id=CLIENT))
        assert len(checks) == 1
        assert "public_key" in checks[0]  # added by signature verification

    def test_clients_null_client_id(self) -> None:
        """A null `client_id` claim is not replaced by the `azp` claim."""
        conf = AuthConfig(clients=[CLIENT])
        with raises_unauthorized():
            auth._verify_client({"client_id": None, "azp": CLIENT}, conf)

    def test_clients_not_configured(self, idp: MagicMock) -> None:
        """Clients are not checked by default."""
        res = validate(make_app(), make_token(client_id="other"))
        assert res["user_id"] == SUBJECT

    def test_clients_without_audience(self, idp: MagicMock) -> None:
        """Clients can be restricted instead of the audience."""
        app = make_app(audience=None, verify_audience=False, clients=[CLIENT])
        res = validate(app, make_token(aud="other", client_id=CLIENT))
        assert res["user_id"] == SUBJECT
        with raises_unauthorized():
            validate(app, make_token(aud="other", client_id="other"))

    def test_add_key_to_claims(self, idp: MagicMock) -> None:
        """Public key used for verification is added to claims."""
        res = validate(make_app(add_key_to_claims=True), make_token())
        assert res["claims"]["public_key"].startswith("-----BEGIN PUBLIC KEY-----")

    def test_no_key_in_claims(self, idp: MagicMock) -> None:
        """Public key is not added to claims if not configured."""
        res = validate(make_app(add_key_to_claims=False), make_token())
        assert "public_key" not in res["claims"]

    def test_userinfo(self, idp: MagicMock) -> None:
        """Tokens are additionally validated via user info endpoint."""
        res = validate(make_app(validate_userinfo=True), make_token())
        assert res["user_id"] == SUBJECT
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI, USERINFO_URI]
        call = idp.call_args_list[-1]
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["headers"]["Authorization"].startswith("Bearer ")

    def test_userinfo_after_signature(self, idp: MagicMock) -> None:
        """Tokens are not sent to user info endpoint before verification."""
        app = make_app(validate_userinfo=True)
        with raises_unauthorized():
            validate(app, make_token(key=OTHER_PRIVATE_KEY))
        assert USERINFO_URI not in urls(idp)

    def test_userinfo_subject_mismatch(self, idp: MagicMock) -> None:
        """User info must be for the token's subject."""
        idp.responses[USERINFO_URI] = MockResponse({"sub": "mallory"})
        with raises_unauthorized():
            validate(make_app(validate_userinfo=True), make_token())

    def test_userinfo_rejected(self, idp: MagicMock) -> None:
        """Tokens are invalid if the user info endpoint rejects them."""
        idp.responses[USERINFO_URI] = MockResponse({}, status_code=401)
        with raises_unauthorized():
            validate(make_app(validate_userinfo=True), make_token())

    @pytest.mark.parametrize("status_code", [403, 500, 503])
    def test_userinfo_unavailable(self, idp: MagicMock, status_code: int) -> None:
        """Tokens are rejected, but not invalid, if user info cannot be fetched."""
        idp.responses[USERINFO_URI] = MockResponse({}, status_code=status_code)
        with raises_unauthorized("Bearer"):
            validate(make_app(validate_userinfo=True), make_token())


class TestUserinfoClaims:
    """Tests for adding claims from the user info endpoint."""

    @pytest.fixture(autouse=True)
    def userinfo(self, idp: MagicMock) -> Iterator[None]:
        """User info with claims that are not in tokens."""
        auth._userinfo_cache.clear()
        idp.responses[USERINFO_URI] = MockResponse(
            {
                "sub": SUBJECT,
                "eduperson_entitlement": ["urn:example:admins"],
                "email": "alice@example.org",
            }
        )
        yield
        auth._userinfo_cache.clear()

    def test_added(self, idp: MagicMock) -> None:
        """Configured claims are added from user info, others are not."""
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        res = validate(app, make_token())
        assert res["claims"]["eduperson_entitlement"] == ["urn:example:admins"]
        assert "email" not in res["claims"]
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI, USERINFO_URI]
        call = idp.call_args_list[-1]
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["headers"]["Authorization"].startswith("Bearer ")

    def test_issuer_claims(self, idp: MagicMock) -> None:
        """Claims are added as configured for the token's issuer."""
        app = make_app(
            issuers=[{"issuer": ISSUER, "userinfo_claims": ["email"]}],
            userinfo_claims=["eduperson_entitlement"],
        )
        res = validate(app, make_token())
        assert res["claims"]["email"] == "alice@example.org"
        assert "eduperson_entitlement" not in res["claims"]
        # an empty list adds none
        auth._userinfo_cache.clear()
        app = make_app(
            issuers=[{"issuer": ISSUER, "userinfo_claims": []}],
            userinfo_claims=["eduperson_entitlement"],
        )
        res = validate(app, make_token())
        assert "eduperson_entitlement" not in res["claims"]

    def test_not_configured(self, idp: MagicMock) -> None:
        """User info is not requested by default."""
        res = validate(make_app(), make_token())
        assert "eduperson_entitlement" not in res["claims"]
        assert USERINFO_URI not in urls(idp)

    def test_token_claims_kept(self, idp: MagicMock) -> None:
        """Claims of the token are not replaced by user info."""
        app = make_app(userinfo_claims=["eduperson_entitlement", "email"])
        token = make_token(email="a@example.org")
        res = validate(app, token)
        assert res["claims"]["email"] == "a@example.org"
        assert res["claims"]["eduperson_entitlement"] == ["urn:example:admins"]

    def test_in_token(self, idp: MagicMock) -> None:
        """User info is not requested if the token has all claims."""
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        res = validate(app, make_token(eduperson_entitlement=["urn:x"]))
        assert res["claims"]["eduperson_entitlement"] == ["urn:x"]
        assert USERINFO_URI not in urls(idp)

    def test_missing(self, idp: MagicMock) -> None:
        """Claims missing from user info are not added."""
        app = make_app(userinfo_claims=["eduperson_entitlement", "groups"])
        res = validate(app, make_token())
        assert "groups" not in res["claims"]

    def test_after_signature(self, idp: MagicMock) -> None:
        """Tokens are not sent to user info endpoint before verification."""
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        with raises_unauthorized():
            validate(app, make_token(key=OTHER_PRIVATE_KEY))
        assert USERINFO_URI not in urls(idp)

    def test_subject_mismatch(self, idp: MagicMock) -> None:
        """User info must be for the token's subject."""
        idp.responses[USERINFO_URI] = MockResponse(
            {"sub": "mallory", "eduperson_entitlement": ["urn:x"]}
        )
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        with raises_unauthorized():
            validate(app, make_token())

    def test_subject_missing(self, idp: MagicMock) -> None:
        """User info is rejected if neither it nor the token has a subject."""
        idp.responses[USERINFO_URI] = MockResponse({"eduperson_entitlement": ["urn:x"]})
        app = make_app(userinfo_claims=["eduperson_entitlement"], claim_identity="jti")
        with raises_unauthorized():
            validate(app, make_token(sub=None, jti="j"))

    def test_unavailable(self, idp: MagicMock) -> None:
        """Tokens are rejected if user info cannot be fetched."""
        idp.responses[USERINFO_URI] = MockResponse({}, status_code=503)
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        with raises_unauthorized("Bearer"):
            validate(app, make_token())

    def test_cached(self, idp: MagicMock) -> None:
        """User info is cached per token."""
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        token = make_token()
        for _ in range(2):
            res = validate(app, token)
            assert "eduperson_entitlement" in res["claims"]
        assert urls(idp).count(USERINFO_URI) == 1
        validate(app, make_token(jti="other"))
        assert urls(idp).count(USERINFO_URI) == 2

    def test_cache_expired(self, idp: MagicMock) -> None:
        """User info is fetched again after TTL."""
        app = make_app(userinfo_claims=["eduperson_entitlement"], cache_ttl=0)
        token = make_token()
        validate(app, token)
        validate(app, token)
        assert urls(idp).count(USERINFO_URI) == 2

    def test_not_cached_beyond_expiry(self, idp: MagicMock) -> None:
        """User info is not cached beyond the token's expiry."""
        app = make_app(userinfo_claims=["eduperson_entitlement"], allow_expired=True)
        token = make_token(exp=int(time.time()) - 10)
        validate(app, token)
        validate(app, token)
        assert urls(idp).count(USERINFO_URI) == 2

    def test_cache_ttl_per_app(self, idp: MagicMock) -> None:
        """Cached user info is not used beyond the app's own TTL."""
        token = make_token()
        validate(make_app(userinfo_claims=["eduperson_entitlement"]), token)
        app = make_app(userinfo_claims=["eduperson_entitlement"], cache_ttl=0)
        validate(app, token)
        assert urls(idp).count(USERINFO_URI) == 2

    def test_claims_copied(self, idp: MagicMock) -> None:
        """Changes to returned claims do not affect cached user info."""
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        token = make_token()
        validate(app, token)["claims"]["eduperson_entitlement"].append("x")
        res = validate(app, token)
        assert res["claims"]["eduperson_entitlement"] == ["urn:example:admins"]

    def test_invalid_expiry_not_cached(self, idp: MagicMock) -> None:
        """User info is not cached for tokens with non-numeric expiry."""
        app = make_app(userinfo_claims=["eduperson_entitlement"], allow_expired=True)
        token = make_token(exp="never")
        validate(app, token)
        validate(app, token)
        assert urls(idp).count(USERINFO_URI) == 2

    def test_cache_size(self, idp: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        """Least recently added user info is evicted from full cache."""
        monkeypatch.setattr(auth, "USERINFO_CACHE_SIZE", 2)
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        tokens = [make_token(jti=str(i)) for i in range(3)]
        for token in tokens:
            validate(app, token)
        assert len(auth._userinfo_cache) == 2
        validate(app, tokens[0])
        assert urls(idp).count(USERINFO_URI) == 4

    def test_expired_evicted(self, idp: MagicMock) -> None:
        """Expired user info is evicted when user info is cached."""
        now = time.monotonic()
        auth._userinfo_cache["expired"] = (now - 2, now - 1, {})
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        validate(app, make_token())
        assert "expired" not in auth._userinfo_cache
        assert len(auth._userinfo_cache) == 1

    def test_validate_userinfo_not_cached(self, idp: MagicMock) -> None:
        """With `validate_userinfo`, user info is fetched every time."""
        app = make_app(
            userinfo_claims=["eduperson_entitlement"], validate_userinfo=True
        )
        token = make_token()
        for _ in range(2):
            res = validate(app, token)
            assert "eduperson_entitlement" in res["claims"]
        assert urls(idp).count(USERINFO_URI) == 2


class TestAuthConfig:
    """Tests for authentication config."""

    def test_defaults(self) -> None:
        """Defaults: no trusted issuers, local signature verification."""
        conf = AuthConfig()
        assert conf.issuers == []
        assert conf.audience is None
        assert conf.verify_audience is True
        assert conf.validate_userinfo is False
        assert conf.userinfo_claims == []

    @pytest.mark.parametrize("audience", [None, [], [""], [AUDIENCE, " "]])
    def test_audience_required(self, audience: list[str] | None) -> None:
        """Trusted issuers require an audience, unless opted out."""
        with pytest.raises(ValidationError, match="verify_audience"):
            AuthConfig(issuers=[ISSUER], audience=audience)

    def test_issuer_audiences(self) -> None:
        """Issuers with audiences of their own need no global audience."""
        own = {"issuer": ISSUER, "audience": ["own"]}
        conf = AuthConfig.model_validate({"issuers": [own]})
        assert conf.get_audience(ISSUER) == ["own"]
        assert conf.get_audience(OTHER_ISSUER) is None
        with pytest.raises(ValidationError, match="verify_audience"):
            AuthConfig.model_validate({"issuers": [own, OTHER_ISSUER]})
        conf = AuthConfig.model_validate(
            {"issuers": [own, OTHER_ISSUER], "audience": [AUDIENCE]}
        )
        assert conf.get_audience(OTHER_ISSUER) == [AUDIENCE]

    @pytest.mark.parametrize(
        "settings,match",
        [
            ({"audience": []}, "at least 1 item"),
            ({"audience": ["a", " "]}, "blank"),
            ({"claim_groups": ""}, "at least 1 character"),
            ({"claim_groups": " groups"}, "whitespace"),
            ({"userinfo_claims": ["azp"]}, "azp"),
        ],
    )
    def test_issuer_settings_invalid(
        self, settings: dict[str, Any], match: str
    ) -> None:
        """Invalid settings of issuers are rejected."""
        with pytest.raises(ValidationError, match=match):
            AuthConfig.model_validate(
                {"issuers": [{"issuer": ISSUER, **settings}], "audience": ["a"]}
            )

    def test_issuer_listed_with_different_settings(self) -> None:
        """Issuers listed twice must have the same settings."""
        with pytest.raises(ValidationError, match="different settings"):
            AuthConfig.model_validate(
                {
                    "issuers": [ISSUER, {"issuer": ISSUER, "audience": ["x"]}],
                    "audience": [AUDIENCE],
                }
            )

    def test_issuer_userinfo_claims(self) -> None:
        """User info claims of issuers fall back to the global ones."""
        conf = AuthConfig.model_validate(
            {
                "issuers": [{"issuer": ISSUER, "userinfo_claims": ["email"]}],
                "audience": [AUDIENCE],
                "userinfo_claims": ["groups"],
            }
        )
        assert conf.get_userinfo_claims(ISSUER) == ["email"]
        assert conf.get_userinfo_claims(OTHER_ISSUER) == ["groups"]

    def test_audience_opt_out(self) -> None:
        """Audience is not required if its validation is disabled."""
        conf = AuthConfig(issuers=[ISSUER], verify_audience=False)
        assert conf.audience is None

    def test_token_types_default(self) -> None:
        """Access tokens as per RFC 9068 are required by default."""
        conf = AuthConfig()
        assert conf.verify_token_type is True
        assert conf.token_types == ["at+jwt", "application/at+jwt"]

    @pytest.mark.parametrize("token_types", [[], [""], ["at+jwt", " "]])
    def test_token_types_required(self, token_types: list[str]) -> None:
        """Token type validation requires token types, unless opted out."""
        with pytest.raises(ValidationError, match="verify_token_type"):
            AuthConfig(token_types=token_types)

    def test_token_types_opt_out(self) -> None:
        """Token types are not required if their validation is disabled."""
        conf = AuthConfig(token_types=[], verify_token_type=False)
        assert conf.token_types == []

    def test_clients_default(self) -> None:
        """Clients are not restricted by default."""
        assert AuthConfig().clients is None

    @pytest.mark.parametrize("clients", [[], [""], [CLIENT, " "]])
    def test_clients_blank(self, clients: list[str]) -> None:
        """Restricting clients requires non-blank client IDs."""
        with pytest.raises(ValidationError, match="clients"):
            AuthConfig(clients=clients)

    def test_clients_without_audience(self) -> None:
        """Clients can be restricted instead of the audience."""
        conf = AuthConfig(issuers=[ISSUER], verify_audience=False, clients=[CLIENT])
        assert conf.audience is None
        assert conf.clients == [CLIENT]

    def test_clients_audience_required(self) -> None:
        """Restricting clients does not replace the audience by itself."""
        with pytest.raises(ValidationError, match="verify_audience"):
            AuthConfig(issuers=[ISSUER], clients=[CLIENT])

    @pytest.mark.parametrize(
        "field", ["validation_methods", "validation_checks", "claim_issuer"]
    )
    def test_removed_fields(self, field: str) -> None:
        """Removed settings are rejected."""
        with pytest.raises(ValidationError):
            AuthConfig.model_validate({field: "any"})


class TestLogAuthConfigWarnings:
    """Tests for `log_auth_config_warnings()`."""

    def test_warnings(self, caplog: pytest.LogCaptureFixture) -> None:
        """Insecure or incomplete settings are logged as warnings."""
        conf = AuthConfig(
            issuers=[],
            verify_audience=False,
            verify_token_type=False,
            allow_expired=True,
        )
        with caplog.at_level(logging.WARNING):
            log_auth_config_warnings(conf)
        assert "No trusted token issuers" in caplog.text
        assert "audience" in caplog.text
        assert "clients are not restricted" in caplog.text
        assert "verify_token_type" in caplog.text
        assert "Expired" in caplog.text

    def test_no_warnings(self, caplog: pytest.LogCaptureFixture) -> None:
        """Complete and secure settings are not warned about."""
        conf = AuthConfig(issuers=[ISSUER], audience=[AUDIENCE])
        with caplog.at_level(logging.WARNING):
            log_auth_config_warnings(conf)
        assert caplog.text == ""

    def test_service_clients_of_several_issuers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Service clients bound to issuers are not warned about."""
        conf = AuthConfig(
            issuers=[ISSUER, OTHER_ISSUER],
            audience=[AUDIENCE],
            service_clients=[ServiceClientConfig(client_id=CLIENT, issuer=ISSUER)],
        )
        with caplog.at_level(logging.WARNING):
            log_auth_config_warnings(conf)
        assert "service client" not in caplog.text

    @pytest.mark.parametrize("on_behalf_of", [False, True])
    def test_on_behalf_of_header(
        self, caplog: pytest.LogCaptureFixture, on_behalf_of: bool
    ) -> None:
        """A header without service clients acting for users is warned about."""
        conf = AuthConfig.model_validate(
            {
                "issuers": [ISSUER],
                "audience": [AUDIENCE],
                "service_clients": [
                    {"client_id": CLIENT, "on_behalf_of": on_behalf_of}
                ],
                "on_behalf_of_header": "X-On-Behalf-Of",
            }
        )
        with caplog.at_level(logging.WARNING):
            log_auth_config_warnings(conf)
        assert ("on_behalf_of_header" in caplog.text) is not on_behalf_of

    def test_clients_instead_of_audience(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Disabled audience validation is not warned about with clients."""
        conf = AuthConfig(issuers=[ISSUER], verify_audience=False, clients=[CLIENT])
        with caplog.at_level(logging.WARNING):
            log_auth_config_warnings(conf)
        assert caplog.text == ""


@pytest.mark.parametrize(
    "auth_conf,refused",
    [
        ({}, False),
        ({"service_clients": [{"client_id": CLIENT}]}, True),
        ({"on_behalf_of_header": "X-On-Behalf-Of"}, True),
    ],
)
def test_require_no_service_clients(
    monkeypatch: pytest.MonkeyPatch, auth_conf: dict[str, Any], refused: bool
) -> None:
    """Apps may refuse service clients and the header naming their users."""
    monkeypatch.setattr(access, "_configs", {})
    conf = AuthConfig.model_validate(
        {"issuers": [ISSUER], "audience": [AUDIENCE], **auth_conf}
    )
    app = Flask(__name__)
    set_app_config(app, Config.model_validate({"security": {"auth": conf}}))
    # another app, created later, with the default configuration
    set_app_config(Flask(__name__), Config())
    with app.app_context():
        if refused:
            with pytest.raises(RuntimeError, match=r"security\.auth\.service_clients"):
                require_no_service_clients()
        else:
            require_no_service_clients()
    # the app given, outside its context
    if refused:
        with pytest.raises(RuntimeError, match=r"security\.auth\.service_clients"):
            require_no_service_clients(app)
    else:
        require_no_service_clients(app)


class TestServiceClients:
    """Tests for recognizing tokens of service clients."""

    SERVICE_CLIENTS: ClassVar[list[dict[str, Any]]] = [
        {"client_id": "conquest"},
        {"client_id": "protes", "subject": "uuid-protes"},
    ]

    @pytest.fixture(autouse=True)
    def userinfo(self, idp: MagicMock) -> Iterator[None]:
        """User info cache cleared."""
        auth._userinfo_cache.clear()
        yield
        auth._userinfo_cache.clear()

    def _app(self, **auth_conf: Any) -> Flask:
        return make_app(
            service_clients=self.SERVICE_CLIENTS,
            userinfo_claims=["eduperson_entitlement"],
            **auth_conf,
        )

    @pytest.mark.parametrize(
        "claims,client",
        [
            ({"sub": "conquest", "client_id": "conquest"}, "conquest"),
            ({"sub": "conquest", "azp": "conquest"}, "conquest"),
            ({"sub": "uuid-protes", "client_id": "protes"}, "protes"),
        ],
    )
    def test_service_token(
        self, idp: MagicMock, claims: dict[str, Any], client: str
    ) -> None:
        """Service tokens are accepted without a user info request."""
        res = validate(self._app(), make_token(**claims))
        assert res["service_client"] == client
        assert res["user_id"] == claims["sub"]
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI]

    def test_validate_userinfo_skipped(self, idp: MagicMock) -> None:
        """Service tokens are not validated via user info either."""
        app = self._app(validate_userinfo=True)
        res = validate(app, make_token(sub="conquest", client_id="conquest"))
        assert res["service_client"] == "conquest"
        assert USERINFO_URI not in urls(idp)

    @pytest.mark.parametrize(
        "claims",
        [
            # user tokens obtained by a service client
            {"client_id": "conquest"},
            {"azp": "conquest"},
            {"sub": "conquest", "client_id": "protes"},
            # other clients
            {"sub": "other", "client_id": "other"},
            {"client_id": None},
            {"sub": "conquest", "client_id": ["conquest"]},
            # `client_id` takes precedence over `azp`
            {"sub": "conquest", "client_id": "other", "azp": "conquest"},
        ],
    )
    def test_user_token(self, idp: MagicMock, claims: dict[str, Any]) -> None:
        """Other tokens are user tokens, as before."""
        idp.responses[USERINFO_URI] = MockResponse({"sub": claims.get("sub", SUBJECT)})
        res = validate(self._app(), make_token(**claims))
        assert res["service_client"] is None
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI, USERINFO_URI]

    def test_other_issuer(self, idp: MagicMock) -> None:
        """Tokens of other issuers than the client's are user tokens."""
        app = make_app(
            issuers=[ISSUER, OTHER_ISSUER],
            service_clients=[{"client_id": "conquest", "issuer": OTHER_ISSUER}],
            userinfo_claims=["eduperson_entitlement"],
        )
        idp.responses[USERINFO_URI] = MockResponse({"sub": "conquest"})
        res = validate(app, make_token(sub="conquest", client_id="conquest"))
        assert res["service_client"] is None
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI, USERINFO_URI]

    def test_not_configured(self, idp: MagicMock) -> None:
        """Without service clients, all tokens are user tokens."""
        token = make_token(sub="conquest", client_id="conquest")
        assert validate(make_app(), token)["service_client"] is None

    def test_get_service_client(self) -> None:
        """Service clients are looked up by issuer, client and subject."""
        conf = AuthConfig.model_validate(
            {
                "issuers": [ISSUER],
                "audience": [AUDIENCE],
                "service_clients": self.SERVICE_CLIENTS,
            }
        )
        claims = {"iss": ISSUER, "sub": "uuid-protes", "client_id": "protes"}
        client = auth.get_service_client(claims, conf)
        assert client is not None
        assert client.client_id == "protes"
        assert auth.get_service_client({"iss": ISSUER, "sub": "protes"}, conf) is None
        # the only trusted issuer is implied, but must match
        other = {**claims, "iss": OTHER_ISSUER}
        assert auth.get_service_client(other, conf) is None
        assert auth.get_service_client({**claims, "iss": None}, conf) is None

    @pytest.mark.parametrize(
        "issuer,client",
        [(ISSUER, None), (OTHER_ISSUER, "conquest"), ("https://x.example.org", None)],
    )
    def test_get_service_client_issuers(self, issuer: str, client: str | None) -> None:
        """With several trusted issuers, the client's issuer must match."""
        conf = AuthConfig.model_validate(
            {
                "issuers": [ISSUER, OTHER_ISSUER],
                "audience": [AUDIENCE],
                "service_clients": [{"client_id": "conquest", "issuer": OTHER_ISSUER}],
            }
        )
        claims = {"iss": issuer, "sub": "conquest", "client_id": "conquest"}
        res = auth.get_service_client(claims, conf)
        assert (None if res is None else res.client_id) == client

    def test_get_client_issuer(self) -> None:
        """The issuer of service clients is the only trusted one by default."""
        client = ServiceClientConfig(client_id="conquest")
        one = AuthConfig(issuers=[ISSUER], audience=[AUDIENCE])
        two = AuthConfig(issuers=[ISSUER, OTHER_ISSUER], audience=[AUDIENCE])
        assert auth.get_client_issuer(client, one) == ISSUER
        assert auth.get_client_issuer(client, two) is None
        other = ServiceClientConfig(client_id="conquest", issuer=OTHER_ISSUER)
        assert auth.get_client_issuer(other, two) == OTHER_ISSUER

    def test_get_service_client_no_issuers(self) -> None:
        """Without trusted issuers, there are no service tokens."""
        conf = AuthConfig.model_validate({"service_clients": self.SERVICE_CLIENTS})
        claims = {"iss": ISSUER, "sub": "conquest", "client_id": "conquest"}
        assert auth.get_service_client(claims, conf) is None


class TestNamespaceSubject:
    """Tests for `namespace_subject()`."""

    def test_one_issuer(self) -> None:
        """Subjects of a single issuer are bare, as before."""
        conf = AuthConfig(issuers=[ISSUER], audience=[AUDIENCE])
        assert auth.namespace_subject("a|b", ISSUER, conf) == "a|b"
        assert auth.namespace_subject("alice", None, conf) == "alice"
        assert auth.namespace_subject("alice", "https://x", AuthConfig()) == "alice"

    def test_several_issuers(self) -> None:
        """Subjects of several issuers are namespaced by issuer by default."""
        conf = AuthConfig(issuers=[ISSUER, OTHER_ISSUER], audience=[AUDIENCE])
        assert auth.namespace_subject("alice", ISSUER, conf) == f"{ISSUER}|alice"
        assert (
            auth.namespace_subject("alice", OTHER_ISSUER, conf)
            == f"{OTHER_ISSUER}|alice"
        )
        # untrusted or missing issuers
        assert auth.namespace_subject("alice", "https://x", conf) is None
        assert auth.namespace_subject("alice", None, conf) is None
        assert auth.namespace_subject("alice", ["x"], conf) is None

    def test_bare_issuer(self) -> None:
        """Bare subjects next to namespaced ones must not contain `|`."""
        conf = AuthConfig.model_validate(
            {
                "issuers": [
                    {"issuer": ISSUER, "namespace": ""},
                    {"issuer": OTHER_ISSUER, "namespace": "other"},
                ],
                "audience": [AUDIENCE],
            }
        )
        assert auth.namespace_subject("alice", ISSUER, conf) == "alice"
        assert auth.namespace_subject("other|alice", ISSUER, conf) is None
        assert auth.namespace_subject("alice", OTHER_ISSUER, conf) == "other|alice"
        # namespaced subjects may contain `|`
        assert auth.namespace_subject("a|b", OTHER_ISSUER, conf) == "other|a|b"

    def test_current_app(self) -> None:
        """Settings default to those of the current app."""
        app = make_app(issuers=[ISSUER, OTHER_ISSUER])
        with app.app_context():
            assert auth.namespace_subject("bob", ISSUER) == f"{ISSUER}|bob"


class TestUnauthorized:
    """Tests for `_unauthorized()`."""

    def test_invalid_token(self) -> None:
        """Invalid tokens are challenged with error code `invalid_token`."""
        exc = auth._unauthorized("JWT expired")
        assert exc.code == 401
        assert exc.description == "JWT expired"
        assert exc.get_headers()[1:] == [("WWW-Authenticate", INVALID_TOKEN_CHALLENGE)]

    def test_not_invalid_token(self) -> None:
        """No error code if the token is not invalid (RFC 6750, §3.1)."""
        exc = auth._unauthorized("IdP unavailable", invalid_token=False)
        assert exc.get_headers()[1:] == [("WWW-Authenticate", "Bearer")]

    def test_quoted(self) -> None:
        """Parameter values are quoted and escaped."""
        challenge = auth._BearerChallenge("bearer", {"realm": 'a"b'})
        assert str(challenge) == 'Bearer realm="a\\"b"'
