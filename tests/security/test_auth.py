"""Tests for authentication module."""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
AUDIENCE = "foca"


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
        "aud": AUDIENCE,
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
    auth_conf = {"issuers": [ISSUER], "audience": [AUDIENCE], **auth_conf}
    app.config.foca = Config(security={"auth": auth_conf})
    return app


# Challenge of `401 Unauthorized` responses for invalid tokens
INVALID_TOKEN_CHALLENGE = (
    'Bearer error="invalid_token", '
    'error_description="The access token could not be validated"'
)


@contextmanager
def raises_unauthorized(
    challenge: str = INVALID_TOKEN_CHALLENGE,
) -> Iterator[None]:
    """Assert that `Unauthorized` is raised with a Bearer challenge (RFC
    6750, §3.1), by default with error code `invalid_token`.
    """
    with pytest.raises(Unauthorized) as exc_info:
        yield
    headers = exc_info.value.get_headers()
    assert ("WWW-Authenticate", challenge) in headers


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

    def test_no_claims_logged(self, idp, monkeypatch):
        """Only issuer and subject are logged, not tokens or other claims."""
        messages: list[str] = []
        monkeypatch.setattr(auth.logger, "debug", messages.append)
        token = make_token(email="alice@example.org", name="Alice A.")
        validate(make_app(), token)
        logged = " ".join(messages)
        assert (
            f"Token issued by '{ISSUER}' validated for subject '{SUBJECT}'"
            in logged
        )
        for value in [token, "alice@example.org", "Alice A.", "openid"]:
            assert value not in logged

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
        with raises_unauthorized():
            validate(make_app(), token)
        idp.assert_not_called()

    def test_no_trusted_issuers(self, idp):
        """All tokens are rejected if no issuers are trusted."""
        with raises_unauthorized():
            validate(make_app(issuers=[]), make_token())
        idp.assert_not_called()

    def test_missing_issuer(self, idp):
        """Tokens without issuer are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(iss=None))
        idp.assert_not_called()

    def test_undecodable_token(self, idp):
        """Malformed tokens are rejected."""
        with raises_unauthorized():
            validate(make_app(), "not-a-token")

    def test_discovery_issuer_mismatch(self, idp):
        """Discovery documents must be issued for the token's issuer."""
        idp.responses[DISCOVERY_URI].data["issuer"] = "https://evil.org"
        with raises_unauthorized("Bearer"):
            validate(make_app(), make_token())
        assert JWKS_URI not in urls(idp)

    def test_discovery_unavailable(self, idp):
        """Tokens are rejected if the discovery document is unavailable."""
        idp.responses[DISCOVERY_URI] = MockResponse({}, status_code=500)
        with raises_unauthorized("Bearer"):
            validate(make_app(), make_token())

    def test_jwks_uri_missing(self, idp):
        """Tokens are rejected if the issuer announces no JWK set."""
        del idp.responses[DISCOVERY_URI].data["jwks_uri"]
        with raises_unauthorized("Bearer"):
            validate(make_app(), make_token())
        assert urls(idp) == [DISCOVERY_URI]

    def test_jwks_invalid(self, idp):
        """Tokens are rejected if the JWK set is invalid."""
        idp.responses[JWKS_URI] = MockResponse({"keys": []})
        with raises_unauthorized("Bearer"):
            validate(make_app(), make_token())

    def test_jwks_unavailable(self, idp):
        """Tokens are rejected if the JWK set is unavailable."""
        idp.responses[JWKS_URI] = MockResponse({}, status_code=500)
        with raises_unauthorized("Bearer"):
            validate(make_app(), make_token())

    @pytest.mark.parametrize("uri", [DISCOVERY_URI, JWKS_URI])
    def test_failed_fetch_cooldown(self, idp, uri):
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
            with raises_unauthorized("Bearer"):
                validate(app, make_token(kid="other-key"))
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI, JWKS_URI]
        # known keys are still accepted
        assert validate(app, make_token())["user_id"] == SUBJECT

    def test_userinfo_failures_not_throttled(self, idp):
        """Failed user info requests depend on the token, so are repeated."""
        app = make_app(validate_userinfo=True)
        idp.responses[USERINFO_URI] = MockResponse({}, status_code=401)
        for _ in range(2):
            with raises_unauthorized():
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
        with raises_unauthorized():
            validate(app, make_token(kid="other-key"))
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI]

    def test_forged_signature(self, idp):
        """Tokens signed with other keys are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(key=OTHER_PRIVATE_KEY))

    def test_unsigned_token(self, idp):
        """Unsigned tokens are rejected."""
        token = make_token(key=None, algorithm="none")
        with raises_unauthorized():
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
        with raises_unauthorized():
            validate(app, make_token(kid=None))

    def test_no_key_id_forged(self, idp):
        """Tokens without key ID signed with other keys are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(key=OTHER_PRIVATE_KEY, kid=None))

    def test_expired(self, idp):
        """Expired tokens are rejected."""
        token = make_token(exp=int(time.time()) - 60)
        with raises_unauthorized():
            validate(make_app(), token)

    def test_expired_allowed(self, idp):
        """Expired tokens are accepted if configured."""
        token = make_token(exp=int(time.time()) - 60)
        res = validate(make_app(allow_expired=True), token)
        assert res["user_id"] == SUBJECT

    @pytest.mark.parametrize("claim", ["exp", "iat", "sub"])
    def test_missing_required_claim(self, idp, claim):
        """Tokens without required claims are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(**{claim: None}))

    def test_custom_identity_claim(self, idp):
        """Identity is taken from the configured claim."""
        app = make_app(claim_identity="email")
        res = validate(app, make_token(email="alice@example.org"))
        assert res["user_id"] == "alice@example.org"

    def test_audience(self, idp):
        """Tokens for any of the configured audiences are accepted."""
        app = make_app(audience=["other", AUDIENCE])
        res = validate(app, make_token(aud=[AUDIENCE, "more"]))
        assert res["user_id"] == SUBJECT

    def test_audience_mismatch(self, idp):
        """Tokens for other audiences are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(aud="other"))

    def test_audience_missing(self, idp):
        """Tokens without audience are rejected."""
        with raises_unauthorized():
            validate(make_app(), make_token(aud=None))

    @pytest.mark.parametrize("audience", [None, [AUDIENCE]])
    def test_audience_not_verified(self, idp, audience):
        """Audiences are not validated if disabled explicitly."""
        app = make_app(audience=audience, verify_audience=False)
        res = validate(app, make_token(aud="other"))
        assert res["user_id"] == SUBJECT

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
        with raises_unauthorized():
            validate(app, make_token(key=OTHER_PRIVATE_KEY))
        assert USERINFO_URI not in urls(idp)

    def test_userinfo_subject_mismatch(self, idp):
        """User info must be for the token's subject."""
        idp.responses[USERINFO_URI] = MockResponse({"sub": "mallory"})
        with raises_unauthorized():
            validate(make_app(validate_userinfo=True), make_token())

    def test_userinfo_rejected(self, idp):
        """Tokens are invalid if the user info endpoint rejects them."""
        idp.responses[USERINFO_URI] = MockResponse({}, status_code=401)
        with raises_unauthorized():
            validate(make_app(validate_userinfo=True), make_token())

    @pytest.mark.parametrize("status_code", [500, 503])
    def test_userinfo_unavailable(self, idp, status_code):
        """Tokens are rejected, but not invalid, if user info cannot be
        fetched.
        """
        idp.responses[USERINFO_URI] = MockResponse({}, status_code=status_code)
        with raises_unauthorized("Bearer"):
            validate(make_app(validate_userinfo=True), make_token())


