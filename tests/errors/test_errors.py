"""Tests for exceptions.py."""

import json
import logging
import sys
from collections import deque
from collections.abc import Iterator
from http import HTTPStatus
from types import ModuleType, SimpleNamespace
from typing import Any, NoReturn

import pytest
from connexion import App
from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    NonConformingResponse,
    NonConformingResponseBody,
    OAuthProblem,
    ProblemException,
)
from flask import Response
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError
from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import (
    Conflict,
    HTTPException,
    MethodNotAllowed,
    NotFound,
    RequestedRangeNotSatisfiable,
    RequestEntityTooLarge,
    RequestURITooLarge,
    ServiceUnavailable,
    Unauthorized,
    UnprocessableEntity,
)
from werkzeug.routing import RequestRedirect

from foca.api.routes import _install_route_table
from foca.errors import exceptions as exceptions_module
from foca.errors.classes import InvalidFieldsProblem
from foca.errors.exceptions import (
    _RFC_9110_PHRASES,
    DEFAULT_PROBLEM,
    _build_headers,
    _exc_to_str,
    _log_exception,
    _shorten,
    _validation_reason,
    build_problem,
    problem_response,
    register_exception_handler,
    shorten_detail,
)
from foca.errors.fields import INVALID_REQUEST_TYPE, FieldError
from foca.models.config import ExceptionConfig, ExceptionLoggingEnum
from foca.testing import Client

THIS_MODULE = __name__


class PetNotFound(NotFound):
    """Pet not found, a listed exception."""


class PetGone(PetNotFound):
    """Pet gone, an unlisted subclass of a listed exception."""


class PetstoreClosed(Exception):
    """Petstore closed, a listed exception without status."""


class UnknownException(Exception):
    """Exception without status that is not listed."""


class StarletteLikeException(Exception):
    """Exception with a Starlette-like `status_code`."""

    status_code = 409


class InvalidStatusException(Exception):
    """Exception with invalid status attributes."""

    code = True
    status = 200
    status_code = "418"


class UnassignedStatusException(Exception):
    """Exception with an unassigned status code."""

    status = 499


class UndescribedException(HTTPException):
    """werkzeug HTTP exception with a code, but without description."""

    code = 404


