"""Tests for authentication module."""

import logging
import time
from unittest.mock import MagicMock

import jwt
import pytest
from connexion.exceptions import Unauthorized
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from flask import Flask
from flask import request as auth_request
from pydantic import ValidationError

from foca.models.config import AuthConfig, Config
from foca.security import auth
from foca.security.auth import log_auth_config_warnings, validate_token

ISSUER = "https://idp.example.org"
DISCOVERY_URI = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER}/jwks"
USERINFO_URI = f"{ISSUER}/userinfo"
KID = "key-1"
SUBJECT = "alice"


def _private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


PRIVATE_KEY = _private_key()
OTHER_PRIVATE_KEY = _private_key()


def _public_jwk(key, kid):
    return {
        **jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
        "kid": kid,
        "use": "sig",
    }


def make_token(key=PRIVATE_KEY, kid=KID, algorithm="RS256", **claims):
    """Create signed token with valid default claims."""
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "iat": now,
        "exp": now + 300,
        "scope": "openid",
    }
    payload.update(claims)
    payload = {key: val for key, val in payload.items() if val is not None}
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(payload, key, algorithm=algorithm, headers=headers)


class MockResponse:
    """Mock response of `requests.get()`."""

    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise auth.requests.HTTPError(str(self.status_code))

    def json(self):
        return self.data


@pytest.fixture
def idp(monkeypatch):
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


def urls(get):
    """URLs requested via mock of `requests.get()`."""
    return [call.args[0] for call in get.call_args_list]


def make_app(**auth_conf):
    """Create Flask app with FOCA config."""
    app = Flask(__name__)
    auth_conf = {"issuers": [ISSUER], **auth_conf}
    app.config.foca = Config(security={"auth": auth_conf})
    return app


def validate(app, token):
    with app.test_request_context():
        return validate_token(token=token)


