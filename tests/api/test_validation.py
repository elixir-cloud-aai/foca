"""Tests for the stricter checks of requests (`foca.api.validation`)."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from connexion.decorators.validation import RequestBodyValidator
from connexion.resolver import Resolution
from flask import Flask
from werkzeug.test import TestResponse

from foca.api.register_openapi import register_openapi
from foca.api.validation import StrictParametersResolver, _has_unpaired_surrogates
from foca.errors.exceptions import register_exception_handler
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, SpecConfig
from foca.utils.service_info import is_date

DATE_TIME = {"type": "string", "format": "date-time"}
SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Test", "version": "1.0"},
    "paths": {
        "/items": {
            "post": {
                "operationId": f"{__name__}.createItem",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "created": DATE_TIME,
                                    "day": {"type": "string", "format": "date"},
                                    "link": {"type": "string", "format": "uri"},
                                    "nested": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {"created": DATE_TIME},
                                        },
                                    },
                                },
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "Created"}},
            },
            "get": {
                "operationId": f"{__name__}.listItems",
                "responses": {"200": {"description": "Items"}},
            },
        },
        "/filtered": {
            "get": {
                "operationId": f"{__name__}.listFiltered",
                "parameters": [
                    {
                        "name": "since",
                        "in": "query",
                        "schema": {"type": "string", "format": "date-time"},
                    }
                ],
                "responses": {"200": {"description": "Items"}},
            },
        },
        "/items/{itemId}": {
            "parameters": [
                {
                    "name": "itemId",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ],
            "get": {
                "operationId": f"{__name__}.getItem",
                "responses": {"200": {"description": "Item"}},
            },
        },
    },
}


def createItem(body: dict[str, Any]) -> dict[str, Any]:
    """Controller returning the body.

    Args:
        body: Body of the request.

    Returns:
        The body.
    """
    return body


def listItems() -> list[str]:
    """Controller without parameters.

    Returns:
        Empty list.
    """
    return []


def listFiltered(since: str | None = None) -> list[str]:
    """Controller with a query parameter.

    Args:
        since: Date and time.

    Returns:
        Empty list.
    """
    return []


def getItem(itemId: str) -> dict[str, str]:
    """Controller with a path parameter.

    Args:
        itemId: Identifier of the item.

    Returns:
        The identifier.
    """
    return {"id": itemId}


def getLog() -> dict[str, str]:
    """Controller returning an invalid value for its specification.

    Returns:
        Log.
    """
    return {"created": "2026-11-04T07:00:00"}


def getSecret() -> str:
    """Controller of an operation with an API key.

    Returns:
        Text.
    """
    return "secret"


def checkKey(api_key: str, required_scopes: Any = None) -> dict[str, str] | None:
    """Check an API key.

    Args:
        api_key: API key.
        required_scopes: Required scopes.

    Returns:
        Token information, if the key is valid.
    """
    return {"sub": "a"} if api_key == "good" else None


class CustomBodyValidator(RequestBodyValidator):
    """Body validator of an app, refusing all bodies."""

    def validate_schema(self, data: Any, url: str) -> Any:
        """Refuse a body.

        Args:
            data: Body.
            url: URL of the request.

        Raises:
            ValueError: Always.
        """
        raise ValueError("Refused")


def _create_app(tmp_path: Path, strict: bool = True, **connexion: Any) -> Flask:
    """Create an app with the test API.

    Args:
        tmp_path: Temporary directory.
        strict: Whether to use `strict_validation`.
        **connexion: Further Connexion options.

    Returns:
        Flask app.
    """
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(SPEC))
    cnx_app = register_exception_handler(create_connexion_app(Config()))
    spec = SpecConfig.model_validate(
        {
            "path": path,
            "disable_auth": True,
            "connexion": {"strict_validation": strict, **connexion},
        }
    )
    return register_openapi(cnx_app, specs=[spec]).app


def _post(app: Flask, json_text: str) -> TestResponse:
    """Post a body.

    Args:
        app: Flask app.
        json_text: JSON document, as text.

    Returns:
        Response.
    """
    return app.test_client().post(
        "/items", data=json_text, content_type="application/json"
    )


class TestSurrogates:
    """Tests of the refusal of unpaired surrogates."""

    @pytest.mark.parametrize(
        "text",
        [
            r'{"name": "\ud800"}',
            r'{"name": "\udc00"}',
            r'{"\ud800": "key"}',
            r'{"nested": [{"name": "a\ud800b"}]}',
            r'{"nested": [{"\ud800": 1}]}',
        ],
    )
    def test_refused(self, tmp_path: Path, text: str) -> None:
        """Unpaired surrogates are refused, wherever they are."""
        response = _post(_create_app(tmp_path), text)
        assert response.status_code == 400
        assert "surrogates" in response.text

    @pytest.mark.parametrize(
        "text", [r'{"name": "😀"}', '{"name": "ä"}', '{"count": 1}', "{}"]
    )
    def test_accepted(self, tmp_path: Path, text: str) -> None:
        """Paired surrogates and others are accepted."""
        assert _post(_create_app(tmp_path), text).status_code == 200

    def test_deep_nesting(self) -> None:
        """Depth doesn't matter."""
        data: list[Any] = ["\ud800"]
        for _ in range(100000):
            data = [data]
        assert _has_unpaired_surrogates(data) is True

    @pytest.mark.parametrize("data", [None, 1, 1.5, True, b"\xff"])
    def test_other_types(self, data: object) -> None:
        """Other types have no surrogates."""
        assert _has_unpaired_surrogates(data) is False