PROBLEMS: dict[type[Exception], dict[str, Any]] = {
    NotFound: {"title": "Not Found", "status": 404},
    PetNotFound: {
        "title": "Pet Not Found",
        "status": 404,
        "detail": "We have never heard of this pet.",
        "type": "https://example.org/problems/pet-not-found",
    },
    PetstoreClosed: {"title": "Closed", "status": 503},
}
PROBLEMS_CATCH_ALL: dict[type[Exception], dict[str, Any]] = {
    **PROBLEMS,
    Exception: {"title": "Oops", "status": 500, "detail": "Unexpected."},
}


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Messages logged via `logger.error()` of the exceptions module.

    Captured directly, as other tests (re)configure logging.
    """
    messages: list[str] = []
    monkeypatch.setattr(exceptions_module.logger, "error", messages.append)
    return messages


@pytest.fixture
def conf(monkeypatch: pytest.MonkeyPatch) -> ExceptionConfig:
    """Exception config with mapping `PROBLEMS`."""
    module = ModuleType("problems_module")
    vars(module).update(problems=PROBLEMS, catch_all=PROBLEMS_CATCH_ALL)
    monkeypatch.setitem(sys.modules, "problems_module", module)
    return ExceptionConfig(mapping="problems_module.problems")


class TestBuildProblem:
    """Tests for building problem details, without any app."""

    @pytest.mark.parametrize(
        "exception,problem",
        [
            # listed exceptions
            (NotFound(), PROBLEMS[NotFound]),
            (PetNotFound(), PROBLEMS[PetNotFound]),
            (PetstoreClosed(), PROBLEMS[PetstoreClosed]),
            # closest listed base class
            (PetGone(), PROBLEMS[PetNotFound]),
            # status codes of exceptions not listed
            (
                ServiceUnavailable(),
                {"title": "Service Unavailable", "status": 503},
            ),
            (BadRequestProblem(), {"title": "Bad Request", "status": 400}),
            (OAuthProblem(), {"title": "Unauthorized", "status": 401}),
            (StarletteLikeException(), {"title": "Conflict", "status": 409}),
            (UnassignedStatusException(), {"title": "Error", "status": 499}),
            # RFC 9110 reason phrases on all Python versions
            (
                RequestEntityTooLarge(),
                {"title": "Content Too Large", "status": 413},
            ),
            (RequestURITooLarge(), {"title": "URI Too Long", "status": 414}),
            (
                RequestedRangeNotSatisfiable(),
                {"title": "Range Not Satisfiable", "status": 416},
            ),
            (
                UnprocessableEntity(),
                {"title": "Unprocessable Content", "status": 422},
            ),
            # no (valid) status code
            (InvalidStatusException(), DEFAULT_PROBLEM),
            (UnknownException(), DEFAULT_PROBLEM),
            (KeyError("x"), DEFAULT_PROBLEM),
        ],
    )
    def test_resolution(
        self, conf: ExceptionConfig, exception: Exception, problem: dict[str, Any]
    ) -> None:
        """Problem details are resolved from the mapping or the status."""
        assert build_problem(exception, conf) == (problem, problem["status"])

    @pytest.mark.skipif(
        sys.version_info < (3, 13),
        reason="HTTPStatus has RFC 9110 phrases since Python 3.13",
    )
    def test_rfc_9110_phrases(self) -> None:
        """Overridden phrases are those of `HTTPStatus` on newer Pythons."""
        assert {
            status: HTTPStatus(status).phrase for status in _RFC_9110_PHRASES
        } == _RFC_9110_PHRASES

    def test_catch_all(self, conf: ExceptionConfig) -> None:
        """Catch-all entries apply only to exceptions without status."""
        conf.mapping = "problems_module.catch_all"
        conf = ExceptionConfig.model_validate(conf.model_dump())
        catch_all = PROBLEMS_CATCH_ALL[Exception]
        assert build_problem(UnknownException(), conf) == (catch_all, 500)
        assert build_problem(PetGone(), conf)[0] == PROBLEMS[PetNotFound]
        assert build_problem(ServiceUnavailable(), conf) == (
            {"title": "Service Unavailable", "status": 503},
            503,
        )

    def test_defaults(self) -> None:
        """Without a mapping, problem details are resolved by status."""
        assert build_problem(NotFound(), ExceptionConfig()) == (
            {"title": "Not Found", "status": 404},
            404,
        )
        assert build_problem(UnknownException(), ExceptionConfig()) == (
            DEFAULT_PROBLEM,
            500,
        )

    @pytest.mark.parametrize(
        "public_members,expected",
        [
            (None, PROBLEMS[PetNotFound]),
            (
                ["title", "detail"],
                {
                    "title": "Pet Not Found",
                    "detail": "We have never heard of this pet.",
                },
            ),
            (["status", "instance"], {"status": 404}),
            ([], {}),
        ],
    )
    def test_public_members(
        self,
        conf: ExceptionConfig,
        public_members: list[str] | None,
        expected: dict[str, Any],
    ) -> None:
        """Only public members of problem details are returned."""
        conf.public_members = public_members
        assert build_problem(PetNotFound(), conf) == (expected, 404)

    def test_mapping_not_modified(self, conf: ExceptionConfig) -> None:
        """Changing returned problem details does not modify the mapping."""
        problem, _ = build_problem(PetNotFound(), conf)
        problem["title"] = "changed"
        assert PROBLEMS[PetNotFound]["title"] == "Pet Not Found"


class TestBuildHeaders:
    """Tests for building headers of error responses, without any app."""

    @pytest.mark.parametrize(
        "exception,headers",
        [
            (MethodNotAllowed(["GET", "PUT"]), [("Allow", "GET, PUT")]),
            (
                Unauthorized(www_authenticate=WWWAuthenticate("basic", {"realm": "x"})),
                [("WWW-Authenticate", "Basic realm=x")],
            ),
            (ServiceUnavailable(retry_after=120), [("Retry-After", "120")]),
            (
                ProblemException(
                    status=429,
                    headers={
                        "Retry-After": "60",
                        "content-type": "text/x",
                        "Content-Length": "3",
                        "Content-Encoding": "gzip",
                        "Transfer-Encoding": "chunked",
                    },
                ),
                [("Retry-After", "60")],
            ),
            # Connexion's security exceptions get a Bearer challenge
            (OAuthProblem(), [("WWW-Authenticate", "Bearer")]),
            (
                OAuthProblem(www_authenticate=WWWAuthenticate("basic", {"realm": "x"})),
                [("WWW-Authenticate", "Basic realm=x")],
            ),
            # no headers (besides `Content-Type`)
            (Unauthorized(), []),
            (ProblemException(status=429), []),
            (NotFound(), []),
            (UnknownException(), []),
        ],
    )
    def test_headers(
        self, exception: Exception, headers: list[tuple[str, str]]
    ) -> None:
        """Headers of exceptions are kept, except those describing the body."""
        status = exceptions_module._get_status(exception) or 500
        assert _build_headers(exception, status).to_wsgi_list() == headers

    @pytest.mark.parametrize(
        "exception",
        [
            MethodNotAllowed(["GET"]),
            OAuthProblem(),
            ProblemException(status=429, headers={"Retry-After": "60"}),
        ],
    )
    def test_other_status(self, exception: Exception) -> None:
        """Headers are dropped if the response has another status."""
        assert _build_headers(exception, 500).to_wsgi_list() == []


def test__exc_to_str() -> None:
    """Test exception reformatter function."""
    try:
        raise UnknownException("x")
    except UnknownException as raised:
        exc = raised
    res = _exc_to_str(exc=exc)
    assert res.startswith("Traceback (most recent call last):\\n")
    assert res.endswith("UnknownException: x")
    assert "\n" not in res
    assert "\n" in _exc_to_str(exc=exc, delimiter="\n")


@pytest.mark.parametrize(
    "format",
    [
        ExceptionLoggingEnum.oneline,
        ExceptionLoggingEnum.minimal,
        ExceptionLoggingEnum.regular,
    ],
)
def test__log_exception(logged: list[str], format: ExceptionLoggingEnum) -> None:
    """Test logging exceptions in different formats."""
    _log_exception(exc=UnknownException("x"), format=format, problem={"a": 1})
    assert len(logged) == 1
    assert "UnknownException" in logged[0]
    assert logged[0].startswith('{"a": 1} ')


@pytest.mark.parametrize(
    "format", [ExceptionLoggingEnum.oneline, ExceptionLoggingEnum.minimal]
)
def test__log_exception_escaped(
    logged: list[str], format: ExceptionLoggingEnum
) -> None:
    """Messages cannot forge log lines in single-line formats."""
    _log_exception(exc=UnknownException("x\nforged\x1b"), format=format)
    assert "\n" not in logged[0]
    assert "x\\nforged\\x1b" in logged[0]


def test__log_exception_none(logged: list[str]) -> None:
    """Test disabled logging of exceptions."""
    _log_exception(exc=UnknownException(), format=ExceptionLoggingEnum.none)
    assert logged == []


# Controllers of test API
def get_pet() -> NoReturn:
    """Raise an unlisted subclass of a listed exception."""
    raise PetGone


def get_closed() -> NoReturn:
    """Raise a listed exception without status."""
    raise PetstoreClosed


def get_error() -> NoReturn:
    """Raise an unlisted exception with a secret message."""
    raise UnknownException("secret")


def get_secured() -> str:
    """Return a response, if authorized."""
    return "ok"


def get_unavailable() -> NoReturn:
    """Raise a werkzeug exception with a `Retry-After` header."""
    raise ServiceUnavailable(retry_after=120)


def get_rate_limited() -> NoReturn:
    """Raise a Connexion exception with headers."""
    raise ProblemException(
        status=429, headers={"Retry-After": "60", "Content-Type": "text/x"}
    )


def get_validated(limit: int) -> str:
    """Return a response, if the request is valid."""
    # never called: requests without the required `limit` fail validation
    return "ok"


def post_validated(body: dict[str, Any]) -> str:
    """Return a response, if the request body is valid."""
    return "ok"


def mock_token_info(token: str) -> NoReturn:
    """Reject any token."""
    raise OAuthProblem(description="Invalid token")


def _operation(operation_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "operationId": f"{THIS_MODULE}.{operation_id}",
        "responses": {"200": {"description": "OK"}},
        **extra,
    }


SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "Test API", "version": "1.0.0"},
    "paths": {
        "/pet": {"get": _operation("get_pet")},
        "/closed": {"get": _operation("get_closed")},
        "/error": {"get": _operation("get_error")},
        "/unavailable": {"get": _operation("get_unavailable")},
        "/rate-limited": {"get": _operation("get_rate_limited")},
        "/validated": {
            "get": _operation(
                "get_validated",
                parameters=[
                    {
                        "name": "limit",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
            )
        },
        "/validated-body": {
            "post": _operation(
                "post_validated",
                requestBody={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "maxLength": 3}
                                },
                            }
                        }
                    },
                },
            )
        },
        "/secured": {"get": _operation("get_secured", security=[{"bearerAuth": []}])},
    },
    "components": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "x-bearerInfoFunc": f"{THIS_MODULE}.mock_token_info",
            },
        },
    },
}


BODY_VALUE = "value-of-the-body"
VALIDATED_RESPONSE = {
    "200": {
        "description": "OK",
        "headers": {
            "X-Required": {"required": True, "schema": {"type": "string"}},
        },
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "labels": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


def get_invalid_body() -> tuple[dict[str, Any], int, dict[str, str]]:
    """Respond with a body that does not match the specification."""
    return {"name": 1, "secret": BODY_VALUE}, 200, {"X-Required": "x"}


def get_invalid_item() -> tuple[dict[str, Any], int, dict[str, str]]:
    """Respond with an invalid item of a list."""
    return {"tags": ["a", 1], "secret": BODY_VALUE}, 200, {"X-Required": "x"}


def get_invalid_key() -> tuple[dict[str, Any], int, dict[str, str]]:
    """Respond with an invalid value under a key taken from the data."""
    return {"labels": {BODY_VALUE: 1}}, 200, {"X-Required": "x"}


def get_invalid_root() -> tuple[str, int, dict[str, str]]:
    """Respond with a body whose root does not match the specification."""
    return BODY_VALUE, 200, {"X-Required": "x"}


def get_invalid_headers() -> dict[str, Any]:
    """Respond without a required header."""
    return {"name": BODY_VALUE}


def get_nonconforming() -> NoReturn:
    """Raise Connexion's exception without a validator's error."""
    raise NonConformingResponse(reason="Custom reason", message=BODY_VALUE)


RESPONSE_SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "Test API", "version": "1.0.0"},
    "paths": {
        f"/{name}": {
            "get": {
                "operationId": f"{THIS_MODULE}.{name}",
                "responses": VALIDATED_RESPONSE,
            }
        }
        for name in (
            "get_invalid_body",
            "get_invalid_item",
            "get_invalid_key",
            "get_invalid_root",
            "get_invalid_headers",
            "get_nonconforming",
        )
    },
}


def _remove_validation_filters(validation_logger: logging.Logger) -> None:
    """Remove FOCA's filters of Connexion's validation logger.

    Apps of other tests add them for the whole process.

    Args:
        validation_logger: Connexion's validation logger.
    """
    validation_logger.removeFilter(exceptions_module._drop_request_validation_records)
    validation_logger.removeFilter(exceptions_module._drop_response_validation_records)


class TestResponseValidation:
    """Tests for responses that fail validation."""

    @pytest.fixture(autouse=True)
    def validation_logger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[logging.Logger]:
        """Connexion's validation logger, enabled for the test."""
        validation_logger = logging.getLogger(exceptions_module._VALIDATION_LOGGER)
        filters = list(validation_logger.filters)
        # other tests may (re)configure logging
        monkeypatch.setattr(validation_logger, "disabled", False)
        monkeypatch.setattr(validation_logger, "propagate", True)
        _remove_validation_filters(validation_logger)
        yield validation_logger
        validation_logger.filters[:] = filters

    @staticmethod
    def _client(conf: ExceptionConfig) -> Client:
        """Client of an app that validates its responses."""
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app, conf)
        # As in FOCA's apps, which know their operations from the route table
        _install_route_table(app)
        app.add_api(RESPONSE_SPEC, validate_responses=True)
        client: Client = Client(app)
        return client

    @pytest.mark.parametrize(
        ("path", "error"),
        [
            ("/get_invalid_body", "failed at schema path '/properties/name/type'"),
            (
                "/get_invalid_item",
                "failed at schema path '/properties/tags/items/type'",
            ),
            (
                "/get_invalid_key",
                "failed at schema path '/properties/labels/additionalProperties/type'",
            ),
            ("/get_invalid_root", "failed at schema path '/type'"),
            ("/get_invalid_headers", "Difference: X-Required"),
            ("/get_nonconforming", "Custom reason"),
        ],
    )
    @pytest.mark.parametrize(
        "logging_format",
        [
            ExceptionLoggingEnum.oneline,
            ExceptionLoggingEnum.minimal,
            ExceptionLoggingEnum.regular,
        ],
    )
    def test_logs_without_body(
        self,
        conf: ExceptionConfig,
        logged: list[str],
        caplog: pytest.LogCaptureFixture,
        validation_logger: logging.Logger,
        logging_format: ExceptionLoggingEnum,
        path: str,
        error: str,
    ) -> None:
        """The operation and error are logged, nothing of the response."""
        conf.logging = logging_format
        client = self._client(conf)
        with caplog.at_level(logging.DEBUG, logger=validation_logger.name):
            response = client.get(path)
        assert response.status_code == 500
        assert json.loads(response.content) == {
            "title": "Internal Server Error",
            "status": 500,
        }
        assert response.headers["content-type"] == conf.media_type
        (message,) = logged
        assert error in message
        assert path.lstrip("/") in message
        assert BODY_VALUE not in message
        assert BODY_VALUE not in caplog.text
        assert "secret" not in message
        assert "secret" not in caplog.text
        assert "\n" not in message

    def test_log_escaped(
        self,
        conf: ExceptionConfig,
        logged: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Line breaks, e.g., in the path of a schema, are escaped."""
        monkeypatch.setattr(
            exceptions_module,
            "_response_validation_error",
            lambda _: "a\nforged line",
        )
        response = self._client(conf).get("/get_invalid_body")
        assert response.status_code == 500
        (message,) = logged
        assert "a\\nforged line" in message
        assert "\n" not in message

    def test_required_names_missing(self) -> None:
        """Missing properties are named, other keys of the body are not."""
        exc = NonConformingResponseBody(message=BODY_VALUE)
        exc.__context__ = JsonSchemaValidationError(
            BODY_VALUE,
            validator="required",
            validator_value=["name", "id"],
            instance={"id": 1, BODY_VALUE: 2},
            schema_path=deque(["required"]),
        )
        assert exceptions_module._response_validation_error(exc) == (
            "failed at schema path '/required': missing name"
        )

    def test_mapping_wins(self, conf: ExceptionConfig, logged: list[str]) -> None:
        """App mappings apply to the response and the logged problem details."""
        problem = {"title": "Bad implementation", "status": 502}
        conf._problems = {**conf.problems, NonConformingResponse: problem}
        response = self._client(conf).get("/get_invalid_body")
        assert response.status_code == 502
        assert json.loads(response.content) == problem
        (message,) = logged
        assert message.startswith(json.dumps(problem))

    def test_logging_none(self, conf: ExceptionConfig, logged: list[str]) -> None:
        """Nothing is logged with logging switched off."""
        conf.logging = ExceptionLoggingEnum.none
        assert self._client(conf).get("/get_invalid_body").status_code == 500
        assert logged == []

    def test_shortened_and_escaped(
        self, conf: ExceptionConfig, logged: list[str]
    ) -> None:
        """Logged errors are shortened, keeping the failed location."""
        conf.max_detail_length = 40
        self._client(conf).get("/get_invalid_body")
        (message,) = logged
        assert "…" in message
        assert message.endswith("/name/type'")

    def test_filter_without_validation_details(
        self, conf: ExceptionConfig, validation_logger: logging.Logger
    ) -> None:
        """Connexion's messages on responses are filtered out without details."""
        conf.validation_details = False
        self._client(conf)

        def record(**extra: Any) -> logging.LogRecord:
            """Log record of Connexion's validation logger."""
            return validation_logger.makeRecord(
                validation_logger.name, logging.ERROR, "", 0, "x", (), None, extra=extra
            )

        assert not validation_logger.filter(record(validator="response"))
        assert validation_logger.filter(record(validator="body"))


class TestHandler:
    """Tests for error responses of an app."""

    @pytest.fixture(
        params=["application/problem+json", "application/json", "text/json"]
    )
    def media_type(self, request: pytest.FixtureRequest) -> str:
        """Configured media type of error responses.

        The default comes first; `text/*` is without charset.
        """
        return request.param

    @pytest.fixture
    def client(self, conf: ExceptionConfig, media_type: str) -> Client:
        """Client of an app with the test API and the given media type."""
        conf.media_type = media_type
        app = App(__name__, skip_error_handlers=True)
        # the handler does not depend on FOCA's app config
        assert not hasattr(app.app.config, "foca")
        register_exception_handler(app, conf)
        app.add_api(SPEC)
        return Client(app)

    @pytest.mark.parametrize(
        "method,path,headers,problem",
        [
            # werkzeug exceptions (route or method not found), mapped or by
            # status code
            ("get", "/unknown", {}, PROBLEMS[NotFound]),
            (
                "post",
                "/pet",
                {},
                {"title": "Method Not Allowed", "status": 405},
            ),
            # Connexion request validation exception, by status code, with
            # the reason (`validation_details` is on by default)
            (
                "get",
                "/validated",
                {},
                {
                    "title": "Bad Request",
                    "status": 400,
                    "detail": "Missing query parameter 'limit'",
                },
            ),
            # Connexion security exceptions, by status code
            ("get", "/secured", {}, {"title": "Unauthorized", "status": 401}),
            (
                "get",
                "/secured",
                {"Authorization": "Bearer x"},
                {"title": "Unauthorized", "status": 401},
            ),
            # custom exceptions, mapped
            ("get", "/pet", {}, PROBLEMS[PetNotFound]),
            ("get", "/closed", {}, PROBLEMS[PetstoreClosed]),
            # unmapped exception
            ("get", "/error", {}, DEFAULT_PROBLEM),
        ],
    )
    def test_responses(
        self,
        client: Client,
        media_type: str,
        method: str,
        path: str,
        headers: dict[str, str],
        problem: dict[str, Any],
    ) -> None:
        """Error responses have problem details of the configured media type."""
        response = client.request(method, path, headers=headers)
        assert response.status_code == problem["status"]
        assert response.headers["content-type"] == media_type
        assert json.loads(response.content) == problem
        assert "secret" not in response.text

    @pytest.mark.parametrize(
        "method,path,headers,name,value",
        [
            # werkzeug exceptions
            ("post", "/pet", {}, "Allow", "GET, HEAD, OPTIONS"),
            ("get", "/unavailable", {}, "Retry-After", "120"),
            # Connexion exceptions
            ("get", "/rate-limited", {}, "Retry-After", "60"),
            ("get", "/secured", {}, "WWW-Authenticate", "Bearer"),
            (
                "get",
                "/secured",
                {"Authorization": "Bearer x"},
                "WWW-Authenticate",
                "Bearer",
            ),
        ],
    )
    def test_headers(
        self,
        client: Client,
        media_type: str,
        method: str,
        path: str,
        headers: dict[str, str],
        name: str,
        value: str,
    ) -> None:
        """Headers of exceptions are kept, except `Content-Type`."""
        response = client.request(method, path, headers=headers)
        values = response.headers.getlist(name)
        assert len(values) == 1
        # order of allowed methods is not defined
        assert sorted(values[0].split(", ")) == sorted(value.split(", "))
        assert response.headers.getlist("Content-Type") == [media_type]

    def test_default_config(self) -> None:
        """Without config, problem details are resolved by status."""
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app)
        app.add_api(SPEC)
        response = Client(app).get("/pet")
        assert response.status_code == 404
        assert json.loads(response.content) == {
            "title": "Not Found",
            "status": 404,
        }

    def test_logged_once_unfiltered(
        self, conf: ExceptionConfig, logged: list[str]
    ) -> None:
        """Exceptions are logged once, with unfiltered problem details."""
        conf.public_members = ["title"]
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app, conf)
        app.add_api(SPEC)
        response = Client(app).get("/pet")
        assert json.loads(response.content) == {"title": "Pet Not Found"}
        assert len(logged) == 1
        assert "pet-not-found" in logged[0]


