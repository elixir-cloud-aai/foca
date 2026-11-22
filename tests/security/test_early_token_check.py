"""Tests for validating bearer tokens before request bodies are read."""

import time
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, jsonify
from werkzeug.datastructures import ImmutableMultiDict
from werkzeug.test import Client as WerkzeugClient
from werkzeug.wrappers import Request

from foca.api.register_openapi import register_openapi
from foca.api.routes import Route
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, SpecConfig
from foca.security import auth
from foca.security.access_control import get_current_subject
from foca.security.auth import _requires_bearer_token
from foca.testing import Client, request_context

ISSUER = "https://idp.example.org"
DISCOVERY_URI = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER}/jwks"
THIS_MODULE = __name__
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "key-1"
BODY = {"name": "Rex"}
MULTIPART = (
    b"--b\r\n"
    b'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n'
    b"Content-Type: text/plain\r\n\r\n"
    b"content\r\n"
    b"--b--\r\n"
)


def _token(**claims: Any) -> str:
    """Create signed access token with valid default claims."""
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "alice",
        "aud": "foca",
        "iat": now,
        "exp": now + 300,
        **claims,
    }
    return jwt.encode(
        payload, KEY, algorithm="RS256", headers={"typ": "at+jwt", "kid": KID}
    )


def controller(*args: Any, **kwargs: Any) -> Any:
    """Controller reporting the identified caller."""
    return jsonify(get_current_subject())


post = get = optional = key = custom = both = either = public = controller


def validate_custom(token: str) -> dict[str, Any]:
    """Accept all tokens, as an app's own validation function may."""
    return {"sub": "custom"}


def validate_key(key: str, required_scopes: Any = None) -> dict[str, Any]:
    """Accept all API keys."""
    return {"sub": "key-client"}


def _operation(
    name: str, security: list[dict[str, Any]] | None, body: bool = False
) -> dict[str, Any]:
    """Build an OpenAPI 3 Operation object with security requirements."""
    operation: dict[str, Any] = {
        "operationId": f"{THIS_MODULE}.{name}",
        "responses": {
            "200": {"description": "OK", "content": {"application/json": {}}}
        },
    }
    if body:
        operation["requestBody"] = {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    }
                }
            }
        }
    if security is not None:
        operation["security"] = security
    return operation


SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "Test API", "version": "1.0.0"},
    # global security, applying to operations without their own
    "security": [{"bearerAuth": []}],
    "paths": {
        "/required": {
            "post": _operation("post", None, body=True),
            "get": _operation("get", None),
        },
        "/optional": {
            "post": _operation("optional", [{"bearerAuth": []}, {}], body=True)
        },
        "/key": {"post": _operation("key", [{"goodKey": []}], body=True)},
        "/custom": {"post": _operation("custom", [{"customAuth": []}], body=True)},
        "/both": {
            "post": _operation("both", [{"bearerAuth": [], "goodKey": []}], body=True)
        },
        "/either": {
            "post": _operation(
                "either", [{"bearerAuth": []}, {"bearerAgain": []}], body=True
            )
        },
        "/public": {"post": _operation("public", [], body=True)},
    },
    "components": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "x-bearerInfoFunc": "foca.security.auth.validate_token",
            },
            "bearerAgain": {
                "type": "http",
                "scheme": "Bearer",
                "x-bearerInfoFunc": "foca.security.auth.validate_token",
            },
            "customAuth": {
                "type": "http",
                "scheme": "bearer",
                "x-bearerInfoFunc": f"{THIS_MODULE}.validate_custom",
            },
            "goodKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Key",
                "x-apikeyInfoFunc": f"{THIS_MODULE}.validate_key",
            },
        },
    },
}


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """Mock identity provider; returns mock of `requests.get()`."""
    auth.clear_caches()
    jwk = {
        **jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True),
        "kid": KID,
        "use": "sig",
    }
    responses = {
        DISCOVERY_URI: {"issuer": ISSUER, "jwks_uri": JWKS_URI},
        JWKS_URI: {"keys": [jwk]},
    }

    def _get(url: str, **kwargs: Any) -> MagicMock:
        response = MagicMock(status_code=200)
        response.json.return_value = responses[url]
        return response

    get = MagicMock(side_effect=_get)
    monkeypatch.setattr(auth.requests, "get", get)
    yield get
    auth.clear_caches()


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    """App with the test API."""
    return _app(tmp_path)


