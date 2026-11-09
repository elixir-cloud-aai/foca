"""Tests for the reusable schemas of problem details (`problem-details.yaml`)."""

from importlib.resources import files
from typing import Any

import pytest
import yaml
from jsonschema import Draft4Validator

import foca.api
import foca.security.access_control.api
from foca.errors.fields import FieldError, json_pointer

SCHEMAS: dict[str, Any] = yaml.safe_load(
    files(foca.api).joinpath("problem-details.yaml").read_text(encoding="utf-8")
)["components"]["schemas"]


def problem_validator() -> Draft4Validator:
    """Build a validator of problem details against the reusable schema.

    Returns:
        Validator of `Problem`, with `FieldError` resolvable.
    """
    return Draft4Validator(
        {"$ref": "#/components/schemas/Problem", "components": {"schemas": SCHEMAS}}
    )


@pytest.mark.parametrize("name", ["access-control-specs.yaml", "me-specs.yaml"])
def test_specs_of_foca(name: str) -> None:
    """FOCA's own APIs declare the same schemas.

    Their version is 1.1.0 since the `Error` schema gained `code`, `errors`
    and `FieldError` (FOCA 0.22.0).
    """
    path = files(foca.security.access_control.api).joinpath(name)
    specs = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert specs["info"]["version"] == "1.1.0"
    schemas = specs["components"]["schemas"]
    assert schemas["Error"] == SCHEMAS["Problem"]
    assert schemas["FieldError"] == SCHEMAS["FieldError"]


def test_valid() -> None:
    """Problem details with invalid fields match the schema."""
    errors = [
        FieldError(detail="Is required.", code="required", pointer=json_pointer("a")),
        FieldError(detail="Is too long.", code="too-long", parameter="q"),
    ]
    problem = {
        "title": "Bad Request",
        "status": 400,
        "detail": "x",
        "type": "https://example.org",
        "code": "invalid-request",
        "errors": [error.to_dict() for error in errors],
    }
    problem_validator().validate(problem)


@pytest.mark.parametrize(
    "entry",
    [
        {"detail": "d", "code": "c"},
        {"detail": "d", "code": "c", "pointer": "#", "parameter": "p"},
        {"detail": "d", "code": "Not_Kebab", "pointer": "#"},
        {"detail": "d", "code": "c", "pointer": "#abc"},
    ],
)
def test_invalid_entries(entry: dict[str, str]) -> None:
    """Entries need exactly one location and a kebab-case code."""
    problem = {"title": "Bad Request", "status": 400, "errors": [entry]}
    assert not problem_validator().is_valid(problem)
