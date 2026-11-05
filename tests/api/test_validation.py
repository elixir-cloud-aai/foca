"""Tests for the stricter checks of requests (`foca.api.validation`)."""

import json
import logging
from pathlib import Path
from typing import Any, ClassVar, NoReturn
from unittest.mock import MagicMock

import pytest
import yaml
from connexion.decorators.validation import RequestBodyValidator
from connexion.resolver import Resolution
from flask import Flask
from werkzeug.test import TestResponse

from foca.api import validation
from foca.api.register_openapi import register_openapi
from foca.api.validation import (
    BodyValidator,
    ParameterValidator,
    StrictParametersResolver,
    _has_unpaired_surrogates,
    _parameter_errors,
)
from foca.errors import exceptions as exceptions_module
from foca.errors.exceptions import register_exception_handler
from foca.errors.fields import INVALID_REQUEST_TYPE, FieldError
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, SpecConfig
from foca.utils.service_info import is_date
from tests.api.test_problem_details import problem_validator

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
        "/things/{thingId}": {
            "put": {
                "operationId": f"{__name__}.putThing",
                "parameters": [
                    {
                        "name": "thingId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "pattern": "^[a-z]+$"},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "maximum": 10},
                    },
                    {
                        "name": "tag",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "X-Mode",
                        "in": "header",
                        "schema": {"type": "string", "enum": ["a", "b"]},
                    },
                    {
                        "name": "session",
                        "in": "cookie",
                        "schema": {"type": "string", "maxLength": 3},
                    },
                    {
                        "name": "note",
                        "in": "query",
                        "schema": {"type": "string", "nullable": True, "maxLength": 1},
                    },
                    {
                        "name": "arr",
                        "in": "query",
                        "schema": {"type": "array", "items": {"enum": ["x"]}},
                    },
                    {
                        "name": "ids",
                        "in": "query",
                        "schema": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                        },
                    },
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name", "url"],
                                "additionalProperties": False,
                                "properties": {
                                    "id": {"type": "string", "readOnly": True},
                                    "name": {"type": "string", "maxLength": 3},
                                    "url": {"type": "string", "format": "uri"},
                                    "organization": {
                                        "type": "object",
                                        "properties": {
                                            "url": {
                                                "type": "string",
                                                "format": "uri",
                                            }
                                        },
                                    },
                                },
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "Thing"}},
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


