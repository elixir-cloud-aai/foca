"""
Tests for exceptions.py
"""

import json
import sys
from types import ModuleType

import pytest
from connexion import App
from connexion.exceptions import BadRequestProblem, OAuthProblem
from werkzeug.exceptions import NotFound, ServiceUnavailable

from foca.errors import exceptions as exceptions_module
from foca.errors.exceptions import (
    DEFAULT_PROBLEM,
    _exc_to_str,
    _log_exception,
    build_problem,
    register_exception_handler,
)
from foca.models.config import ExceptionConfig, ExceptionLoggingEnum

THIS_MODULE = __name__


class PetNotFound(NotFound):
    pass


class PetGone(PetNotFound):
    pass


class PetstoreClosed(Exception):
    pass


class UnknownException(Exception):
    pass


class StarletteLikeException(Exception):
    status_code = 409


class InvalidStatusException(Exception):
    code = True
    status = 200
    status_code = "418"


class UnassignedStatusException(Exception):
    status = 499


PROBLEMS = {
    NotFound: {"title": "Not Found", "status": 404},
    PetNotFound: {
        "title": "Pet Not Found",
        "status": 404,
        "detail": "We have never heard of this pet.",
        "type": "https://example.org/problems/pet-not-found",
    },
    PetstoreClosed: {"title": "Closed", "status": 503},
}
PROBLEMS_CATCH_ALL = {
    **PROBLEMS,
    Exception: {"title": "Oops", "status": 500, "detail": "Unexpected."},
}


@pytest.fixture
def logged(monkeypatch) -> list[str]:
    """Messages logged via `logger.error()` of the exceptions module.

    Captured directly, as other tests (re)configure logging.
    """
    messages: list[str] = []
    monkeypatch.setattr(exceptions_module.logger, "error", messages.append)
    return messages


@pytest.fixture
def conf(monkeypatch):
    """Exception config with mapping `PROBLEMS`."""
    module = ModuleType("problems_module")
    module.problems = PROBLEMS
    module.catch_all = PROBLEMS_CATCH_ALL
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
            # no (valid) status code
            (InvalidStatusException(), DEFAULT_PROBLEM),
            (UnknownException(), DEFAULT_PROBLEM),
            (KeyError("x"), DEFAULT_PROBLEM),
        ],
    )
    def test_resolution(self, conf, exception, problem):
        assert build_problem(exception, conf) == (problem, problem["status"])

    def test_catch_all(self, conf):
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

    def test_defaults(self):
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
    def test_public_members(self, conf, public_members, expected):
        conf.public_members = public_members
        assert build_problem(PetNotFound(), conf) == (expected, 404)

    def test_mapping_not_modified(self, conf):
        problem, _ = build_problem(PetNotFound(), conf)
        problem["title"] = "changed"
        assert PROBLEMS[PetNotFound]["title"] == "Pet Not Found"


def test__exc_to_str():
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
def test__log_exception(logged, format):
    """Test logging exceptions in different formats."""
    _log_exception(exc=UnknownException("x"), format=format, problem={"a": 1})
    assert len(logged) == 1
    assert "UnknownException" in logged[0]
    assert logged[0].startswith('{"a": 1} ')


def test__log_exception_none(logged):
    """Test disabled logging of exceptions."""
    _log_exception(exc=UnknownException(), format=ExceptionLoggingEnum.none)
    assert logged == []


# Controllers of test API
def get_pet():
    raise PetGone


def get_closed():
    raise PetstoreClosed


def get_error():
    raise UnknownException("secret")


def get_secured():
    return "ok"


def get_validated(limit):
    return "ok"


def mock_token_info(token):
    raise OAuthProblem(description="Invalid token")


def _operation(operation_id: str, **extra) -> dict:
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
        "/secured": {
            "get": _operation("get_secured", security=[{"bearerAuth": []}])
        },
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


class TestHandler:
    """Tests for error responses of an app."""

    @pytest.fixture(params=[None, "application/json"])
    def media_type(self, request) -> str | None:
        """Configured media type of error responses (`None`: default)."""
        return request.param

    @pytest.fixture
    def client(self, conf, media_type):
        if media_type is not None:
            conf.media_type = media_type
        app = App(__name__, skip_error_handlers=True)
        # the handler does not depend on FOCA's app config
        assert not hasattr(app.app.config, "foca")
        register_exception_handler(app, conf)
        app.add_api(SPEC)
        return app.app.test_client()

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
            # Connexion request validation exception, by status code
            (
                "get",
                "/validated",
                {},
                {"title": "Bad Request", "status": 400},
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
        self, client, media_type, method, path, headers, problem
    ):
        response = client.open(path, method=method, headers=headers)
        assert response.status_code == problem["status"]
        assert response.content_type == (
            media_type or "application/problem+json"
        )
        assert json.loads(response.data) == problem
        assert "secret" not in response.get_data(as_text=True)

    def test_default_config(self):
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app)
        app.add_api(SPEC)
        response = app.app.test_client().get("/pet")
        assert response.status_code == 404
        assert json.loads(response.data) == {
            "title": "Not Found",
            "status": 404,
        }

    def test_logged_once_unfiltered(self, conf, logged):
        conf.public_members = ["title"]
        app = App(__name__, skip_error_handlers=True)
        register_exception_handler(app, conf)
        app.add_api(SPEC)
        response = app.app.test_client().get("/pet")
        assert json.loads(response.data) == {"title": "Pet Not Found"}
        assert len(logged) == 1
        assert "pet-not-found" in logged[0]