def _app(tmp_path: Path) -> Flask:
    """Create app with the test API.

    Args:
        tmp_path: Directory for the specification.

    Returns:
        Flask app.
    """
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(yaml.safe_dump(SPEC))
    config = Config.model_validate(
        {
            "security": {
                "auth": {
                    "issuers": [{"issuer": ISSUER, "namespace": ""}],
                    "audience": ["foca"],
                }
            }
        }
    )
    cnx_app = register_exception_handler(create_connexion_app(config))
    return register_openapi(app=cnx_app, specs=[SpecConfig(path=spec_file)]).app


@pytest.fixture
def body_reads() -> Iterator[MagicMock]:
    """Record reads of request bodies.

    Returns:
        Mock called on each read via `get_data()`, `form` or `files`, with
        the name of what was read; `form` and `files` read as empty.
    """
    reads = MagicMock()
    get_data = Request.get_data

    def _get_data(self: Request, *args: Any, **kwargs: Any) -> Any:
        reads("get_data")
        return get_data(self, *args, **kwargs)

    def _empty(name: str) -> Any:
        def _read(self: Request) -> ImmutableMultiDict[str, Any]:
            reads(name)
            return ImmutableMultiDict()

        return property(_read)

    with (
        mock.patch.object(Request, "get_data", _get_data),
        mock.patch.object(Request, "form", _empty("form")),
        mock.patch.object(Request, "files", _empty("files")),
    ):
        yield reads


def _post(
    app: Flask, path: str, header: str | None, kind: str = "json", **headers: str
) -> Any:
    """Send a request with a body of some kind, and a token, if any."""
    if header is not None:
        headers["Authorization"] = header
    client = Client(app)
    if kind == "json":
        return client.post(path, json=BODY, headers=headers)
    if kind == "form":
        return client.post(path, data=BODY, headers=headers)
    headers["Content-Type"] = "multipart/form-data; boundary=b"
    return client.post(path, content=MULTIPART, headers=headers)


