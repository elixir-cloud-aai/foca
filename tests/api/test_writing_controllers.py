"""Tests of the examples of "Writing controllers" (`docs/writing-controllers.md`).

The controllers are those of the page, served by a FOCA app on Connexion 2,
so that the inputs and responses that the page describes are checked. Keep
them in sync with the page. That the return values give the same responses on
Connexion 3 was checked outside of the test suite, as FOCA doesn't run on it
yet.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from connexion import NoContent
from flask.testing import FlaskClient

from foca.api.register_openapi import register_openapi
from foca.context import current_request
from foca.errors import NotFound
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, SpecConfig
from foca.security.auth import current_claims

# Examples of the page

ITEMS: dict[str, dict[str, Any]] = {}  # in memory, for the examples


def put_item(
    id: str, body: dict[str, Any], dry_run: bool = False
) -> tuple[dict[str, Any], int]:
    """Create or replace an item (example of the page).

    Args:
        id: Identifier of the item.
        body: Item.
        dry_run: Whether to leave the items as they are.

    Returns:
        The item and the status: `201` if it was created, `200` if replaced.
    """
    item = {**body, "id": id}
    status = 200 if id in ITEMS else 201
    if not dry_run:
        ITEMS[id] = item
    return item, status


def get_whoami() -> dict[str, Any]:
    """Describe the caller and the request (example of the page).

    Returns:
        Subject, route and `Accept` header of the request.
    """
    request = current_request()
    return {
        "subject": current_claims().get("sub"),  # None without a token
        "route": request.route,  # e.g., "/whoami"
        "accept": request.headers.get("accept"),
    }


def get_item(id: str) -> dict[str, Any]:  # body; status 200
    """Get an item (example of the page).

    Args:
        id: Identifier of the item.

    Returns:
        The item.

    Raises:
        NotFound: There is no item with the identifier.
    """
    if id not in ITEMS:
        raise NotFound
    return ITEMS[id]


def list_items() -> tuple[list[dict[str, Any]], int, dict[str, str]]:
    """List the items (example of the page).

    Returns:
        The items, the status and their number in a header.
    """
    return list(ITEMS.values()), 200, {"X-Total-Count": str(len(ITEMS))}


def delete_item(id: str) -> tuple[object, int]:
    """Delete an item (example of the page).

    Args:
        id: Identifier of the item.

    Returns:
        No content and the status.

    Raises:
        NotFound: There is no item with the identifier.
    """
    if ITEMS.pop(id, None) is None:
        raise NotFound
    return NoContent, 204


def export_items() -> tuple[str, int, dict[str, str]]:
    """Export the identifiers of the items as CSV (example of the page).

    Returns:
        The CSV, the status and the headers of a download.
    """
    csv = "".join(f"{item_id}\n" for item_id in ITEMS)
    headers = {
        "Content-Type": "text/csv",
        "Content-Disposition": 'attachment; filename="items.csv"',
    }
    return csv, 200, headers


# Test API

ID = {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
OBJECT = {"application/json": {"schema": {"type": "object"}}}


def _operation(name: str, status: str, content: Any = None, **extra: Any) -> Any:
    """Build an OpenAPI 3 Operation object.

    Args:
        name: Name of the controller in this module.
        status: Status of the response.
        content: Media types of the response; none without.
        **extra: Further fields of the operation.

    Returns:
        The Operation object.
    """
    response: dict[str, Any] = {"description": "Response"}
    if content is not None:
        response["content"] = content
    return {
        "operationId": f"{__name__}.{name}",
        "responses": {status: response},
        **extra,
    }


SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Items", "version": "1.0"},
    "paths": {
        "/items": {
            "get": _operation(
                "list_items",
                "200",
                {"application/json": {"schema": {"type": "array"}}},
            ),
        },
        "/items.csv": {
            "get": _operation(
                "export_items", "200", {"text/csv": {"schema": {"type": "string"}}}
            ),
        },
        "/items/{id}": {
            "parameters": [ID],
            "get": _operation("get_item", "200", OBJECT),
            "put": _operation(
                "put_item",
                "200",
                OBJECT,
                parameters=[
                    {
                        "name": "dry_run",
                        "in": "query",
                        "schema": {"type": "boolean", "default": False},
                    }
                ],
                requestBody={"required": True, "content": OBJECT},
            ),
            "delete": _operation("delete_item", "204"),
        },
        "/whoami": {"get": _operation("get_whoami", "200", OBJECT)},
    },
}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[FlaskClient]:
    """Serve the test API with the controllers of the page, without items.

    Args:
        tmp_path: Temporary directory.

    Yields:
        Test client of the app.
    """
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(SPEC))
    cnx_app = register_exception_handler(create_connexion_app(Config()))
    spec = SpecConfig.model_validate({"path": path, "disable_auth": True})
    ITEMS.clear()
    yield register_openapi(cnx_app, specs=[spec]).app.test_client()
    ITEMS.clear()


def test_inputs(client: FlaskClient) -> None:
    """Path and query parameters and the body come as typed arguments."""
    response = client.put("/items/a", json={"name": "A"})
    assert (response.status_code, response.json) == (201, {"name": "A", "id": "a"})
    response = client.put("/items/a?dry_run=true", json={"name": "B"})
    assert (response.status_code, response.json) == (200, {"name": "B", "id": "a"})
    assert ITEMS == {"a": {"name": "A", "id": "a"}}


def test_accessors(client: FlaskClient) -> None:
    """The accessors give the request and the claims of the caller."""
    response = client.get("/whoami", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.json == {
        "subject": None,
        "route": "/whoami",
        "accept": "application/json",
    }


def test_body(client: FlaskClient) -> None:
    """A dictionary is sent as JSON, with status 200."""
    ITEMS["a"] = {"id": "a"}
    response = client.get("/items/a")
    assert response.status_code == 200
    assert response.content_type == "application/json"
    assert response.json == {"id": "a"}
    assert client.get("/items/b").status_code == 404


def test_body_status_headers(client: FlaskClient) -> None:
    """A list is sent as JSON, with the status and headers returned."""
    ITEMS.update({"a": {"id": "a"}, "b": {"id": "b"}})
    response = client.get("/items")
    assert response.status_code == 200
    assert response.content_type == "application/json"
    assert response.headers["X-Total-Count"] == "2"
    assert response.json == [{"id": "a"}, {"id": "b"}]


def test_no_content(client: FlaskClient) -> None:
    """`NoContent` gives a response without a body."""
    ITEMS["a"] = {"id": "a"}
    response = client.delete("/items/a")
    assert response.status_code == 204
    assert response.data == b""
    assert not ITEMS


def test_string(client: FlaskClient) -> None:
    """A string is sent as it is for other media types than JSON."""
    ITEMS.update({"a": {"id": "a"}, "b": {"id": "b"}})
    response = client.get("/items.csv")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert response.headers["Content-Disposition"] == (
        'attachment; filename="items.csv"'
    )
    assert response.data == b"a\nb\n"