class TestFormats:
    """Tests of the checks of formats."""

    @pytest.mark.parametrize(
        ("field", "value", "status"),
        [
            ("created", "2026-01-31T12:00:00Z", 200),
            ("created", "2026-01-31T12:00:00.5+01:00", 200),
            ("created", "2026-02-31T12:00:00Z", 400),
            ("created", "yesterday", 400),
            ("day", "2026-01-31", 200),
            ("day", "2026-02-31", 400),
            ("day", "2026-1-3", 400),
            ("day", "2026-01-31T12:00:00Z", 400),
            ("link", "https://example.org/a", 200),
            ("link", "urn:example:a", 200),
            ("link", "not a uri", 400),
        ],
    )
    def test_body(self, tmp_path: Path, field: str, value: str, status: int) -> None:
        """Formats of properties are checked."""
        response = (
            _create_app(tmp_path).test_client().post("/items", json={field: value})
        )
        assert response.status_code == status

    def test_nested(self, tmp_path: Path) -> None:
        """Formats of nested properties are checked."""
        client = _create_app(tmp_path).test_client()
        body = {"nested": [{"created": "yesterday"}]}
        assert client.post("/items", json=body).status_code == 400

    def test_parameter(self, tmp_path: Path) -> None:
        """Formats of parameters are checked."""
        client = _create_app(tmp_path).test_client()
        assert client.get("/filtered?since=yesterday").status_code == 400
        assert client.get("/filtered?since=2026-01-31T12:00:00Z").status_code == 200