class TestEarlyCheck:
    """Tests of operations requiring bearer tokens validated by FOCA."""

    @pytest.mark.parametrize("kind", ["json", "form", "multipart"])
    @pytest.mark.parametrize(
        "header",
        [
            pytest.param(None, id="none"),
            pytest.param("Basic abc", id="other-scheme"),
            pytest.param("Bearer", id="no-token"),
            pytest.param("Bearer not-a-jwt", id="malformed"),
        ],
    )
    @pytest.mark.parametrize("path", ["/required", "/either"])
    def test_rejected_unread(
        self,
        idp: MagicMock,
        app: Flask,
        body_reads: MagicMock,
        path: str,
        header: str | None,
        kind: str,
    ) -> None:
        """Requests without valid tokens get `401`, their body unread."""
        response = _post(app, path, header, kind)
        assert response.status_code == 401
        assert response.headers["content-type"] == "application/problem+json"
        assert not body_reads.called

    @pytest.mark.parametrize(
        "claims",
        [
            pytest.param({"exp": 1000, "iat": 500}, id="expired"),
            pytest.param({"aud": "other"}, id="wrong-audience"),
            pytest.param({"iss": "https://evil.example.org"}, id="untrusted-issuer"),
        ],
    )
    def test_invalid_rejected_unread(
        self,
        idp: MagicMock,
        app: Flask,
        body_reads: MagicMock,
        claims: dict[str, Any],
    ) -> None:
        """Invalid tokens get a challenge, their body unread."""
        response = _post(app, "/required", f"Bearer {_token(**claims)}")
        assert response.status_code == 401
        assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]
        assert not body_reads.called

    @pytest.mark.parametrize(
        "header",
        [
            pytest.param(None, id="none"),
            pytest.param("Basic abc", id="other-scheme"),
            pytest.param("Bearer", id="no-token"),
            pytest.param("Bearer not-a-jwt", id="malformed"),
        ],
    )
    def test_same_as_connexion(
        self, idp: MagicMock, app: Flask, header: str | None
    ) -> None:
        """Rejections are those of Connexion's security handler."""
        early = _post(app, "/required", header)
        with mock.patch.object(auth, "_requires_bearer_token", return_value=False):
            late = _post(app, "/required", header)
        assert early.status_code == late.status_code == 401
        assert early.json() == late.json()
        assert early.headers.get("WWW-Authenticate") == late.headers.get(
            "WWW-Authenticate"
        )

    def test_no_body(self, idp: MagicMock, app: Flask, body_reads: MagicMock) -> None:
        """Operations without a request body are checked, too."""
        response = Client(app).get("/required")
        assert response.status_code == 401
        assert not body_reads.called

    def test_chunked_unread(
        self, idp: MagicMock, app: Flask, body_reads: MagicMock
    ) -> None:
        """Bodies without a declared size aren't read for the size check."""
        response = WerkzeugClient(app).post(
            "/required",
            input_stream=BytesIO(b'{"name": "Rex"}'),
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )
        assert response.status_code == 401
        assert not body_reads.called

    def test_size_checked_after_token(self, idp: MagicMock, app: Flask) -> None:
        """Too large bodies get `401` without, and `413` with a valid token."""
        content = b"x" * (app.config["MAX_CONTENT_LENGTH"] + 1)
        client = Client(app)
        headers = {"Content-Type": "application/json"}
        assert (
            client.post("/required", content=content, headers=headers).status_code
            == 401
        )
        headers["Authorization"] = f"Bearer {_token()}"
        assert (
            client.post("/required", content=content, headers=headers).status_code
            == 413
        )

    def test_identity_provider_unavailable(
        self, idp: MagicMock, app: Flask, body_reads: MagicMock
    ) -> None:
        """Tokens that can't be validated are rejected without a challenge."""
        idp.side_effect = auth.requests.ConnectionError("down")
        response = _post(app, "/required", f"Bearer {_token()}")
        assert response.status_code == 401
        assert "invalid_token" not in response.headers.get("WWW-Authenticate", "")
        assert not body_reads.called

    @pytest.mark.parametrize("path", ["/required", "/either"])
    @pytest.mark.parametrize("scheme", ["Bearer", "bearer"])
    def test_valid(self, idp: MagicMock, app: Flask, path: str, scheme: str) -> None:
        """Valid tokens are accepted, validated once per request."""
        with mock.patch.object(auth, "_validate", wraps=auth._validate) as validate:
            response = _post(app, path, f"{scheme} {_token()}")
            assert response.status_code == 200
            assert response.json() == "alice"
            assert validate.call_count == 1
            response = _post(app, path, f"{scheme} {_token(sub='bob')}")
            assert response.json() == "bob"
            assert validate.call_count == 2

    def test_valid_body_validated(self, idp: MagicMock, app: Flask) -> None:
        """Bodies of requests with valid tokens are validated as before."""
        response = Client(app).post(
            "/required",
            json={"name": 1},
            headers={"Authorization": f"Bearer {_token()}"},
        )
        assert response.status_code == 400

    def test_function_resolved_when_added(
        self, idp: MagicMock, tmp_path: Path, body_reads: MagicMock
    ) -> None:
        """Tokens are checked with the function Connexion resolved."""
        stub = MagicMock(return_value={"sub": "stub"})
        with mock.patch.object(auth, "validate_token", stub):
            app = _app(tmp_path)
        response = _post(app, "/required", "Bearer opaque")
        assert response.status_code == 200
        assert response.json() == "stub"
        assert stub.call_args.args == ("opaque",)
        stub.side_effect = auth.Unauthorized("no")
        body_reads.reset_mock()
        assert _post(app, "/required", "Bearer opaque").status_code == 401
        assert not body_reads.called

    def test_preflight_unchecked(self, idp: MagicMock, app: Flask) -> None:
        """`OPTIONS` requests that Flask answers aren't checked."""
        assert Client(app).options("/required").status_code == 200


