"""Tests of the test client and request contexts (`foca.testing.client`)."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from connexion import App
from flask import Flask, current_app, request
from flask import Response as FlaskResponse
from flask.globals import request_ctx

from foca.api.register_openapi import register_openapi
from foca.context import Headers, _set_token_info, current_request
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, SpecConfig
from foca.security.auth import current_token_info
from foca.testing import Client, Response, request_context
from foca.testing.pytest_plugin import foca_app as default_foca_app

BINARY = bytes(range(256))
SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Test", "version": "1.0"},
    "paths": {
        "/echo": {
            method: {
                "operationId": f"{__name__}.echo_{method}",
                "parameters": [
                    {
                        "name": "tag",
                        "in": "query",
                        "schema": {"type": "array", "items": {"type": "string"}},
                        "explode": True,
                    },
                    {"name": "flag", "in": "query", "schema": {"type": "boolean"}},
                    {"name": "n", "in": "query", "schema": {"type": "integer"}},
                ],
                **(
                    {
                        "requestBody": {
                            "content": {
                                "application/json": {"schema": {}},
                                "application/octet-stream": {"schema": {}},
                                "text/plain": {"schema": {}},
                                "application/x-www-form-urlencoded": {
                                    "schema": {"type": "object"}
                                },
                            }
                        }
                    }
                    if method in ("post", "put", "patch")
                    else {}
                ),
                "responses": {"200": {"description": "Echo"}},
            }
            for method in ("get", "post", "put", "patch", "delete", "options", "head")
        },
        "/binary": {
            "get": {
                "operationId": f"{__name__}.binary",
                "responses": {"200": {"description": "Bytes"}},
            }
        },
        "/text": {
            "get": {
                "operationId": f"{__name__}.text",
                "parameters": [
                    {"name": "media", "in": "query", "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "Text"}},
            }
        },
        "/redirect": {
            "get": {
                "operationId": f"{__name__}.redirect",
                "responses": {"302": {"description": "Elsewhere"}},
            }
        },
    },
}


def _echo_body() -> Any:
    """Describe the body of the current request.

    Returns:
        Body, as JSON, text or form fields, or `None` if there is none.
    """
    if request.mimetype == "application/x-www-form-urlencoded":
        return dict(request.form)
    data = request.get_data()
    if request.mimetype == "application/json":
        return json.loads(data)
    return data.decode("latin-1") if data else None


def echo(**kwargs: Any) -> tuple[dict[str, Any], int, list[tuple[str, str]]]:
    """Echo the request.

    Args:
        **kwargs: Parameters, as Connexion passes them.

    Returns:
        Method, query, headers and body of the request, and the parameters.
    """
    snapshot = current_request()
    headers = [("X-Echo", "yes"), ("X-Multi", "1"), ("X-Multi", "2")]
    return (
        {
            "method": snapshot.method,
            "query": {name: snapshot.query.getlist(name) for name in snapshot.query},
            "headers": {
                name: snapshot.headers.getlist(name) for name in snapshot.headers
            },
            "body": _echo_body(),
            "params": {
                name: kwargs[name] for name in ("tag", "flag", "n") if name in kwargs
            },
        },
        200,
        headers,
    )


# One operation per method, as Flask's endpoints are named after them
echo_get = echo_post = echo_put = echo_patch = echo_delete = echo
echo_options = echo_head = echo


def binary() -> FlaskResponse:
    """Return bytes.

    Returns:
        All bytes, as a binary response.
    """
    return FlaskResponse(BINARY, content_type="application/octet-stream")


def text(media: str) -> FlaskResponse:
    """Return text in an encoding.

    Args:
        media: Content type of the response.

    Returns:
        `é`, encoded as Latin-1, in the given content type.
    """
    return FlaskResponse("é".encode("latin-1"), content_type=media)


def redirect() -> tuple[str, int, dict[str, str]]:
    """Redirect to the echo.

    Returns:
        A redirect.
    """
    return "", 302, {"Location": "/echo?tag=redirected"}


@pytest.fixture
def app(tmp_path: Path) -> App:
    """App with the test API."""
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(SPEC))
    cnx_app = register_exception_handler(create_connexion_app(Config()))
    spec = SpecConfig.model_validate({"path": path, "disable_auth": True})
    return register_openapi(cnx_app, specs=[spec])


@pytest.fixture
def client(app: App) -> Client:
    """Client of the app with the test API."""
    with Client(app) as client:
        return client


class TestClient:
    """Tests of the client against a real app."""

    def test_json(self, client: Client) -> None:
        """JSON bodies are sent with their content type and parsed."""
        response = client.post("/echo", json={"a": [1, "b"]})
        assert response.status_code == 200
        assert response.json()["body"] == {"a": [1, "b"]}
        assert response.json()["headers"]["content-type"] == ["application/json"]

    def test_json_keeps_content_type(self, client: Client) -> None:
        """A content type in the headers is kept for JSON bodies."""
        response = client.put(
            "/echo",
            json={"a": 1},
            headers={"content-type": "application/json; charset=utf-8"},
        )
        assert response.json()["headers"]["content-type"] == [
            "application/json; charset=utf-8"
        ]

    @pytest.mark.parametrize("content", [b"\x00raw\xff", "rawé"])
    def test_content(self, client: Client, content: bytes | str) -> None:
        """Raw bodies are sent as they are, strings as UTF-8."""
        expected = content if isinstance(content, bytes) else content.encode()
        response = client.patch(
            "/echo",
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.json()["body"] == expected.decode("latin-1")

    def test_json_as_httpx(self, client: Client) -> None:
        """JSON bodies are compact UTF-8; NaN and infinity are refused."""
        response = client.post("/echo", json={"name": "\u00e9", "n": [1, 2]})
        assert response.json()["body"] == {"name": "\u00e9", "n": [1, 2]}
        assert response.json()["headers"]["content-length"] == ["23"]
        with pytest.raises(ValueError, match="JSON compliant"):
            client.post("/echo", json={"n": float("nan")})

    def test_form_with_other_type(self, client: Client) -> None:
        """Form fields are sent also with another content type."""
        response = client.post(
            "/echo", data={"a": "b c"}, headers={"Content-Type": "text/plain"}
        )
        assert response.json()["body"] == "a=b+c"

    def test_form(self, client: Client) -> None:
        """Form fields are sent form-encoded."""
        response = client.post("/echo", data={"name": "a b"})
        assert response.json()["body"] == {"name": "a b"}

    def test_one_body_only(self, client: Client) -> None:
        """Only one kind of body is accepted."""
        with pytest.raises(ValueError, match="only one"):
            client.post("/echo", json={}, content=b"")

    def test_bytes(self, client: Client) -> None:
        """Binary bodies of responses are kept."""
        response = client.get("/binary")
        assert response.content == BINARY
        assert response.headers["Content-Type"] == "application/octet-stream"

    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("text/plain; charset=latin-1", "é"),
            ('text/plain; format=x; Charset="ISO-8859-1"', "é"),
            ("text/plain; charset=unknown", "�"),
            ("text/plain; charset=hex", "�"),
            ("text/plain", "�"),
        ],
    )
    def test_text(self, client: Client, content_type: str, expected: str) -> None:
        """Text is decoded with the charset of the response, else as UTF-8."""
        response = client.get("/text", params={"media": content_type})
        assert response.text == expected

    def test_json_whatever_the_type(self) -> None:
        """Bodies of other media types are parsed as JSON if they are."""
        headers = Headers([("Content-Type", "text/plain")])
        assert Response(200, headers, b'{"a": 1}').json() == {"a": 1}

    def test_errors(self, client: Client) -> None:
        """Errors are answered as by the app, with problem details."""
        response = client.get("/missing")
        assert response.status_code == 404
        assert response.json()["status"] == 404
        response = client.get("/echo", params={"n": "x"})
        assert response.status_code == 400
        assert response.headers["content-type"] == "application/problem+json"

    def test_json_of_other_types(self, client: Client) -> None:
        """Bodies that aren't JSON can't be parsed as JSON."""
        with pytest.raises(json.JSONDecodeError):
            client.get("/binary").json()

    def test_headers(self, client: Client) -> None:
        """Headers are sent and read case-insensitively, repeated ones too."""
        for headers in ({"X-One": "a", "X-Two": "b"}, [("X-One", "a"), ("X-Two", "b")]):
            sent = client.delete("/echo", headers=headers).json()["headers"]
            assert (sent["x-one"], sent["x-two"]) == (["a"], ["b"])
        response = client.get("/echo")
        assert response.headers["x-echo"] == response.headers["X-ECHO"] == "yes"
        assert response.headers.getlist("X-Multi") == ["1", "2"]

    def test_cookie_header(self, client: Client) -> None:
        """`Cookie` headers are sent as they are."""
        sent = client.get("/echo", headers={"Cookie": "session=abc"}).json()
        assert sent["headers"]["cookie"] == ["session=abc"]

    @pytest.mark.parametrize(
        ("url", "params", "query"),
        [
            ("/echo", {"tag": ["a", "b"], "n": 2}, {"tag": ["a", "b"], "n": ["2"]}),
            ("/echo", [("tag", "a"), ("tag", "b")], {"tag": ["a", "b"]}),
            ("/echo", {"flag": True}, {"flag": ["true"]}),
            (
                "/echo",
                {"flag": False, "tag": "x y"},
                {"flag": ["false"], "tag": ["x y"]},
            ),
            ("/echo", "tag=a&tag=b", {"tag": ["a", "b"]}),
            ("/echo?tag=a&n=1&tag=c", {"tag": "b"}, {"tag": ["b"], "n": ["1"]}),
            (
                "/echo?n=1",
                [("tag", "a"), ("tag", "b")],
                {"n": ["1"], "tag": ["a", "b"]},
            ),
            ("/echo?n=1", {"tag": None}, {"n": ["1"], "tag": [""]}),
            ("/echo?tag=a", None, {"tag": ["a"]}),
            ("/echo?tag=a", {}, {"tag": ["a"]}),
        ],
    )
    def test_params(
        self, client: Client, url: str, params: Any, query: dict[str, list[str]]
    ) -> None:
        """Query parameters are sent after those in the URL."""
        assert client.get(url, params=params).json()["query"] == query

    def test_params_reach_controllers(self, client: Client) -> None:
        """Query parameters are converted as by Connexion."""
        params: dict[str, list[str] | bool | int] = {"tag": ["a"], "flag": True, "n": 3}
        response = client.get("/echo", params=params)
        assert response.json()["params"] == {"tag": ["a"], "flag": True, "n": 3}

    def test_redirects_not_followed(self, client: Client) -> None:
        """Redirects aren't followed by default."""
        response = client.get("/redirect")
        assert response.status_code == 302
        assert response.headers["location"].endswith("/echo?tag=redirected")

    def test_redirects_followed(self, app: App) -> None:
        """Redirects are followed if set for the client or the request."""
        with Client(app, follow_redirects=True) as client:
            response = client.get("/redirect")
            assert response.status_code == 200
            assert response.json()["query"] == {"tag": ["redirected"]}
            assert client.get("/redirect", follow_redirects=False).status_code == 302
        with Client(app) as client:
            response = client.get("/redirect", follow_redirects=True)
            assert response.status_code == 200

    @pytest.mark.parametrize("method", ["head", "options"])
    def test_methods(self, client: Client, method: str) -> None:
        """Requests have their methods."""
        response = getattr(client, method)("/echo")
        assert response.status_code == 200
        if method == "head":
            assert response.content == b""

    def test_request(self, client: Client) -> None:
        """Requests of any method can be sent."""
        assert client.request("get", "/echo").json()["method"] == "GET"

    def test_flask_app(self, app: App) -> None:
        """Flask apps work too."""
        assert Client(app.app).get("/echo").status_code == 200

    def test_repr(self) -> None:
        """Responses are represented by their status code."""
        assert repr(Response(204, Headers([]), b"")) == "<Response [204]>"


