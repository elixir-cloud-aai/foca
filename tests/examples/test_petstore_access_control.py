"""Smoke test of the access control petstore example app."""

import sys
from pathlib import Path
from typing import Any

import pytest
from connexion.exceptions import OAuthProblem
from flask.testing import FlaskClient

from foca import Foca
from foca.security import auth
from tests.mock_data import MongoClients

EXAMPLE_DIR = Path(__file__).parents[2] / "examples" / "petstore-access-control"
ISSUER = "https://login.example.org/oidc/"
# Service client of the example, allowed to act for users
SERVICE_CLIENT = "pet-shop"


def mock_validate_token(token: str) -> dict[str, Any]:
    """Accept bearer tokens of users `alice` and `bob` and of `pet-shop`.

    Tokens of users are their identifiers; `pet-shop` gets a service token.
    """
    if token == SERVICE_CLIENT:
        claims = {"iss": ISSUER, "sub": token, "client_id": token}
        return {"claims": claims, "user_id": token}
    if token not in {"alice", "bob"}:
        raise OAuthProblem(description="Invalid token")
    return {"claims": {"iss": ISSUER, "sub": token}, "user_id": token}


def _headers(user: str | None, on_behalf_of: str | None = None) -> dict[str, str]:
    headers = {} if user is None else {"Authorization": f"Bearer {user}"}
    if on_behalf_of is not None:
        headers["X-On-Behalf-Of"] = on_behalf_of
    return headers


def _add_pet(client: FlaskClient, user: str, **headers: str) -> str:
    """Add a pet as a user, returning its URL."""
    response = client.post(
        "/pets", json={"name": "Rex"}, headers={**_headers(user), **headers}
    )
    assert response.status_code == 200
    assert response.json is not None
    return f"/pets/{response.json['id']}"


def _pet_ids(client: FlaskClient, user: str) -> list[int]:
    """List the identifiers of the pets a user sees."""
    response = client.get("/pets", headers=_headers(user))
    assert response.status_code == 200
    assert response.json is not None
    return [pet["id"] for pet in response.json]


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    mongo_clients: MongoClients,
) -> FlaskClient:
    """Client of the example app.

    With in-memory MongoDB and stubbed token validation.
    """
    # The example's modules `controllers` and `exceptions` are imported by
    # name from the example directory
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    for name in ["controllers", "exceptions"]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    monkeypatch.setattr(auth, "validate_token", mock_validate_token)
    app = Foca(config_file=EXAMPLE_DIR / "config.yaml").create_app()
    return app.app.test_client()


def test_owners_manage_their_pets(client: FlaskClient) -> None:
    """Owners can get and delete their pets; others get 403."""
    response = client.post("/pets", json={"name": "Rex"}, headers=_headers("alice"))
    assert response.status_code == 200
    assert response.json is not None
    pet_id = response.json["id"]
    url = f"/pets/{pet_id}"

    # users only list their own pets, and can't access others' pets
    assert _pet_ids(client, "alice") == [pet_id]
    assert _pet_ids(client, "bob") == []
    assert client.get(url, headers=_headers("bob")).status_code == 403
    assert client.delete(url, headers=_headers("bob")).status_code == 403

    # the owner may
    response = client.get(url, headers=_headers("alice"))
    assert response.status_code == 200
    assert response.json is not None
    assert response.json["name"] == "Rex"
    assert client.delete(url, headers=_headers("alice")).status_code == 204

    # ownership is removed with the pet
    assert client.get(url, headers=_headers("alice")).status_code == 403


def test_unauthenticated(client: FlaskClient) -> None:
    """Requests without valid tokens are rejected with problem details."""
    for headers in [_headers(None), _headers("mallory")]:
        response = client.get("/pets", headers=headers)
        assert response.status_code == 401
        assert response.mimetype == "application/problem+json"
        assert response.json is not None
        assert response.json["status"] == 401


def test_owners_manage_roles(client: FlaskClient) -> None:
    """Owners grant and revoke roles on their pets; others get 403."""
    url = _add_pet(client, "alice")
    bob = f"{url}/permissions/users/bob"
    alice = f"{url}/permissions/users/alice"

    # only owners manage roles
    assert client.get(f"{url}/permissions", headers=_headers("bob")).status_code == 403
    response = client.put(bob, json={"role": "owner"}, headers=_headers("bob"))
    assert response.status_code == 403

    # maintainers may get, but not delete pets
    response = client.put(bob, json={"role": "maintainer"}, headers=_headers("alice"))
    assert response.status_code == 200
    assert response.json == {
        "subject_type": "user",
        "subject": "bob",
        "role": "maintainer",
    }
    assert _pet_ids(client, "bob") == [int(url.rsplit("/", 1)[1])]
    assert client.get(url, headers=_headers("bob")).status_code == 200
    assert client.delete(url, headers=_headers("bob")).status_code == 403
    response = client.get(f"{url}/permissions", headers=_headers("alice"))
    assert response.status_code == 200
    assert response.json == [
        {"subject_type": "user", "subject": "alice", "role": "owner"},
        {"subject_type": "user", "subject": "bob", "role": "maintainer"},
    ]

    # the last owner can't be removed
    assert client.delete(alice, headers=_headers("alice")).status_code == 409
    assert client.delete(bob, headers=_headers("alice")).status_code == 200
    assert client.get(url, headers=_headers("bob")).status_code == 403

    # owners of deleted pets lose their roles
    assert client.delete(url, headers=_headers("alice")).status_code == 204
    response = client.put(bob, json={"role": "owner"}, headers=_headers("alice"))
    assert response.status_code == 403


def test_roles_on_deleted_pets(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roles granted while a pet is deleted are revoked again."""
    url = _add_pet(client, "alice")
    # the pet is deleted while the role is granted
    monkeypatch.setattr(
        sys.modules["controllers"], "_pet_exists", lambda id: lambda: False
    )
    bob = f"{url}/permissions/users/bob"
    response = client.put(bob, json={"role": "owner"}, headers=_headers("alice"))
    assert response.status_code == 404
    response = client.get(f"{url}/permissions", headers=_headers("alice"))
    assert response.json == [
        {"subject_type": "user", "subject": "alice", "role": "owner"}
    ]


def test_service_client_acts_for_user(client: FlaskClient) -> None:
    """Pets added by a service client for a user are owned by the user."""
    url = _add_pet(client, "pet-shop", **{"X-On-Behalf-Of": "bob"})
    assert client.get(url, headers=_headers("bob")).status_code == 200
    assert client.get(url, headers=_headers("pet-shop")).status_code == 403
    # users can't act for others
    response = client.post(
        "/pets", json={"name": "Rex"}, headers=_headers("alice", on_behalf_of="bob")
    )
    assert response.status_code == 403