class TestUnchecked:
    """Tests of operations left to Connexion."""

    @pytest.mark.parametrize(
        ("path", "headers", "subject"),
        [
            pytest.param("/optional", {}, None, id="optional"),
            pytest.param("/public", {}, None, id="public"),
            pytest.param("/key", {"X-Key": "k"}, "key-client", id="api-key"),
            pytest.param("/custom", {}, "custom", id="other-function"),
            pytest.param("/both", {"X-Key": "k"}, "key-client", id="with-other-scheme"),
        ],
    )
    def test_read_as_before(
        self,
        idp: MagicMock,
        app: Flask,
        body_reads: MagicMock,
        path: str,
        headers: dict[str, str],
        subject: str | None,
    ) -> None:
        """Bodies are read before Connexion's checks, as before."""
        token = _token() if path == "/both" else "opaque"
        header = None if path in {"/optional", "/public"} else f"Bearer {token}"
        # whether the body was read when the token was validated
        read_first: list[bool] = []
        validate = auth._validate

        def _validate(*args: Any, **kwargs: Any) -> Any:
            read_first.append(body_reads.called)
            return validate(*args, **kwargs)

        with mock.patch.object(auth, "_validate", _validate):
            response = _post(app, path, header, **headers)
        assert body_reads.called
        assert read_first == ([True] if path == "/both" else [])
        assert response.status_code == 200
        if path != "/both":
            assert response.json() == subject

    def test_without_config(self, tmp_path: Path, body_reads: MagicMock) -> None:
        """Apps created without a config are checked, too."""
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(yaml.safe_dump(SPEC))
        cnx_app = register_exception_handler(create_connexion_app())
        app = register_openapi(app=cnx_app, specs=[SpecConfig(path=spec_file)]).app
        assert _post(app, "/required", None).status_code == 401
        assert not body_reads.called

    def test_unmatched(self, idp: MagicMock, app: Flask) -> None:
        """Requests matching no operation aren't checked."""
        assert Client(app).post("/unknown", json=BODY).status_code == 404


def test_other_routes_unchecked(idp: MagicMock, tmp_path: Path) -> None:
    """Routes that aren't operations aren't checked, also if paths match."""
    operation = _operation("get", None)
    operation["parameters"] = [
        {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}}
    ]
    spec = {**SPEC, "paths": {"/{name}": {"get": operation}}}
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(yaml.safe_dump(spec))
    cnx_app = register_exception_handler(create_connexion_app(Config()))
    app = register_openapi(app=cnx_app, specs=[SpecConfig(path=spec_file)]).app
    app.add_url_rule("/health", "health", lambda: "OK")
    client = Client(app)
    assert client.get("/health").status_code == 200
    assert client.get("/ui/").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/pets").status_code == 401


def test_app_route_same_rule_unchecked(idp: MagicMock, tmp_path: Path) -> None:
    """App routes with an operation's rule and method aren't checked."""
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(yaml.safe_dump(SPEC))
    cnx_app = register_exception_handler(create_connexion_app(Config()))
    # added first, so that Flask routes requests to it
    cnx_app.app.add_url_rule("/required", "mine", lambda: "public", methods=["POST"])
    app = register_openapi(app=cnx_app, specs=[SpecConfig(path=spec_file)]).app
    response = Client(app).post("/required", json=BODY)
    assert response.status_code == 200
    assert response.text == "public"
    # the operation's own rule is still checked
    assert Client(app).get("/required").status_code == 401