class TestExtraParameters:
    """Tests of the refusal of query parameters of operations without any."""

    def test_refused(self, tmp_path: Path) -> None:
        """Extra query parameters are refused."""
        response = _create_app(tmp_path).test_client().get("/items?other=1")
        assert response.status_code == 400

    def test_without_query(self, tmp_path: Path) -> None:
        """Requests without them pass."""
        assert _create_app(tmp_path).test_client().get("/items").status_code == 200

    def test_operations_with_parameters(self, tmp_path: Path) -> None:
        """Connexion's check goes on for operations with parameters."""
        client = _create_app(tmp_path).test_client()
        assert client.get("/filtered?other=1").status_code == 400
        assert client.get("/items/1").status_code == 200
        assert client.get("/items/1?other=1").status_code == 400

    def test_body_only(self, tmp_path: Path) -> None:
        """Operations with only a body count as having no parameters."""
        client = _create_app(tmp_path).test_client()
        assert client.post("/items?other=1", json={}).status_code == 400

    def test_not_strict(self, tmp_path: Path) -> None:
        """Without `strict_validation`, nothing changes."""
        client = _create_app(tmp_path, strict=False).test_client()
        assert client.get("/items?other=1").status_code == 200

    def test_api_key(self, tmp_path: Path) -> None:
        """API keys in the query aren't extra parameters, as for Connexion."""
        path = tmp_path / "keys.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "openapi": "3.0.3",
                    "info": {"title": "Keys", "version": "1.0"},
                    "security": [{"key": []}],
                    "components": {
                        "securitySchemes": {
                            "key": {
                                "type": "apiKey",
                                "in": "query",
                                "name": "api_key",
                                "x-apikeyInfoFunc": f"{__name__}.checkKey",
                            }
                        }
                    },
                    "paths": {
                        "/secret": {
                            "get": {
                                "operationId": f"{__name__}.getSecret",
                                "responses": {"200": {"description": "Secret"}},
                            }
                        }
                    },
                }
            )
        )
        cnx_app = register_exception_handler(create_connexion_app(Config()))
        spec = SpecConfig.model_validate(
            {"path": path, "connexion": {"strict_validation": True}}
        )
        client = register_openapi(cnx_app, specs=[spec]).app.test_client()
        assert client.get("/secret?api_key=good").status_code == 200
        assert client.get("/secret?api_key=bad").status_code == 401
        assert client.get("/secret?api_key=good&other=1").status_code == 400

    def test_custom_resolver(self) -> None:
        """The resolver of an app is used."""
        calls: list[str] = []

        def _resolve(operation_id: str) -> Any:
            calls.append(operation_id)
            return listItems

        resolver = StrictParametersResolver(_resolve)
        operation = MagicMock(strict_validation=True)
        resolution = resolver.resolve(operation)
        assert isinstance(resolution, Resolution)
        assert len(calls) == 1


class TestResponses:
    """Tests of the format checks of responses."""

    def test_invalid(self, tmp_path: Path) -> None:
        """Responses with invalid values fail, with `validate_responses`."""
        path = tmp_path / "logs.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "openapi": "3.0.3",
                    "info": {"title": "Logs", "version": "1.0"},
                    "paths": {
                        "/log": {
                            "get": {
                                "operationId": f"{__name__}.getLog",
                                "responses": {
                                    "200": {
                                        "description": "Log",
                                        "content": {
                                            "application/json": {
                                                "schema": {
                                                    "type": "object",
                                                    "properties": {
                                                        "created": DATE_TIME
                                                    },
                                                }
                                            }
                                        },
                                    }
                                },
                            }
                        }
                    },
                }
            )
        )
        cnx_app = register_exception_handler(create_connexion_app(Config()))
        spec = SpecConfig.model_validate(
            {
                "path": path,
                "disable_auth": True,
                "connexion": {"validate_responses": True},
            }
        )
        client = register_openapi(cnx_app, specs=[spec]).app.test_client()
        assert client.get("/log").status_code == 500


class TestValidatorMap:
    """Tests of validators of apps."""

    def test_custom_body_validator(self, tmp_path: Path) -> None:
        """Validators of apps win."""
        app = _create_app(tmp_path, validator_map={"body": CustomBodyValidator})
        assert _post(app, "{}").status_code == 500


class TestDate:
    """Tests of the format `date`."""

    @pytest.mark.parametrize(
        ("value", "valid"),
        [
            ("2026-11-04", True),
            ("2023-02-30", False),
            ("0000-01-01", False),
            ("2026-1-4", False),
            ("٢٠٢٦-11-04", False),
            (1, True),
            (None, True),
        ],
    )
    def test_is_date(self, value: object, valid: bool) -> None:
        """Valid dates and non-strings pass."""
        assert is_date(value) is valid