class TestValidateToken:
    """Tests for `validate_token()`."""

    def test_warnings_logged_once(self, idp, monkeypatch):
        """Settings are checked for warnings on the first validation."""
        calls = []
        monkeypatch.setattr(auth, "log_auth_config_warnings", calls.append)
        app = make_app()
        validate(app, make_token())
        validate(app, make_token())
        assert calls == [app.config.foca.security.auth]

    def test_success(self, idp):
        """Valid token is accepted; verified claims are returned."""
        res = validate(make_app(), make_token())
        assert res["user_id"] == SUBJECT
        assert res["claims"]["iss"] == ISSUER
        assert res["scope"] == "openid"
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI]
        for call in idp.call_args_list:
            assert call.kwargs["allow_redirects"] is False
            assert call.kwargs["timeout"] == 10

    def test_request_headers_unchanged(self, idp):
        """Claims are not added to request headers."""
        app = make_app()
        headers = {"Authorization": "Bearer x", "Content-Type": "text/plain"}
        with app.test_request_context(headers=headers):
            before = dict(auth_request.headers)
            validate_token(token=make_token())
            assert dict(auth_request.headers) == before
            assert "sub" not in auth_request.headers
            assert "user_id" not in auth_request.headers

    def test_untrusted_issuer(self, idp):
        """Tokens of untrusted issuers are rejected without requests."""
        token = make_token(iss="https://evil.example.org")
        with pytest.raises(Unauthorized):
            validate(make_app(), token)
        idp.assert_not_called()

    def test_no_trusted_issuers(self, idp):
        """All tokens are rejected if no issuers are trusted."""
        with pytest.raises(Unauthorized):
            validate(make_app(issuers=[]), make_token())
        idp.assert_not_called()

    def test_missing_issuer(self, idp):
        """Tokens without issuer are rejected."""
        with pytest.raises(Unauthorized):
            validate(make_app(), make_token(iss=None))
        idp.assert_not_called()

    def test_undecodable_token(self, idp):
        """Malformed tokens are rejected."""
        with pytest.raises(Unauthorized):
            validate(make_app(), "not-a-token")

    def test_discovery_issuer_mismatch(self, idp):
        """Discovery documents must be issued for the token's issuer."""
        idp.responses[DISCOVERY_URI].data["issuer"] = "https://evil.org"
        with pytest.raises(Unauthorized):
            validate(make_app(), make_token())
        assert JWKS_URI not in urls(idp)

    def test_discovery_unavailable(self, idp):
        """Tokens are rejected if the discovery document is unavailable."""
        idp.responses[DISCOVERY_URI] = MockResponse({}, status_code=500)
        with pytest.raises(Unauthorized):
            validate(make_app(), make_token())

    def test_jwks_uri_missing(self, idp):
        """Tokens are rejected if the issuer announces no JWK set."""
        del idp.responses[DISCOVERY_URI].data["jwks_uri"]
        with pytest.raises(Unauthorized):
            validate(make_app(), make_token())
        assert urls(idp) == [DISCOVERY_URI]

    def test_jwks_invalid(self, idp):
        """Tokens are rejected if the JWK set is invalid."""
        idp.responses[JWKS_URI] = MockResponse({"keys": []})
        with pytest.raises(Unauthorized):
            validate(make_app(), make_token())

    def test_jwks_unavailable(self, idp):
        """Tokens are rejected if the JWK set is unavailable."""
        idp.responses[JWKS_URI] = MockResponse({}, status_code=500)
        with pytest.raises(Unauthorized):
            validate(make_app(), make_token())

    @pytest.mark.parametrize("uri", [DISCOVERY_URI, JWKS_URI])
    def test_failed_fetch_cooldown(self, idp, uri):
        """Failed fetches are not repeated within cooldown."""
        app = make_app()
        ok = idp.responses[uri]
        idp.responses[uri] = MockResponse({}, status_code=503)
        for _ in range(3):
            with pytest.raises(Unauthorized):
                validate(app, make_token())
        assert urls(idp).count(uri) == 1
        # fetched again after cooldown
        idp.responses[uri] = ok
        auth._failed_fetches[uri] -= auth.JWKS_REFETCH_COOLDOWN
        assert validate(app, make_token())["user_id"] == SUBJECT
        assert urls(idp).count(uri) == 2
        assert uri not in auth._failed_fetches

    def test_failed_refetch_cooldown(self, idp):
        """Failed refetches for unknown key IDs are not repeated within
        cooldown."""
        app = make_app()
        validate(app, make_token())
        idp.responses[JWKS_URI] = MockResponse({}, status_code=503)
        auth._jwk_sets[JWKS_URI] = (
            auth._jwk_sets[JWKS_URI][0] - auth.JWKS_REFETCH_COOLDOWN,
            auth._jwk_sets[JWKS_URI][1],
        )
        for _ in range(3):
            with pytest.raises(Unauthorized):
                validate(app, make_token(kid="other-key"))
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI, JWKS_URI]
        # known keys are still accepted
        assert validate(app, make_token())["user_id"] == SUBJECT

    def test_userinfo_failures_not_throttled(self, idp):
        """Failed user info requests depend on the token, so are repeated."""
        app = make_app(validate_userinfo=True)
        idp.responses[USERINFO_URI] = MockResponse({}, status_code=401)
        for _ in range(2):
            with pytest.raises(Unauthorized):
                validate(app, make_token())
        assert urls(idp).count(USERINFO_URI) == 2

    def test_cached(self, idp):
        """Discovery documents and JWK sets are cached."""
        app = make_app()
        validate(app, make_token())
        validate(app, make_token())
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI]

    def test_cache_expired(self, idp):
        """Discovery documents and JWK sets are refetched after TTL."""
        app = make_app(cache_ttl=0)
        validate(app, make_token())
        validate(app, make_token())
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI] * 2

    def test_key_rotation(self, idp):
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

    def test_unknown_key_id_cooldown(self, idp):
        """JWK set is not refetched for unknown key IDs within cooldown."""
        app = make_app()
        validate(app, make_token())
        with pytest.raises(Unauthorized):
            validate(app, make_token(kid="other-key"))
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI]

    def test_forged_signature(self, idp):
        """Tokens signed with other keys are rejected."""
        with pytest.raises(Unauthorized):
            validate(make_app(), make_token(key=OTHER_PRIVATE_KEY))

    def test_unsigned_token(self, idp):
        """Unsigned tokens are rejected."""
        token = make_token(key=None, algorithm="none")
        with pytest.raises(Unauthorized):
            validate(make_app(), token)

    def test_no_key_id(self, idp):
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

    def test_no_key_id_mixed_key_types(self, idp):
        """Signing keys incompatible with the token's algorithm are
        skipped."""
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

    def test_algorithm_not_allowed(self, idp):
        """Tokens signed with algorithms that are not allowed are
        rejected."""
        app = make_app(algorithms=["RS512"])
        with pytest.raises(Unauthorized):
            validate(app, make_token(kid=None))

    def test_no_key_id_forged(self, idp):
        """Tokens without key ID signed with other keys are rejected."""
        with pytest.raises(Unauthorized):
            validate(make_app(), make_token(key=OTHER_PRIVATE_KEY, kid=None))

    def test_expired(self, idp):
        """Expired tokens are rejected."""
        token = make_token(exp=int(time.time()) - 60)
        with pytest.raises(Unauthorized):
            validate(make_app(), token)

    def test_expired_allowed(self, idp):
        """Expired tokens are accepted if configured."""
        token = make_token(exp=int(time.time()) - 60)
        res = validate(make_app(allow_expired=True), token)
        assert res["user_id"] == SUBJECT

    @pytest.mark.parametrize("claim", ["exp", "iat", "sub"])
    def test_missing_required_claim(self, idp, claim):
        """Tokens without required claims are rejected."""
        with pytest.raises(Unauthorized):
            validate(make_app(), make_token(**{claim: None}))

    def test_custom_identity_claim(self, idp):
        """Identity is taken from the configured claim."""
        app = make_app(claim_identity="email")
        res = validate(app, make_token(email="alice@example.org"))
        assert res["user_id"] == "alice@example.org"

    def test_audience(self, idp):
        """Tokens for the configured audience are accepted."""
        res = validate(make_app(audience=["foca"]), make_token(aud="foca"))
        assert res["user_id"] == SUBJECT

    def test_audience_mismatch(self, idp):
        """Tokens for other audiences are rejected."""
        with pytest.raises(Unauthorized):
            validate(make_app(audience=["foca"]), make_token(aud="other"))

    def test_add_key_to_claims(self, idp):
        """Public key used for verification is added to claims."""
        res = validate(make_app(add_key_to_claims=True), make_token())
        assert res["claims"]["public_key"].startswith(
            "-----BEGIN PUBLIC KEY-----"
        )

    def test_no_key_in_claims(self, idp):
        """Public key is not added to claims if not configured."""
        res = validate(make_app(add_key_to_claims=False), make_token())
        assert "public_key" not in res["claims"]

    def test_userinfo(self, idp):
        """Tokens are additionally validated via user info endpoint."""
        res = validate(make_app(validate_userinfo=True), make_token())
        assert res["user_id"] == SUBJECT
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI, USERINFO_URI]
        call = idp.call_args_list[-1]
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["headers"]["Authorization"].startswith("Bearer ")

    def test_userinfo_after_signature(self, idp):
        """Tokens are not sent to user info endpoint before verification."""
        app = make_app(validate_userinfo=True)
        with pytest.raises(Unauthorized):
            validate(app, make_token(key=OTHER_PRIVATE_KEY))
        assert USERINFO_URI not in urls(idp)

    def test_userinfo_subject_mismatch(self, idp):
        """User info must be for the token's subject."""
        idp.responses[USERINFO_URI] = MockResponse({"sub": "mallory"})
        with pytest.raises(Unauthorized):
            validate(make_app(validate_userinfo=True), make_token())

    def test_userinfo_unavailable(self, idp):
        """Tokens are rejected if user info cannot be fetched."""
        idp.responses[USERINFO_URI] = MockResponse({}, status_code=401)
        with pytest.raises(Unauthorized):
            validate(make_app(validate_userinfo=True), make_token())