class TestUserinfoClaims:
    """Tests for adding claims from the user info endpoint."""

    @pytest.fixture(autouse=True)
    def userinfo(self, idp):
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

    def test_added(self, idp):
        """Configured claims are added from user info, others are not."""
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        res = validate(app, make_token())
        assert res["claims"]["eduperson_entitlement"] == ["urn:example:admins"]
        assert "email" not in res["claims"]
        assert urls(idp) == [DISCOVERY_URI, JWKS_URI, USERINFO_URI]
        call = idp.call_args_list[-1]
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["headers"]["Authorization"].startswith("Bearer ")

    def test_not_configured(self, idp):
        """User info is not requested by default."""
        res = validate(make_app(), make_token())
        assert "eduperson_entitlement" not in res["claims"]
        assert USERINFO_URI not in urls(idp)

    def test_token_claims_kept(self, idp):
        """Claims of the token are not replaced by user info."""
        app = make_app(userinfo_claims=["eduperson_entitlement", "email"])
        token = make_token(email="a@example.org")
        res = validate(app, token)
        assert res["claims"]["email"] == "a@example.org"
        assert res["claims"]["eduperson_entitlement"] == ["urn:example:admins"]

    def test_in_token(self, idp):
        """User info is not requested if the token has all claims."""
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        res = validate(app, make_token(eduperson_entitlement=["urn:x"]))
        assert res["claims"]["eduperson_entitlement"] == ["urn:x"]
        assert USERINFO_URI not in urls(idp)

    def test_missing(self, idp):
        """Claims missing from user info are not added."""
        app = make_app(userinfo_claims=["eduperson_entitlement", "groups"])
        res = validate(app, make_token())
        assert "groups" not in res["claims"]

    def test_after_signature(self, idp):
        """Tokens are not sent to user info endpoint before verification."""
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        with raises_unauthorized():
            validate(app, make_token(key=OTHER_PRIVATE_KEY))
        assert USERINFO_URI not in urls(idp)

    def test_subject_mismatch(self, idp):
        """User info must be for the token's subject."""
        idp.responses[USERINFO_URI] = MockResponse(
            {"sub": "mallory", "eduperson_entitlement": ["urn:x"]}
        )
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        with raises_unauthorized():
            validate(app, make_token())

    def test_subject_missing(self, idp):
        """User info is rejected if neither it nor the token has a subject."""
        idp.responses[USERINFO_URI] = MockResponse(
            {"eduperson_entitlement": ["urn:x"]}
        )
        app = make_app(
            userinfo_claims=["eduperson_entitlement"], claim_identity="jti"
        )
        with raises_unauthorized():
            validate(app, make_token(sub=None, jti="j"))

    def test_unavailable(self, idp):
        """Tokens are rejected if user info cannot be fetched."""
        idp.responses[USERINFO_URI] = MockResponse({}, status_code=503)
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        with raises_unauthorized("Bearer"):
            validate(app, make_token())

    def test_cached(self, idp):
        """User info is cached per token."""
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        token = make_token()
        for _ in range(2):
            res = validate(app, token)
            assert "eduperson_entitlement" in res["claims"]
        assert urls(idp).count(USERINFO_URI) == 1
        validate(app, make_token(jti="other"))
        assert urls(idp).count(USERINFO_URI) == 2

    def test_cache_expired(self, idp):
        """User info is fetched again after TTL."""
        app = make_app(userinfo_claims=["eduperson_entitlement"], cache_ttl=0)
        token = make_token()
        validate(app, token)
        validate(app, token)
        assert urls(idp).count(USERINFO_URI) == 2

    def test_not_cached_beyond_expiry(self, idp):
        """User info is not cached beyond the token's expiry."""
        app = make_app(
            userinfo_claims=["eduperson_entitlement"], allow_expired=True
        )
        token = make_token(exp=int(time.time()) - 10)
        validate(app, token)
        validate(app, token)
        assert urls(idp).count(USERINFO_URI) == 2

    def test_cache_ttl_per_app(self, idp):
        """Cached user info is not used beyond the app's own TTL."""
        token = make_token()
        validate(make_app(userinfo_claims=["eduperson_entitlement"]), token)
        app = make_app(userinfo_claims=["eduperson_entitlement"], cache_ttl=0)
        validate(app, token)
        assert urls(idp).count(USERINFO_URI) == 2

    def test_claims_copied(self, idp):
        """Changes to returned claims do not affect cached user info."""
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        token = make_token()
        validate(app, token)["claims"]["eduperson_entitlement"].append("x")
        res = validate(app, token)
        assert res["claims"]["eduperson_entitlement"] == ["urn:example:admins"]

    def test_invalid_expiry_not_cached(self, idp):
        """User info is not cached for tokens with non-numeric expiry."""
        app = make_app(
            userinfo_claims=["eduperson_entitlement"], allow_expired=True
        )
        token = make_token(exp="never")
        validate(app, token)
        validate(app, token)
        assert urls(idp).count(USERINFO_URI) == 2

    def test_cache_size(self, idp, monkeypatch):
        """Least recently added user info is evicted from full cache."""
        monkeypatch.setattr(auth, "USERINFO_CACHE_SIZE", 2)
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        tokens = [make_token(jti=str(i)) for i in range(3)]
        for token in tokens:
            validate(app, token)
        assert len(auth._userinfo_cache) == 2
        validate(app, tokens[0])
        assert urls(idp).count(USERINFO_URI) == 4

    def test_expired_evicted(self, idp):
        """Expired user info is evicted when user info is cached."""
        now = time.monotonic()
        auth._userinfo_cache["expired"] = (now - 2, now - 1, {})
        app = make_app(userinfo_claims=["eduperson_entitlement"])
        validate(app, make_token())
        assert "expired" not in auth._userinfo_cache
        assert len(auth._userinfo_cache) == 1

    def test_validate_userinfo_not_cached(self, idp):
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

    def test_defaults(self):
        """Defaults: no trusted issuers, local signature verification."""
        conf = AuthConfig()
        assert conf.issuers == []
        assert conf.audience is None
        assert conf.verify_audience is True
        assert conf.validate_userinfo is False
        assert conf.userinfo_claims == []

    @pytest.mark.parametrize("audience", [None, [], [""], [AUDIENCE, " "]])
    def test_audience_required(self, audience):
        """Trusted issuers require an audience, unless opted out."""
        with pytest.raises(ValidationError, match="verify_audience"):
            AuthConfig(issuers=[ISSUER], audience=audience)

    def test_audience_opt_out(self):
        """Audience is not required if its validation is disabled."""
        conf = AuthConfig(issuers=[ISSUER], verify_audience=False)
        assert conf.audience is None

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
        conf = AuthConfig(
            issuers=[], verify_audience=False, allow_expired=True
        )
        with caplog.at_level(logging.WARNING):
            log_auth_config_warnings(conf)
        assert "No trusted token issuers" in caplog.text
        assert "audience" in caplog.text
        assert "Expired" in caplog.text

    def test_no_warnings(self, caplog):
        """Complete and secure settings are not warned about."""
        conf = AuthConfig(issuers=[ISSUER], audience=[AUDIENCE])
        with caplog.at_level(logging.WARNING):
            log_auth_config_warnings(conf)
        assert caplog.text == ""


class TestUnauthorized:
    """Tests for `_unauthorized()`."""

    def test_invalid_token(self):
        exc = auth._unauthorized("JWT expired")
        assert exc.code == 401
        assert exc.description == "JWT expired"
        assert exc.get_headers()[1:] == [
            ("WWW-Authenticate", INVALID_TOKEN_CHALLENGE)
        ]

    def test_not_invalid_token(self):
        """No error code if the token is not invalid (RFC 6750, §3.1)."""
        exc = auth._unauthorized("IdP unavailable", invalid_token=False)
        assert exc.get_headers()[1:] == [("WWW-Authenticate", "Bearer")]

    def test_quoted(self):
        """Parameter values are quoted and escaped."""
        challenge = auth._BearerChallenge("bearer", {"realm": 'a"b'})
        assert str(challenge) == 'Bearer realm="a\\"b"'
