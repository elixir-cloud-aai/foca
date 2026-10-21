"""Smoke test of the access control petstore example app."""

import sys
from pathlib import Path

import pytest
from connexion.exceptions import OAuthProblem

from foca import Foca
from foca.security import auth

EXAMPLE_DIR = Path(__file__).parents[2] / "examples" / "petstore-access-control"
ISSUER = "https://login.example.org/oidc/"


def mock_validate_token(token: str) -> dict:
    """Accept bearer tokens `alice` and `bob` as their user identifiers."""
    if token not in {"alice", "bob"}:
        raise OAuthProblem(description="Invalid token")
    return {"claims": {"iss": ISSUER, "sub": token}, "user_id": token}


def _headers(user: str | None) -> dict:
    return {} if user is None else {"Authorization": f"Bearer {user}"}


@pytest.fixture
def client(monkeypatch, mongo_clients):
    """Client of the example app, with in-memory MongoDB and stubbed token
    validation."""
    # The example's modules `controllers` and `exceptions` are imported by
    # name from the example directory
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    for name in ["controllers", "exceptions"]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    monkeypatch.setattr(auth, "validate_token", mock_validate_token)
    app = Foca(config_file=EXAMPLE_DIR / "config.yaml").create_app()
    return app.app.test_client()


def test_owners_manage_their_pets(client):
    """Owners can get and delete their pets; others get 403."""
    response = client.post("/pets", json={"name": "Rex"}, headers=_headers("alice"))
    assert response.status_code == 200
    pet_id = response.json["id"]
    url = f"/pets/{pet_id}"

    # any authenticated user may list pets, but not access others' pets
    assert client.get("/pets", headers=_headers("bob")).status_code == 200
    assert client.get(url, headers=_headers("bob")).status_code == 403
    assert client.delete(url, headers=_headers("bob")).status_code == 403

    # the owner may
    response = client.get(url, headers=_headers("alice"))
    assert response.status_code == 200
    assert response.json["name"] == "Rex"
    assert client.delete(url, headers=_headers("alice")).status_code == 204

    # ownership is removed with the pet
    assert client.get(url, headers=_headers("alice")).status_code == 403


def test_unauthenticated(client):
    """Requests without valid tokens are rejected with problem details."""
    for headers in [_headers(None), _headers("mallory")]:
        response = client.get("/pets", headers=headers)
        assert response.status_code == 401
        assert response.mimetype == "application/problem+json"
        assert response.json["status"] == 401