class TestShorten:
    """Tests for shortening details."""

    @pytest.mark.parametrize(
        "detail,max_length,expected",
        [
            ("abc", 3, "abc"),
            ("", 1, ""),
            ("abcd", 3, "a…d"),
            ("abcde", 4, "ab…e"),
            ("abcde", 1, "…"),
            ("abcde", 2, "a…"),
        ],
    )
    def test__shorten(self, detail: str, max_length: int, expected: str) -> None:
        """Long details are shortened in the middle to the maximum length."""
        assert _shorten(detail, max_length) == expected

    def test__shorten_long(self) -> None:
        """Long details keep their start and end."""
        detail = "a" * 1000 + "z"
        shortened = _shorten(detail, 500)
        assert len(shortened) == 500
        assert shortened == "a" * 250 + "…" + "a" * 248 + "z"

    def test_shorten_detail(
        self, monkeypatch: pytest.MonkeyPatch, conf: ExceptionConfig
    ) -> None:
        """Details are shortened to the length of the app's config."""
        conf.max_detail_length = 5
        monkeypatch.setattr(
            exceptions_module, "get_config", lambda: SimpleNamespace(exceptions=conf)
        )
        assert shorten_detail("abcdefgh") == "ab…gh"
        assert shorten_detail("abcde") == "abcde"

    @pytest.mark.parametrize("max_detail_length", [0, -1])
    def test_max_detail_length_invalid(self, max_detail_length: int) -> None:
        """The maximum length of details must be positive."""
        with pytest.raises(ValidationError):
            ExceptionConfig(max_detail_length=max_detail_length)


