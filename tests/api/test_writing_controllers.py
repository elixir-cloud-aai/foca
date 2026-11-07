"""Tests of "Writing controllers" (`docs/writing-controllers.md`).

The controllers and the operation of the page's examples are taken from the
page itself, served by a FOCA app on Connexion 2, so that the inputs and
responses that the page describes are checked, and edits of the examples are
tested. Further return values check the Connexion 2 side of the comparison
on the page. That Connexion 3 gives the same responses (with the differences
listed on the page) was checked with a script outside of the test suite, as
FOCA doesn't run on Connexion 3 yet (see the pull request of issue #719).
"""

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from connexion import NoContent
from flask import Response

from foca.api.register_openapi import register_openapi
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, SpecConfig
from foca.security import auth
from foca.testing import Client

PAGE = (Path(__file__).resolve().parents[2] / "docs/writing-controllers.md").read_text(
    encoding="utf-8"
)
# Module that the page's operation names
PAGE_MODULE = "my_app.controllers"
# Module of the page's controllers in the tests
CONTROLLERS = f"{__name__}_page"


def _blocks(language: str, title: str) -> list[str]:
    """Get the code blocks of the page with a title.

    Args:
        language: Language of the blocks, e.g., `python`.
        title: Title of the blocks, e.g., the file name.

    Returns:
        The code of the blocks, in the order of the page.
    """
    pattern = rf'^```{language} title="{re.escape(title)}"\n(.*?)^```$'
    return re.findall(pattern, PAGE, flags=re.DOTALL | re.MULTILINE)


def _page_controllers() -> ModuleType:
    """Run the code blocks of the page's controllers as one module.

    Returns:
        The module of the controllers.
    """
    module = ModuleType(CONTROLLERS)
    code = "\n".join(_blocks("python", f"{PAGE_MODULE.replace('.', '/')}.py"))
    exec(code, vars(module))  # noqa: S102 (the code of our own docs)
    return module


def _page_paths() -> dict[str, Any]:
    """Get the paths of the page's specification, for the page's controllers.

    Returns:
        The Paths object, with the operations of the test module.
    """
    (block,) = _blocks("yaml", "openapi.yaml")
    paths: dict[str, Any] = yaml.safe_load(
        block.replace(f"{PAGE_MODULE}.", f"{CONTROLLERS}.")
    )["paths"]
    return paths


