"""Tests for `foca.errors.classes` and `foca.errors.problems`."""

import ast
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import werkzeug.exceptions as wz
from connexion import exceptions as connexion_exceptions

import foca.errors
from foca.errors import exceptions as adapter
from foca.errors import problems as core
from foca.errors.classes import (
    AccessDeniedProblem,
    AuthenticationProblem,
    BadRequest,
    Conflict,
    ExtraParameterProblem,
    Forbidden,
    HTTPError,
    InternalServerError,
    MethodNotAllowed,
    NotFound,
    PayloadTooLarge,
    RequestValidationProblem,
    ServiceUnavailable,
    Unauthorized,
    UnprocessableContent,
)
from foca.errors.exceptions import _warn_werkzeug_mappings
from foca.errors.problems import build_headers, build_problem
from foca.models.config import ExceptionConfig

PAIRS = [
    (BadRequest, wz.BadRequest, 400),
    (Unauthorized, wz.Unauthorized, 401),
    (Forbidden, wz.Forbidden, 403),
    (NotFound, wz.NotFound, 404),
    (MethodNotAllowed, wz.MethodNotAllowed, 405),
    (Conflict, wz.Conflict, 409),
    (PayloadTooLarge, wz.RequestEntityTooLarge, 413),
    (UnprocessableContent, wz.UnprocessableEntity, 422),
    (InternalServerError, wz.InternalServerError, 500),
    (ServiceUnavailable, wz.ServiceUnavailable, 503),
]


def _conf(problems: dict[type[BaseException], dict[str, Any]]) -> ExceptionConfig:
    """Build settings with an exception map.

    Args:
        problems: Exception map.

    Returns:
        Exception handling settings.
    """
    conf = ExceptionConfig()
    conf._problems = problems
    return conf


def test_exports() -> None:
    """All classes and aliases are exported."""
    expected = {
        "AccessDeniedProblem",
        "AuthenticationProblem",
        "BadRequest",
        "Conflict",
        "ExtraParameterProblem",
        "Forbidden",
        "HTTPError",
        "InternalServerError",
        "MethodNotAllowed",
        "NotFound",
        "PayloadTooLarge",
        "RequestValidationProblem",
        "ServiceUnavailable",
        "Unauthorized",
        "UnprocessableContent",
    }
    assert expected <= set(foca.errors.__all__)
    for name in expected:
        assert hasattr(foca.errors, name)


def test_aliases() -> None:
    """The stable names are those of Connexion's classes."""
    assert RequestValidationProblem is connexion_exceptions.BadRequestProblem
    assert ExtraParameterProblem is connexion_exceptions.ExtraParameterProblem
    assert AuthenticationProblem is connexion_exceptions.OAuthProblem
    assert AccessDeniedProblem is connexion_exceptions.Forbidden


@pytest.mark.parametrize(("cls", "base", "code"), PAIRS)
class TestPairs:
    """Tests for each class and its Werkzeug counterpart."""

    def test_attributes(self, cls: type[HTTPError], base: type[Any], code: int) -> None:
        """Classes have the status and default description of their counterparts."""
        exc = cls()
        assert isinstance(exc, base)
        assert isinstance(exc, HTTPError)
        assert exc.code == code
        assert exc.description == base.description
        assert cls("Why").description == "Why"

    def test_same_problem_without_map(
        self, cls: type[HTTPError], base: type[Any], code: int
    ) -> None:
        """Without a map, a class and its counterpart give the same problem."""
        conf = ExceptionConfig()
        assert build_problem(cls(), conf) == build_problem(base(), conf)

    @pytest.mark.parametrize("mapped", ["foca", "werkzeug"])
    def test_same_problem_with_map(
        self, cls: type[HTTPError], base: type[Any], code: int, mapped: str
    ) -> None:
        """A mapping of either applies to both."""
        problem = {"title": "Mapped", "status": code}
        conf = _conf({cls if mapped == "foca" else base: problem})
        assert build_problem(cls(), conf) == (problem, code)
        assert build_problem(base(), conf) == (problem, code)


class TestResolution:
    """Tests for the order of classes in exception maps."""

    @pytest.mark.parametrize("base", [HTTPError, wz.HTTPException])
    @pytest.mark.parametrize("specific", [NotFound, wz.NotFound])
    def test_specific_before_base(self, base: type[Any], specific: type[Any]) -> None:
        """Specific classes win over the bases of all HTTP errors, for both kinds."""
        generic = {"title": "Generic", "status": 500}
        mapped = {"title": "Specific", "status": 404}
        conf = _conf({base: generic, specific: mapped})
        assert build_problem(NotFound(), conf)[0] == mapped
        assert build_problem(wz.NotFound(), conf)[0] == mapped
        assert build_problem(Conflict(), conf)[0] == generic