class TestProblemResponse:
    """Tests for error responses with the descriptions of exceptions."""

    @pytest.fixture(autouse=True)
    def app_config(
        self, monkeypatch: pytest.MonkeyPatch, conf: ExceptionConfig
    ) -> ExceptionConfig:
        """Exception config of the current app, with mapping `PROBLEMS`."""
        monkeypatch.setattr(
            exceptions_module, "get_config", lambda: SimpleNamespace(exceptions=conf)
        )
        return conf

    @staticmethod
    def _problem(response: Response) -> dict[str, Any]:
        """Problem details of a response."""
        problem: dict[str, Any] = json.loads(response.get_data(as_text=True))
        return problem

    def test_response(self) -> None:
        """Responses have the description, status and extension members."""
        response = problem_response(Conflict("Service 'x' exists."), serviceId="x")
        assert response.status_code == 409
        assert response.headers["content-type"] == "application/problem+json"
        assert self._problem(response) == {
            "title": "Conflict",
            "status": 409,
            "detail": "Service 'x' exists.",
            "serviceId": "x",
        }

    def test_mapped(self) -> None:
        """Responses have the problem details mapped to the class."""
        response = problem_response(PetGone("Pet 'x' is gone."))
        assert self._problem(response) == {
            **PROBLEMS[PetNotFound],
            "detail": "Pet 'x' is gone.",
        }

    def test_config(self, app_config: ExceptionConfig) -> None:
        """Responses have the media type, length and members as configured."""
        app_config.media_type = "application/json"
        app_config.max_detail_length = 5
        app_config.public_members = ["title", "status", "detail"]
        response = problem_response(Conflict("abcdefgh"), serviceId="x")
        assert response.headers["content-type"] == "application/json"
        assert self._problem(response) == {
            "title": "Conflict",
            "status": 409,
            "detail": "ab…gh",
        }

    def test_no_public_detail(self, app_config: ExceptionConfig) -> None:
        """Descriptions are not sent if `detail` is not public."""
        app_config.public_members = ["title", "status"]
        response = problem_response(Conflict("secret"))
        assert self._problem(response) == {"title": "Conflict", "status": 409}

    def test_headers(self) -> None:
        """Responses have the headers of the exception."""
        exc = Unauthorized(
            "Log in.", www_authenticate=WWWAuthenticate("basic", {"realm": "x"})
        )
        response = problem_response(exc)
        assert response.headers.getlist("WWW-Authenticate") == ["Basic realm=x"]
        assert response.headers.getlist("Content-Type") == ["application/problem+json"]

    @pytest.mark.parametrize(
        "exc",
        [
            HTTPException("x"),
            UndescribedException(),
            RequestRedirect("/x"),
            BadRequestProblem(detail="x"),
        ],
    )
    def test_invalid_exception(self, exc: Any) -> None:
        """Exceptions need an HTTP error status code and a description."""
        with pytest.raises(ValueError, match="error status code"):
            problem_response(exc)

    @pytest.mark.parametrize("member", ["status", "detail", "type"])
    def test_reserved_members(self, member: str) -> None:
        """Extension members cannot override those set from the exception."""
        with pytest.raises(ValueError, match=member):
            problem_response(Conflict("x"), **{member: "x"})

    def test_title(self) -> None:
        """Titles may be specific to the request."""
        response = problem_response(Conflict("x"), title="Name Taken")
        assert self._problem(response)["title"] == "Name Taken"