def _operation(
    module: str, name: str, status: str, content: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build an OpenAPI 3 Operation object.

    Args:
        module: Module of the controller.
        name: Name of the controller.
        status: Status of the response.
        content: Media types of the response; none without.

    Returns:
        The Operation object.
    """
    response: dict[str, Any] = {"description": "Response"}
    if content is not None:
        response["content"] = content
    return {"operationId": f"{module}.{name}", "responses": {status: response}}


def _content(*media_types: str) -> dict[str, Any]:
    """Build the Content object of a response.

    Args:
        *media_types: Media types of the response.

    Returns:
        The Content object, with any schema for each media type.
    """
    return {media_type: {"schema": {}} for media_type in media_types}


# Further return values (Connexion 2 side of the comparison)


def body_headers() -> tuple[dict[str, str], dict[str, str]]:
    """Return a body and headers.

    Returns:
        Body and headers.
    """
    return {"id": "a"}, {"X-Custom": "yes"}


def json_string() -> str:
    """Return a string, e.g., for a JSON media type.

    Returns:
        A string.
    """
    return "hello"


# Operations need distinct controllers
text_string = undeclared_string = json_string


def no_content_headers() -> tuple[object, int, dict[str, str]]:
    """Return no content with headers.

    Returns:
        No content, status and headers.
    """
    return NoContent, 204, {"X-Custom": "yes"}


def download() -> tuple[bytes, int, dict[str, str]]:
    """Return bytes as a download.

    Returns:
        Bytes, status and headers.
    """
    return b"\x00data", 200, {"Content-Type": "application/octet-stream"}


def json_bytes() -> bytes:
    """Return bytes for a JSON media type.

    Returns:
        Bytes.
    """
    return b'{"id": "a"}'


def charset_header() -> tuple[str, int, dict[str, str]]:
    """Return a `Content-Type` that isn't a declared media type.

    Returns:
        A string, status and headers.
    """
    return "a,b\n", 200, {"Content-Type": "text/csv; charset=utf-8"}


def two_media_types() -> dict[str, str]:
    """Return a body for a response with two media types.

    Returns:
        A body.
    """
    return {"id": "a"}


def none_204() -> tuple[None, int]:
    """Return `None` with status 204.

    Returns:
        `None` and the status.
    """
    return None, 204


def flask_response() -> Response:
    """Return a Flask response.

    Returns:
        A Flask response.
    """
    return Response('{"id": "a"}', status=201, mimetype="application/json")


JSON = _content("application/json")
FORMS: dict[str, dict[str, Any]] = {
    "/body-headers": _operation(__name__, "body_headers", "200", JSON),
    "/json-string": _operation(__name__, "json_string", "200", JSON),
    "/no-content-string": _operation(__name__, "undeclared_string", "200"),
    "/text-string": _operation(__name__, "text_string", "200", _content("text/plain")),
    "/no-content-headers": _operation(__name__, "no_content_headers", "204"),
    "/download": _operation(
        __name__, "download", "200", _content("application/octet-stream")
    ),
    "/json-bytes": _operation(__name__, "json_bytes", "200", JSON),
    "/charset-header": _operation(
        __name__, "charset_header", "200", _content("text/csv")
    ),
    "/two-media-types": _operation(
        __name__, "two_media_types", "200", _content("application/json", "text/plain")
    ),
    "/none-204": _operation(__name__, "none_204", "204"),
    "/flask-response": _operation(__name__, "flask_response", "201", JSON),
}


@pytest.fixture
def page() -> Iterator[ModuleType]:
    """Provide the page's controllers, importable, without items.

    Yields:
        The module of the controllers.
    """
    module = _page_controllers()
    sys.modules[CONTROLLERS] = module
    try:
        yield module
    finally:
        del sys.modules[CONTROLLERS]


@pytest.fixture
def client(tmp_path: Path, page: ModuleType) -> Client:
    """Serve the page's operation and controllers and the further ones.

    Args:
        tmp_path: Temporary directory.
        page: The page's controllers.

    Returns:
        Test client of the app.
    """
    paths = {
        **_page_paths(),
        "/items": {"get": _operation(CONTROLLERS, "list_items", "200", JSON)},
        "/items.csv": {
            "get": _operation(CONTROLLERS, "export_items", "200", _content("text/csv"))
        },
        "/whoami": {"get": _operation(CONTROLLERS, "get_whoami", "200", JSON)},
        **{path: {"get": operation} for path, operation in FORMS.items()},
    }
    # The page's operation, and reads and deletions of the same items
    item = paths["/items/{id}"]
    item["get"] = _operation(CONTROLLERS, "get_item", "200", JSON)
    item["delete"] = _operation(CONTROLLERS, "delete_item", "204")
    id_parameter = item["put"]["parameters"][:1]
    item["get"]["parameters"] = item["delete"]["parameters"] = id_parameter
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Items", "version": "1.0"},
        "paths": paths,
    }
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(spec))
    cnx_app = register_exception_handler(create_connexion_app(Config()))
    config = SpecConfig.model_validate({"path": path, "disable_auth": True})
    return Client(register_openapi(cnx_app, specs=[config]))


def test_page_examples(page: ModuleType) -> None:
    """The page's controllers and operation are found."""
    assert page.ITEMS == {}
    assert list(_page_paths()) == ["/items/{id}"]


def test_inputs(client: Client, page: ModuleType) -> None:
    """Path and query parameters and the body come as typed arguments."""
    response = client.put("/items/a", json={"name": "A"})
    assert (response.status_code, response.json()) == (201, {"name": "A", "id": "a"})
    response = client.put("/items/a?dry_run=true", json={"name": "B"})
    assert (response.status_code, response.json()) == (200, {"name": "B", "id": "a"})
    assert page.ITEMS == {"a": {"name": "A", "id": "a"}}


def test_accessors(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The accessors give the request and the claims of the caller."""
    response = client.get("/whoami", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.json() == {
        "subject": None,
        "route": "/whoami",
        "accept": "application/json",
    }
    # Token info as FOCA's `validate_token()` returns it
    monkeypatch.setattr(auth, "_get_token_info", lambda: {"claims": {"sub": "alice"}})
    response = client.get("/whoami")
    assert response.json() is not None
    assert response.json()["subject"] == "alice"


def test_body(client: Client, page: ModuleType) -> None:
    """A dictionary is sent as JSON, with status 200."""
    page.ITEMS["a"] = {"id": "a"}
    response = client.get("/items/a")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"id": "a"}
    assert client.get("/items/b").status_code == 404


def test_body_status_headers(client: Client, page: ModuleType) -> None:
    """A list is sent as JSON, with the status and headers returned."""
    page.ITEMS.update({"a": {"id": "a"}, "b": {"id": "b"}})
    response = client.get("/items")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["X-Total-Count"] == "2"
    assert response.json() == [{"id": "a"}, {"id": "b"}]


def test_no_content(client: Client, page: ModuleType) -> None:
    """`NoContent` gives a response without a body."""
    page.ITEMS["a"] = {"id": "a"}
    response = client.delete("/items/a")
    assert response.status_code == 204
    assert response.content == b""
    assert not page.ITEMS
    # Not `None`, which Connexion 3 sends with another `Content-Type`
    page.ITEMS["a"] = {"id": "a"}
    assert page.delete_item("a") == (NoContent, 204)


def test_string(client: Client, page: ModuleType) -> None:
    """A string is sent as it is for other media types than JSON."""
    page.ITEMS.update({"a": {"id": "a"}, "b": {"id": "b"}})
    response = client.get("/items.csv")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["Content-Disposition"] == (
        'attachment; filename="items.csv"'
    )
    assert response.content == b"a\nb\n"
    # A declared media type exactly, as Connexion 3 requires
    assert page.export_items()[2]["Content-Type"] == "text/csv"


JSON_BODY = b'{\n  "id": "a"\n}\n'  # indented by 2 spaces


@pytest.mark.parametrize(
    ("path", "status", "content_type", "custom", "data"),
    [
        ("/body-headers", 200, "application/json", "yes", JSON_BODY),
        ("/json-string", 200, "application/json", None, b'"hello"\n'),
        ("/no-content-string", 200, "application/json", None, b'"hello"\n'),
        ("/text-string", 200, "text/plain; charset=utf-8", None, b"hello"),
        ("/no-content-headers", 204, "application/json", "yes", b""),
        ("/download", 200, "application/octet-stream", None, b"\x00data"),
        ("/charset-header", 200, "text/csv; charset=utf-8", None, b"a,b\n"),
        ("/two-media-types", 200, "application/json", None, JSON_BODY),
        ("/none-204", 204, "application/json", None, b""),
        ("/flask-response", 201, "application/json", None, b'{"id": "a"}'),
    ],
)
def test_return_values(
    client: Client,
    path: str,
    status: int,
    content_type: str,
    custom: str | None,
    data: bytes,
) -> None:
    """Return values give the responses on Connexion 2 that the page lists."""
    response = client.get(path)
    assert response.status_code == status
    assert response.headers.get("Content-Type") == content_type
    assert response.headers.get("X-Custom") == custom
    assert response.content == data


def test_json_bytes(client: Client, caplog: pytest.LogCaptureFixture) -> None:
    """Bytes for a JSON media type fail, as they can't be serialized."""
    assert client.get("/json-bytes").status_code == 500
    assert "Object of type bytes is not JSON serializable" in caplog.text
