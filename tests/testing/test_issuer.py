"""Tests of `foca.testing.issuer`, against FOCA's token validation."""

from typing import Any

import pytest
import requests
from connexion.exceptions import Unauthorized
from flask import Flask

from foca.config.access import _set_app_config
from foca.models.config import AuthConfig, Config, SecurityConfig
from foca.security import auth
from foca.testing.issuer import JWKS_PATH, USERINFO_PATH, Issuer, issuer

AUDIENCE = "my-app"
OIDC_CONFIG_PATH = f"/{auth.OIDC_CONFIG_PATH}"


def _validate(idp: Issuer, token: str, **settings: Any) -> dict[str, Any]:
    """Validate a token as an app trusting the identity provider does.

    Args:
        idp: Identity provider.
        token: Token.
        **settings: Further settings of `security.auth`.

    Returns:
        The token info of FOCA.
    """
    app = Flask(__name__)
    conf = Config(
        security=SecurityConfig(
            auth=AuthConfig(issuers=[idp.url], audience=[AUDIENCE], **settings)
        )
    )
    _set_app_config(app, conf)
    with app.app_context():
        return auth.validate_token(token)


class TestToken:
    """Tokens are validated end to end, without mocks."""

    def test_valid(self, issuer: Issuer) -> None:
        """A token passes all of FOCA's checks, with the audience checked."""
        info = _validate(issuer, issuer.token(sub="bob", aud=AUDIENCE))
        assert info["user_id"] == "bob"

    def test_wrong_audience(self, issuer: Issuer) -> None:
        """The audience is checked."""
        with pytest.raises(Unauthorized):
            _validate(issuer, issuer.token(aud="other"))

    def test_wrong_type(self, issuer: Issuer) -> None:
        """The token type is checked."""
        with pytest.raises(Unauthorized):
            _validate(issuer, issuer.token(aud=AUDIENCE, typ="JWT"))

    def test_expired(self, issuer: Issuer) -> None:
        """Expired tokens are rejected."""
        with pytest.raises(Unauthorized):
            _validate(issuer, issuer.token(aud=AUDIENCE, expires_in=-120))

    def test_other_key(self, issuer: Issuer) -> None:
        """A token signed by another identity provider isn't valid."""
        other = Issuer()
        token = other.token(aud=AUDIENCE, iss=issuer.url)
        other._server.server_close()
        with pytest.raises(Unauthorized):
            _validate(issuer, token)

    def test_groups_and_client(self, issuer: Issuer) -> None:
        """Groups, clients and further claims are in the token."""
        token = issuer.token(
            aud=AUDIENCE, groups=["admins"], client_id="my-client", scope="a b"
        )
        claims = _validate(issuer, token)["claims"]
        assert claims["groups"] == ["admins"]
        assert claims["client_id"] == "my-client"
        assert claims["scope"] == "a b"

    def test_no_groups_or_client(self, issuer: Issuer) -> None:
        """Without them, the claims are left out."""
        claims = _validate(issuer, issuer.token(aud=AUDIENCE))["claims"]
        assert "groups" not in claims
        assert "client_id" not in claims


class TestEndpoints:
    """The endpoints of the identity provider."""

    def test_discovery(self, issuer: Issuer) -> None:
        """The discovery document names the other endpoints."""
        document = requests.get(f"{issuer.url}{OIDC_CONFIG_PATH}", timeout=5).json()
        assert document == {
            "issuer": issuer.url,
            "jwks_uri": f"{issuer.url}{JWKS_PATH}",
            "userinfo_endpoint": f"{issuer.url}{USERINFO_PATH}",
        }

    def test_jwks(self, issuer: Issuer) -> None:
        """The key set has the public key only."""
        keys = requests.get(f"{issuer.url}{JWKS_PATH}", timeout=5).json()["keys"]
        assert len(keys) == 1
        assert keys[0]["kty"] == "RSA"
        assert keys[0]["use"] == "sig"
        assert "d" not in keys[0]

    def test_unknown_path(self, issuer: Issuer) -> None:
        """Other paths aren't found."""
        assert requests.get(f"{issuer.url}/other", timeout=5).status_code == 404

    def test_userinfo_default(self, issuer: Issuer) -> None:
        """User info has the subject of the token by default."""
        response = requests.get(
            f"{issuer.url}{USERINFO_PATH}",
            headers={"Authorization": f"Bearer {issuer.token(sub='bob')}"},
            timeout=5,
        )
        assert response.json() == {"sub": "bob"}

    def test_userinfo_claims(self, issuer: Issuer) -> None:
        """Claims can be set, and are used by FOCA's validation."""
        token = issuer.token(aud=AUDIENCE)
        issuer.set_userinfo(groups=["first"])
        settings: dict[str, Any] = {"userinfo_claims": ["groups"]}
        assert _validate(issuer, token, **settings)["claims"]["groups"] == ["first"]
        # Not read from FOCA's cache of user info
        issuer.set_userinfo(groups=["second"])
        assert _validate(issuer, token, **settings)["claims"]["groups"] == ["second"]

    def test_userinfo_failure(self, issuer: Issuer) -> None:
        """Failures can be set, and reject the token with `validate_userinfo`."""
        issuer.set_userinfo(status=401)
        with pytest.raises(Unauthorized):
            _validate(issuer, issuer.token(aud=AUDIENCE), validate_userinfo=True)

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Authorization": "Basic abc"},
            {"Authorization": "Bearer invalid"},
        ],
    )
    def test_userinfo_without_token(
        self, issuer: Issuer, headers: dict[str, str]
    ) -> None:
        """User info needs a valid token."""
        response = requests.get(
            f"{issuer.url}{USERINFO_PATH}", headers=headers, timeout=5
        )
        assert response.status_code == 401


def test_context_manager_stops() -> None:
    """The port is closed after use, and FOCA forgets the issuer."""
    with issuer() as idp:
        _validate(idp, idp.token(aud=AUDIENCE))
        url = idp.url
        assert url in auth._oidc_configs
        assert f"{url}{JWKS_PATH}" in auth._jwk_sets
    with pytest.raises(requests.ConnectionError):
        requests.get(f"{url}{JWKS_PATH}", timeout=1)
    assert url not in auth._oidc_configs
    assert f"{url}{JWKS_PATH}" not in auth._jwk_sets