def putThing(thingId: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Controller with parameters and a body.

    Args:
        thingId: Identifier of the thing.
        body: Body of the request.
        **kwargs: Query parameters.

    Returns:
        The body.
    """
    return body


def postForm(**kwargs: Any) -> str:
    """Controller with form data.

    Args:
        **kwargs: Form data.

    Returns:
        Text.
    """
    return "ok"


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
        with Flask(__name__).test_request_context("/items"):
            assert resolution.function() == []


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


class TestFieldErrors:
    """Tests of the invalid fields of rejected requests (`errors`)."""

    URL = "/things/abc?tag=t"
    VALID: ClassVar[dict[str, str]] = {"name": "abc", "url": "https://example.org"}

    def test_valid(self, tmp_path: Path) -> None:
        """Valid requests pass."""
        client = _create_app(tmp_path).test_client()
        client.set_cookie("session", "abc")
        response = client.put(
            self.URL + "&limit=1&ids=1&ids=2&note=null",
            json=self.VALID,
            headers={"X-Mode": "a"},
        )
        assert response.status_code == 200

    def test_body(self, tmp_path: Path) -> None:
        """All invalid fields of the body are listed; the detail stays."""
        client = _create_app(tmp_path).test_client()
        body = {
            "id": "x",
            "name": "abcd",
            "organization": {"url": "not a uri"},
            "other": 1,
        }
        response = client.put(self.URL, json=body)
        assert response.status_code == 400
        data = response.get_json()
        assert data["title"] == "Bad Request"
        assert data["status"] == 400
        # Connexion's message of the first error, as before
        assert data["detail"] == (
            "Additional properties are not allowed ('other' was unexpected)"
        )
        assert data["type"] == INVALID_REQUEST_TYPE
        assert data["code"] == "invalid-request"
        assert [(e["pointer"], e["code"]) for e in data["errors"]] == [
            ("#/other", "unknown-property"),
            ("#/id", "read-only"),
            ("#/name", "too-long"),
            ("#/organization/url", "invalid-format"),
            ("#/url", "required"),
        ]
        problem_validator().validate(data)

    def test_parameters(self, tmp_path: Path) -> None:
        """All invalid parameters are listed; the detail stays."""
        client = _create_app(tmp_path).test_client()
        client.set_cookie("session", "abcd")
        response = client.put(
            "/things/ABC?limit=11&ids=0&ids=x",
            json=self.VALID,
            headers={"X-Mode": "c"},
        )
        assert response.status_code == 400
        data = response.get_json()
        # Connexion's message on the first, as before
        assert data["detail"].startswith("11 is greater than the maximum of 10")
        assert data["code"] == "invalid-request"
        assert data["errors"] == [
            {
                "detail": "Must be at most 10.",
                "parameter": "limit",
                "code": "too-large",
            },
            {"detail": "Is required.", "parameter": "tag", "code": "required"},
            {"detail": "Must be at least 1.", "parameter": "ids", "code": "too-small"},
            {
                "detail": "Must be of type integer.",
                "parameter": "ids",
                "code": "invalid-type",
            },
            {
                "detail": "Must match the pattern '^[a-z]+$'.",
                "parameter": "thingId",
                "code": "pattern-mismatch",
            },
            {
                "detail": 'Must be one of: "a", "b".',
                "parameter": "X-Mode",
                "code": "invalid-value",
            },
            {
                "detail": "Must be at most 3 characters long.",
                "parameter": "session",
                "code": "too-long",
            },
        ]

    def test_parameter_type(self, tmp_path: Path) -> None:
        """Parameters that can't be converted have the wrong type."""
        client = _create_app(tmp_path).test_client()
        response = client.put(self.URL + "&limit=x", json=self.VALID)
        data = response.get_json()
        assert data["detail"] == (
            "Wrong type, expected 'integer' for query parameter 'limit'"
        )
        assert data["errors"] == [
            {
                "detail": "Must be of type integer.",
                "parameter": "limit",
                "code": "invalid-type",
            }
        ]

    def test_extra_parameters(self, tmp_path: Path) -> None:
        """Unknown query parameters are listed."""
        client = _create_app(tmp_path).test_client()
        response = client.put(self.URL + "&b=1&a=2", json=self.VALID)
        assert [e["parameter"] for e in response.get_json()["errors"]] == ["a", "b"]

    def test_extra_and_invalid_parameters(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown and invalid declared parameters are listed together."""
        client = _create_app(tmp_path).test_client()
        with caplog.at_level(logging.DEBUG):
            response = client.put("/things/abc?limit=s3cr3t&b=1", json=self.VALID)
        data = response.get_json()
        assert data["detail"] == "Extra query parameter(s) b not in spec"
        assert [(e["parameter"], e["code"]) for e in data["errors"]] == [
            ("b", "unknown-parameter"),
            ("limit", "invalid-type"),
            ("tag", "required"),
        ]
        assert "s3cr3t" not in caplog.text

    def test_values_not_echoed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Invalid fields and logs don't quote rejected values."""
        leaky = "https://user:s3cr3t@example.org/ x"
        client = _create_app(tmp_path).test_client()
        with caplog.at_level(logging.DEBUG):
            response = client.put(
                self.URL,
                json={"name": "abc", "url": leaky, "organization": {"url": leaky}},
            )
        data = response.get_json()
        assert len(data["errors"]) == 2
        assert "s3cr3t" not in json.dumps(data["errors"])
        assert "s3cr3t" not in caplog.text

    def test_no_details(self, tmp_path: Path) -> None:
        """Without validation details, responses are as before."""
        path = tmp_path / "spec.yaml"
        path.write_text(yaml.safe_dump(SPEC))
        config = Config()
        config.exceptions.validation_details = False
        cnx_app = register_exception_handler(
            create_connexion_app(config), config.exceptions
        )
        spec = SpecConfig.model_validate(
            {
                "path": path,
                "disable_auth": True,
                "connexion": {"strict_validation": True},
            }
        )
        client = register_openapi(cnx_app, specs=[spec]).app.test_client()
        response = client.put(self.URL, json={})
        assert response.get_json() == {"title": "Bad Request", "status": 400}

    def test_null_body(self, tmp_path: Path) -> None:
        """Nullable bodies may be null."""
        validator = BodyValidator(
            {"type": "object", "nullable": True}, ["application/json"], None, True
        )
        assert validator.validate_schema(None, "/") is None

    def test_form_data(self, tmp_path: Path) -> None:
        """Invalid form data parameters (Swagger 2) are listed."""
        path = tmp_path / "swagger.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "swagger": "2.0",
                    "info": {"title": "Forms", "version": "1.0"},
                    "paths": {
                        "/forms": {
                            "post": {
                                "operationId": f"{__name__}.postForm",
                                "consumes": ["multipart/form-data"],
                                "parameters": [
                                    {
                                        "name": "name",
                                        "in": "formData",
                                        "type": "string",
                                        "maxLength": 2,
                                    },
                                    {
                                        "name": "upload",
                                        "in": "formData",
                                        "type": "file",
                                        "required": True,
                                    },
                                ],
                                "responses": {"200": {"description": "Form"}},
                            }
                        }
                    },
                }
            )
        )
        cnx_app = register_exception_handler(create_connexion_app(Config()))
        spec = SpecConfig.model_validate({"path": path, "disable_auth": True})
        client = register_openapi(cnx_app, specs=[spec]).app.test_client()
        response = client.post("/forms", data={"name": "abc"})
        assert response.status_code == 400
        assert response.get_json()["errors"] == [
            {
                "detail": "Must be at most 2 characters long.",
                "parameter": "name",
                "code": "too-long",
            },
            {"detail": "Is required.", "parameter": "upload", "code": "required"},
        ]

    def test_parameter_without_error(self) -> None:
        """Parameters rejected without a schema error get a generic entry."""
        param = {"name": "upload", "in": "formData", "type": "file"}
        assert _parameter_errors("formdata", object(), param) == []
        validator = ParameterValidator([param], None)
        reason = validator.validate_parameter("formdata", object(), param)
        assert reason
        assert getattr(reason, "field_errors", None) == [
            FieldError(detail="Is invalid.", code="invalid", parameter="upload")
        ]

    def test_later_parameter_fails(self, tmp_path: Path) -> None:
        """Parameters that Connexion can't check after the first invalid one.

        Connexion's check of `arr` raises (its items have no type), but only
        the first invalid parameter is checked by Connexion, as before. As
        the fields of `arr` can't be listed, none are.
        """
        client = _create_app(tmp_path).test_client()
        response = client.put("/things/abc?limit=11&tag=t&arr=x", json=self.VALID)
        assert response.status_code == 400
        data = response.get_json()
        assert data["detail"].startswith("11 is greater than the maximum of 10")
        assert "errors" not in data

    def test_later_parameters_not_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Values of invalid parameters after the first aren't logged."""
        client = _create_app(tmp_path).test_client()
        with caplog.at_level(logging.DEBUG):
            response = client.put(
                "/things/abc?limit=11&note=null",
                json=self.VALID,
                headers={"X-Mode": "s3cr3t"},
            )
        assert [e["parameter"] for e in response.get_json()["errors"]] == [
            "limit",
            "tag",
            "X-Mode",
        ]
        assert "s3cr3t" not in caplog.text


