"""Example of testing an app with `foca.testing`, without mocks of validation."""

import sys
from pathlib import Path

import pytest
from pymongo import MongoClient

from foca import Foca
from foca.testing import Client
from foca.testing.issuer import Issuer

EXAMPLE_DIR = Path(__file__).parents[2] / "examples" / "petstore-access-control"
AUDIENCE = "petstore"  # as in the configuration of the example


@pytest.fixture
def client(
    issuer: Issuer,
    mongodb: MongoClient[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Client:
    """Client of the example app, which trusts the local identity provider."""
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(f"security:\n  auth:\n    issuers:\n      - {issuer.url}\n")
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(overlay))
    # Modules of the example are imported by name from its directory
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    for name in ("controllers", "exceptions"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return Client(Foca(config_file=EXAMPLE_DIR / "config.yaml").create_app())


def _headers(token: str) -> dict[str, str]:
    """Get the headers of a request with a token.

    Args:
        token: Token.

    Returns:
        Headers.
    """
    return {"Authorization": f"Bearer {token}"}


def test_tokens_end_to_end(client: Client, issuer: Issuer) -> None:
    """Tokens are validated as in production, audience included."""
    alice = _headers(issuer.token(sub="alice", aud=AUDIENCE))
    response = client.post("/pets", json={"name": "Rex"}, headers=alice)
    assert response.status_code == 200
    assert response.json() is not None
    url = f"/pets/{response.json()['id']}"

    # The subject of the token is what the app uses for access control
    assert client.get(url, headers=alice).status_code == 200
    bob = _headers(issuer.token(sub="bob", aud=AUDIENCE))
    assert client.get(url, headers=bob).status_code == 403

    for rejected in (
        issuer.token(sub="alice", aud="another-app"),
        issuer.token(sub="alice", aud=AUDIENCE, typ="JWT"),
        issuer.token(sub="alice", aud=AUDIENCE, expires_in=-120),
        issuer.token(sub="alice", aud=AUDIENCE, iss="https://other.example.org"),
    ):
        assert client.get(url, headers=_headers(rejected)).status_code == 401