class TestValidateTokenCache:
    """Tests of reusing validated token info within a request."""

    def test_same_token(self, idp: MagicMock, app: Flask) -> None:
        """The same token is validated once per request."""
        token = _token()
        with (
            request_context(app=app),
            mock.patch.object(auth, "_validate", wraps=auth._validate) as validate,
        ):
            first = auth.validate_token(token)
            assert auth.validate_token(token) is first
        assert validate.call_count == 1

    def test_other_token(self, idp: MagicMock, app: Flask) -> None:
        """Other tokens are validated again."""
        with (
            request_context(app=app),
            mock.patch.object(auth, "_validate", wraps=auth._validate) as validate,
        ):
            auth.validate_token(_token())
            assert auth.validate_token(_token(sub="bob"))["user_id"] == "bob"
        assert validate.call_count == 2

    def test_invalid_not_cached(self, idp: MagicMock, app: Flask) -> None:
        """Rejected tokens are validated again."""
        token = _token(aud="other")
        with (
            request_context(app=app),
            mock.patch.object(auth, "_validate", wraps=auth._validate) as validate,
        ):
            for _ in range(2):
                with pytest.raises(auth.Unauthorized):
                    auth.validate_token(token)
        assert validate.call_count == 2

    def test_no_request(self, idp: MagicMock, app: Flask) -> None:
        """Outside requests, tokens are validated each time."""
        token = _token()
        with (
            app.app_context(),
            mock.patch.object(auth, "_validate", wraps=auth._validate) as validate,
        ):
            for _ in range(2):
                assert auth.validate_token(token)["user_id"] == "alice"
            assert "foca_token_info" not in auth.g
        assert validate.call_count == 2

    def test_other_request(self, idp: MagicMock, app: Flask) -> None:
        """Tokens are validated again in other requests of an app context."""
        token = _token()
        with (
            app.app_context(),
            mock.patch.object(auth, "_validate", wraps=auth._validate) as validate,
        ):
            for _ in range(2):
                with request_context(app=app):
                    auth.validate_token(token)
        assert validate.call_count == 2


def _route(
    security: list[dict[str, Any]] | None,
    functions: dict[str, Any] | None = None,
    **schemes: Any,
) -> Route:
    """Build a route with security requirements and schemes.

    Args:
        security: Security requirements.
        functions: Functions resolved for schemes, by name (`None` for
            unresolved ones); by default, `validate_token()` for all schemes
            naming a function.
        schemes: Security schemes, by name.

    Returns:
        The route.
    """
    functions = functions or {}
    return Route(
        method="GET",
        path="/",
        rule="/",
        operation_id=None,
        security=security,
        extensions={},
        function=controller,
        security_schemes=schemes,
        bearer_info_functions={
            name: function
            for name, scheme in schemes.items()
            if "x-bearerInfoFunc" in scheme
            and (function := functions.get(name, auth.validate_token)) is not None
        },
    )


FOCA = {
    "type": "http",
    "scheme": "bearer",
    "x-bearerInfoFunc": "foca.security.auth.validate_token",
}


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        pytest.param(_route([{"a": []}], a=FOCA), True, id="required"),
        pytest.param(
            _route([{"a": []}, {"b": []}], a=FOCA, b=FOCA), True, id="alternatives"
        ),
        pytest.param(_route([{"a": [], "b": []}], a=FOCA, b=FOCA), True, id="and"),
        pytest.param(
            _route([{"a": []}, {"b": []}], {"b": controller}, a=FOCA, b=FOCA),
            False,
            id="resolved-differently",
        ),
        pytest.param(_route([{"a": []}], {"a": None}, a=FOCA), False, id="unresolved"),
        pytest.param(_route(None), False, id="none"),
        pytest.param(_route([]), False, id="public"),
        pytest.param(_route([{"a": []}, {}], a=FOCA), False, id="optional"),
        pytest.param(_route([{"a": []}]), False, id="unknown-scheme"),
        pytest.param(
            _route([{"a": []}], a={**FOCA, "type": "apiKey"}), False, id="api-key"
        ),
        pytest.param(
            _route([{"a": []}], a={**FOCA, "scheme": "basic"}), False, id="basic"
        ),
        pytest.param(
            _route([{"a": []}], a={**FOCA, "x-bearerInfoFunc": "app.validate"}),
            False,
            id="other-function",
        ),
        pytest.param(
            _route([{"a": []}, {"b": []}], a=FOCA, b={"type": "oauth2"}),
            False,
            id="other-alternative",
        ),
    ],
)
def test_requires_bearer_token(route: Route, expected: bool) -> None:
    """Only operations that Connexion would reject without FOCA's token."""
    assert _requires_bearer_token(route) is expected