class TestListingFails:
    """Requests are rejected as before if their invalid fields can't be listed."""

    @staticmethod
    def _fail(*args: Any, **kwargs: Any) -> NoReturn:
        """Fail to list invalid fields.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Raises:
            TypeError: Always.
        """
        raise TypeError("unexpected")

    def test_body(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invalid bodies are rejected without invalid fields."""
        monkeypatch.setattr(validation, "schema_field_errors", self._fail)
        response = _create_app(tmp_path).test_client().put(TestFieldErrors.URL, json={})
        assert response.status_code == 400
        assert "errors" not in response.get_json()

    def test_parameters(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invalid parameters are rejected without invalid fields."""
        monkeypatch.setattr(validation, "_parameter_errors", self._fail)
        client = _create_app(tmp_path).test_client()
        response = client.put("/things/abc?limit=11", json=TestFieldErrors.VALID)
        assert response.status_code == 400
        data = response.get_json()
        assert "errors" not in data
        assert data["detail"].startswith("11 is greater than the maximum of 10")

    def test_later_parameter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fields aren't listed in part if those of a parameter can't be."""
        calls: list[str] = []
        listed = validation._parameter_errors

        def _fail_once(*args: Any) -> list[FieldError]:
            """Fail for the first invalid parameter only.

            Args:
                *args: Arguments of `_parameter_errors()`.

            Returns:
                Invalid fields.

            Raises:
                TypeError: On the first call.
            """
            calls.append("call")
            if len(calls) == 1:
                raise TypeError("unexpected")
            return listed(*args)

        monkeypatch.setattr(validation, "_parameter_errors", _fail_once)
        client = _create_app(tmp_path).test_client()
        response = client.put("/things/abc?limit=11", json=TestFieldErrors.VALID)
        assert response.status_code == 400
        assert "errors" not in response.get_json()
        assert len(calls) > 1

    def test_extra_parameters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown parameters are listed alone if declared ones can't be."""
        monkeypatch.setattr(validation, "_parameter_errors", self._fail)
        client = _create_app(tmp_path).test_client()
        response = client.put("/things/abc?limit=11&b=1", json=TestFieldErrors.VALID)
        assert [e["parameter"] for e in response.get_json()["errors"]] == ["b"]

    def test_handler(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Responses keep their status if the handler can't list the fields."""
        monkeypatch.setattr(exceptions_module, "add_field_errors", self._fail)
        response = _create_app(tmp_path).test_client().put(TestFieldErrors.URL, json={})
        assert response.status_code == 400
        assert set(response.get_json()) == {"title", "status", "detail"}

    def test_empty_parameter_name(self, tmp_path: Path) -> None:
        """Unknown parameters without name are listed."""
        response = _create_app(tmp_path).test_client().get("/items?=1")
        assert response.status_code == 400
        assert response.get_json()["errors"] == [
            {"detail": "Is not allowed.", "parameter": "", "code": "unknown-parameter"}
        ]