class TestAuthConfig:
    """Tests for authentication config."""

    def test_defaults(self):
        """Defaults: no trusted issuers, local signature verification."""
        conf = AuthConfig()
        assert conf.issuers == []
        assert conf.validate_userinfo is False

    @pytest.mark.parametrize(
        "field", ["validation_methods", "validation_checks", "claim_issuer"]
    )
    def test_removed_fields(self, field):
        """Removed settings are rejected."""
        with pytest.raises(ValidationError):
            AuthConfig(**{field: "any"})


class TestLogAuthConfigWarnings:
    """Tests for `log_auth_config_warnings()`."""

    def test_warnings(self, caplog):
        """Insecure or incomplete settings are logged as warnings."""
        conf = AuthConfig(issuers=[], audience=None, allow_expired=True)
        with caplog.at_level(logging.WARNING):
            log_auth_config_warnings(conf)
        assert "No trusted token issuers" in caplog.text
        assert "audience" in caplog.text
        assert "Expired" in caplog.text

    def test_no_warnings(self, caplog):
        """Complete and secure settings are not warned about."""
        conf = AuthConfig(issuers=[ISSUER], audience=["foca"])
        with caplog.at_level(logging.WARNING):
            log_auth_config_warnings(conf)
        assert caplog.text == ""