class TestHeaders:
    """Tests for the headers of errors."""

    @pytest.mark.parametrize(
        "headers", [{"Retry-After": "120"}, [("Retry-After", "120")]]
    )
    def test_headers(self, headers: Any) -> None:
        """Headers of a class go into responses, without those of its body."""
        exc = ServiceUnavailable(headers=headers)
        assert exc.headers == [("Retry-After", "120")]
        assert build_headers(exc, 503) == [("Retry-After", "120")]

    def test_several_values(self) -> None:
        """Lists of values give several headers."""
        fields = {"X-A": ["1", "2"]}
        expected = [("X-A", "1"), ("X-A", "2")]
        assert build_headers(NotFound(headers=fields), 404) == expected
        exc = connexion_exceptions.ProblemException(status=400, headers=fields)
        assert build_headers(exc, 400) == expected

    def test_other_status(self) -> None:
        """Headers aren't kept for other statuses."""
        assert build_headers(NotFound(headers={"X-A": "1"}), 500) == []

    def test_arguments_of_werkzeug(self) -> None:
        """Other arguments of the Werkzeug class keep working."""
        exc = MethodNotAllowed(valid_methods=["GET"], headers={"X-A": "1"})
        assert build_headers(exc, 405) == [("Allow", "GET"), ("X-A", "1")]

    def test_body_headers_dropped(self) -> None:
        """Headers of the body are dropped, whatever their case."""
        exc = NotFound(headers={"content-length": "1", "X-A": "1"})
        assert build_headers(exc, 404) == [("X-A", "1")]

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [({"X-A": "1"}, [("X-A", "1")]), ([("X-A", "1")], [("X-A", "1")]), (None, [])],
    )
    def test_problem_exception(self, fields: Any, expected: Any) -> None:
        """Headers of Connexion's problems are taken."""
        exc = connexion_exceptions.ProblemException(status=400, headers=fields)
        assert build_headers(exc, 400) == expected

    def test_bearer_challenge(self) -> None:
        """Connexion's security exceptions get a challenge if they have none."""
        assert build_headers(AuthenticationProblem(), 401) == [
            ("WWW-Authenticate", "Bearer")
        ]


class TestDeprecation:
    """Tests for the warning about raw Werkzeug classes in exception maps."""

    @pytest.fixture(autouse=True)
    def _forget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Forget earlier warnings."""
        monkeypatch.setattr(adapter, "_warned_mappings", set())

    def test_once_per_class(self, caplog: pytest.LogCaptureFixture) -> None:
        """Raw classes are warned of once each."""
        conf = _conf(
            {
                wz.NotFound: {"title": "A", "status": 404},
                wz.Conflict: {"title": "B", "status": 409},
            }
        )
        with caplog.at_level(logging.WARNING):
            _warn_werkzeug_mappings(conf)
            _warn_werkzeug_mappings(conf)
        assert len(caplog.records) == 2
        assert "werkzeug.exceptions.NotFound" in caplog.text

    def test_stable_names(self, caplog: pytest.LogCaptureFixture) -> None:
        """Classes and names of `foca.errors` aren't warned of."""
        conf = _conf(
            {
                NotFound: {"title": "A", "status": 404},
                AccessDeniedProblem: {"title": "B", "status": 403},
                RequestValidationProblem: {"title": "C", "status": 400},
            }
        )
        with caplog.at_level(logging.WARNING):
            _warn_werkzeug_mappings(conf)
        assert not caplog.records

    def test_registering(self, caplog: pytest.LogCaptureFixture) -> None:
        """Registering the handler warns."""
        with caplog.at_level(logging.WARNING):
            adapter.register_exception_handler(
                MagicMock(), _conf({wz.Gone: {"title": "G", "status": 410}})
            )
        assert "werkzeug.exceptions.Gone" in caplog.text


def test_core_without_flask() -> None:
    """The core and the classes don't import Flask."""
    for module in (core, foca.errors.classes):
        tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
        names = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "flask" not in names


def test_no_raw_raises() -> None:
    """FOCA's own code imports no Werkzeug or Connexion exceptions to raise them."""
    root = Path(foca.errors.__file__ or "").parents[1]
    allowed = {"errors/classes.py", "errors/problems.py", "errors/exceptions.py"}
    for path in root.rglob("*.py"):
        if str(path.relative_to(root)) in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module in ("werkzeug.exceptions", "connexion.exceptions")
            for alias in node.names
        }
        raised = {
            getattr(getattr(node.exc, "func", node.exc), "id", None)
            for node in ast.walk(tree)
            if isinstance(node, ast.Raise) and node.exc is not None
        }
        assert not imported & raised, path