@pytest.mark.parametrize(
    "detail,expected",
    [
        ("Missing query parameter 'limit'", "Missing query parameter 'limit'"),
        (["a", "b"], "a; b"),
        (["a"], "a"),
        ([], None),
        ("", None),
        (None, None),
        ({"a": "b"}, None),
    ],
)
def test__validation_reason(detail: object, expected: str | None) -> None:
    """Reasons are the details, with lists joined, if not empty."""
    assert _validation_reason(BadRequestProblem(detail=detail)) == expected


class TestValidationDetails:
    """Tests for responses to requests rejected by Connexion's validation."""

    @pytest.fixture(autouse=True)
    def validation_logger(self) -> Iterator[logging.Logger]:
        """Connexion's validation logger, with its filters restored after.

        FOCA's filter, added for the whole process by apps of other tests
        (validation details are on by default), is removed for the test.
        """
        validation_logger = logging.getLogger(exceptions_module._VALIDATION_LOGGER)
        filters = list(validation_logger.filters)
        _remove_validation_filters(validation_logger)
        yield validation_logger
        validation_logger.filters[:] = filters

    @pytest.fixture
    def infos(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Messages logged via `logger.info()` of the exceptions module."""
        messages: list[str] = []
        monkeypatch.setattr(
            exceptions_module.logger,
            "info",
            lambda msg, *args: messages.append(msg % args),
        )
        return messages

    @staticmethod
    def _client(conf: ExceptionConfig) -> Client:
        """Client of an app with the test API (strict validation)."""
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app, conf)
        app.add_api(SPEC, strict_validation=True)
        return Client(app)

    @pytest.fixture
    def conf_details(self, conf: ExceptionConfig) -> ExceptionConfig:
        """Exception config with validation details."""
        conf.validation_details = True
        return conf

    @pytest.mark.parametrize(
        "method,path,kwargs,detail,members",
        [
            ("get", "/validated", {}, "Missing query parameter 'limit'", {}),
            (
                "get",
                "/validated",
                {"params": {"limit": 1, "extra": 1}},
                "Extra query parameter(s) extra not in spec",
                {
                    "type": INVALID_REQUEST_TYPE,
                    "code": "invalid-request",
                    "errors": [
                        {
                            "detail": "Is not allowed.",
                            "parameter": "extra",
                            "code": "unknown-parameter",
                        }
                    ],
                },
            ),
            (
                "post",
                "/validated-body",
                {"json": {"name": "abcd"}},
                "'abcd' is too long - 'name'",
                {},
            ),
        ],
    )
    def test_details(
        self,
        conf_details: ExceptionConfig,
        infos: list[str],
        logged: list[str],
        method: str,
        path: str,
        kwargs: dict[str, Any],
        detail: str,
        members: dict[str, Any],
    ) -> None:
        """Responses have the reason as detail, logged at `INFO`.

        Connexion's own validators (without FOCA's `register_openapi()`) give
        no invalid fields, except for unknown parameters.
        """
        conf_details.media_type = "application/json"
        response = self._client(conf_details).request(method, path, **kwargs)
        assert response.status_code == 400
        assert response.headers["content-type"] == "application/json"
        assert json.loads(response.content) == {
            "title": "Bad Request",
            "status": 400,
            "detail": detail,
            **members,
        }
        logged_reason = "extra (unknown-parameter)" if members else detail
        assert infos == [f"Request rejected: {logged_reason}"]
        assert logged == []

    def test_default(self, infos: list[str]) -> None:
        """Validation details are on by default, also without a config."""
        assert ExceptionConfig().validation_details
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app)
        app.add_api(SPEC, strict_validation=True)
        response = Client(app).get("/validated")
        assert json.loads(response.content) == {
            "title": "Bad Request",
            "status": 400,
            "detail": "Missing query parameter 'limit'",
        }
        assert infos == ["Request rejected: Missing query parameter 'limit'"]

    def test_off(self, conf: ExceptionConfig, infos: list[str]) -> None:
        """Without validation details, responses are as mapped, not logged."""
        conf.validation_details = False
        response = self._client(conf).get("/validated")
        assert json.loads(response.content) == {"title": "Bad Request", "status": 400}
        assert infos == []
        validation_logger = logging.getLogger(exceptions_module._VALIDATION_LOGGER)
        assert (
            exceptions_module._drop_request_validation_records
            not in validation_logger.filters
        )

    @pytest.mark.parametrize("validation_details", [False, True])
    def test_connexion_messages(
        self,
        conf: ExceptionConfig,
        caplog: pytest.LogCaptureFixture,
        validation_logger: logging.Logger,
        monkeypatch: pytest.MonkeyPatch,
        validation_details: bool,
    ) -> None:
        """Connexion's request validation messages are only logged without details."""
        # other tests may (re)configure logging
        monkeypatch.setattr(validation_logger, "disabled", False)
        monkeypatch.setattr(validation_logger, "propagate", True)
        conf.validation_details = validation_details
        client = self._client(conf)
        with caplog.at_level(logging.INFO, logger=validation_logger.name):
            client.post("/validated-body", json={"name": "abcd"})
        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == validation_logger.name
        ]
        assert bool(messages) is not validation_details
        assert all("is too long" in message for message in messages)

    def test_logger_filtered(
        self, conf_details: ExceptionConfig, validation_logger: logging.Logger
    ) -> None:
        """Connexion's request validation messages are filtered out, once."""
        self._client(conf_details)
        self._client(conf_details)
        keep = exceptions_module._drop_request_validation_records
        assert validation_logger.filters.count(keep) == 1

        def record(**extra: Any) -> logging.LogRecord:
            """Log record of Connexion's validation logger."""
            return validation_logger.makeRecord(
                validation_logger.name, logging.ERROR, "", 0, "x", (), None, extra=extra
            )

        assert not validation_logger.filter(record(validator="body"))
        assert not validation_logger.filter(record())
        assert not validation_logger.filter(record(validator="response"))

    def test_long_detail(self, conf_details: ExceptionConfig, infos: list[str]) -> None:
        """Long reasons are shortened and escaped in logs."""
        conf_details.max_detail_length = 20
        response = self._client(conf_details).post(
            "/validated-body", json={"name": "a\n" * 100}
        )
        detail = json.loads(response.content)["detail"]
        assert len(detail) == 20
        assert detail.endswith("- 'name'")
        assert len(infos) == 1
        logged = infos[0].removeprefix("Request rejected: ")
        assert len(logged) == 20
        assert logged.startswith("'a\\na\\n")
        assert logged.endswith("- 'name'")
        assert "\n" not in infos[0]

    def test_mapped(
        self,
        conf_details: ExceptionConfig,
        infos: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without a reason, responses have the mapped details."""
        mapped = {
            "title": "Invalid",
            "status": 422,
            "detail": "The request is invalid.",
            "type": "https://example.org/problems/invalid",
        }
        monkeypatch.setitem(PROBLEMS, BadRequestProblem, mapped)
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app, conf_details)

        @app.app.route("/empty")
        def empty() -> NoReturn:
            """Raise a validation exception without reason."""
            raise BadRequestProblem(detail=[])

        response = Client(app).get("/empty")
        assert response.status_code == 422
        assert json.loads(response.content) == mapped
        assert infos == ["Request rejected: The request is invalid."]

    def test_no_reason_no_detail(
        self, conf_details: ExceptionConfig, infos: list[str]
    ) -> None:
        """Without a reason or mapped detail, the title is logged."""
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app, conf_details)

        @app.app.route("/empty")
        def empty() -> NoReturn:
            """Raise a validation exception without reason."""
            raise ExtraParameterProblem([], [])

        response = Client(app).get("/empty")
        assert json.loads(response.content) == {"title": "Bad Request", "status": 400}
        assert infos == ["Request rejected: Bad Request"]

    def test_public_members(
        self, conf_details: ExceptionConfig, infos: list[str]
    ) -> None:
        """Reasons are not sent if `detail` is not public."""
        conf_details.public_members = ["title", "status"]
        response = self._client(conf_details).get("/validated")
        assert json.loads(response.content) == {"title": "Bad Request", "status": 400}
        assert infos == ["Request rejected: Missing query parameter 'limit'"]

    def test_logging_none(
        self, conf_details: ExceptionConfig, infos: list[str]
    ) -> None:
        """Reasons are not logged if logging is disabled."""
        conf_details.logging = ExceptionLoggingEnum.none
        response = self._client(conf_details).get("/validated")
        assert json.loads(response.content)["detail"] == (
            "Missing query parameter 'limit'"
        )
        assert infos == []


class TestInvalidFields:
    """Tests for responses with invalid fields (`errors`)."""

    ERRORS = (
        FieldError(
            detail="Must use HTTPS.", code="scheme-not-allowed", pointer="#/url"
        ),
        FieldError(detail="Is required.", code="required", parameter="limit"),
    )

    @classmethod
    def _client(cls, conf: ExceptionConfig) -> Client:
        """Client of an app whose route raises `InvalidFieldsProblem`."""
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app, conf)

        @app.app.route("/invalid")
        def invalid() -> NoReturn:
            """Raise an exception with invalid fields."""
            raise InvalidFieldsProblem(cls.ERRORS, detail="Invalid URL")

        return Client(app)

    def test_errors(self, conf: ExceptionConfig) -> None:
        """Responses list the invalid fields, with type and code."""
        response = self._client(conf).get("/invalid")
        assert response.status_code == 400
        assert json.loads(response.content) == {
            "title": "Bad Request",
            "status": 400,
            "detail": "Invalid URL",
            "type": INVALID_REQUEST_TYPE,
            "code": "invalid-request",
            "errors": [error.to_dict() for error in self.ERRORS],
        }

    def test_logged(
        self, conf: ExceptionConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The invalid fields are logged by location and code, not the reason."""
        infos: list[str] = []
        monkeypatch.setattr(
            exceptions_module.logger, "info", lambda msg, *a: infos.append(msg % a)
        )
        self._client(conf).get("/invalid")
        assert infos == [
            "Request rejected: #/url (scheme-not-allowed); limit (required)"
        ]

    def test_shortened(self, conf: ExceptionConfig) -> None:
        """Entries are shortened to `max_detail_length`."""
        conf.max_detail_length = 5
        response = self._client(conf).get("/invalid")
        errors = json.loads(response.content)["errors"]
        assert errors[0] == {
            "detail": "Mu…S.",
            "pointer": "#/url",
            "code": "scheme-not-allowed",
        }

    def test_public_members(self, conf: ExceptionConfig) -> None:
        """Only public members are sent."""
        conf.public_members = ["title", "status", "errors"]
        response = self._client(conf).get("/invalid")
        assert set(json.loads(response.content)) == {"title", "status", "errors"}

    def test_mapped_type(
        self, conf: ExceptionConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mapped `type` wins."""
        mapped = {"title": "Bad Request", "status": 400, "type": "about:blank"}
        monkeypatch.setitem(PROBLEMS, BadRequestProblem, mapped)
        data = json.loads(self._client(conf).get("/invalid").content)
        assert data["type"] == "about:blank"
        assert data["code"] == "invalid-request"
        assert len(data["errors"]) == 2

    def test_off(self, conf: ExceptionConfig, logged: list[str]) -> None:
        """Without validation details, responses are as mapped."""
        conf.validation_details = False
        response = self._client(conf).get("/invalid")
        assert json.loads(response.content) == {"title": "Bad Request", "status": 400}
        assert len(logged) == 1

    def test_extra_form_fields(self, conf: ExceptionConfig) -> None:
        """Unknown form fields are named as parameters, like by Connexion."""
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app, conf)

        @app.app.route("/extra")
        def extra() -> NoReturn:
            """Raise an exception for an unknown form field."""
            raise ExtraParameterProblem(["field"], [])

        data = json.loads(Client(app).get("/extra").content)
        assert data["errors"] == [
            {
                "detail": "Is not allowed.",
                "parameter": "field",
                "code": "unknown-parameter",
            }
        ]