class TestRequestContext:
    """Tests of `request_context`."""

    def test_defaults(self) -> None:
        """Without arguments, a `GET` request of `/` on an empty app."""
        with request_context():
            snapshot = current_request()
            assert current_token_info() == {}
        assert snapshot.method == "GET"
        assert snapshot.path == "/"
        assert snapshot.url == "http://localhost/"
        assert snapshot.client_address == "127.0.0.1"

    def test_request(self, app: App) -> None:
        """The request is as given, on the app."""
        with request_context(
            "/echo?tag=a",
            "post",
            headers={"X-Test": "one"},
            token_info={"sub": "alice"},
            app=app,
            params={"tag": "b"},
            root_path="/root",
            client_address="203.0.113.5",
        ):
            snapshot = current_request()
            assert current_token_info() == {"sub": "alice"}
        assert snapshot.method == "POST"
        assert snapshot.path == "/echo"
        assert snapshot.root_path == "/root"
        assert snapshot.query.getlist("tag") == ["b"]
        assert snapshot.headers["x-test"] == "one"
        assert snapshot.route == "/echo"
        assert snapshot.client_address == "203.0.113.5"

    def test_flask_app(self) -> None:
        """Flask apps work too, with their configuration."""
        app = Flask(__name__)
        app.config["marker"] = "yes"
        with request_context(app=app):
            assert current_app.config["marker"] == "yes"

    @pytest.mark.parametrize(("root_path", "expected"), [("/tes/", "/tes"), ("", "")])
    def test_root_path(self, root_path: str, expected: str) -> None:
        """Root paths lose trailing slashes."""
        with request_context(root_path=root_path):
            assert current_request().root_path == expected

    def test_root_path_without_slash(self) -> None:
        """Root paths start with a slash."""
        with (
            pytest.raises(ValueError, match="doesn't start"),
            request_context(root_path="tes"),
        ):
            pass  # pragma: no cover

    def test_token_info_keeps_context(self) -> None:
        """Other entries of Connexion's context are kept."""
        with request_context():
            request_ctx.connexion_context = {"user": "x"}  # type: ignore[attr-defined]
            _set_token_info({"sub": "alice"})
            assert request_ctx.connexion_context == {  # type: ignore[attr-defined]
                "user": "x",
                "token_info": {"sub": "alice"},
            }

    def test_token_info_needs_request(self) -> None:
        """Token info can't be set without a request."""
        with pytest.raises(RuntimeError, match="only in a request"):
            _set_token_info({"sub": "alice"})


class TestFixtures:
    """Tests of the fixtures of the pytest plugin."""

    def test_foca_app_undefined(self) -> None:
        """Without a fixture of the app, the plugin's tells how to add it."""
        with pytest.raises(pytest.UsageError, match="Define the fixture 'foca_app'"):
            default_foca_app.__wrapped__()  # type: ignore[attr-defined]

    @pytest.fixture
    def foca_app(self, app: App) -> App:
        """App under test, as apps define it."""
        return app

    def test_foca_client(self, foca_client: Client) -> None:
        """The client fixture gives a client of the app."""
        assert foca_client.get("/echo").status_code == 200
