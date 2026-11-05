"""Tests for `foca.errors.fields`."""

import json
from datetime import date
from typing import Any

import pytest
from connexion.json_schema import Draft4RequestValidator
from jsonschema import Draft4Validator, ValidationError, draft4_format_checker

import foca.api.validation  # noqa: F401  (registers the formats `uri` etc.)
from foca.errors.fields import (
    INVALID_REQUEST_CODE,
    INVALID_REQUEST_TYPE,
    MAX_FIELD_ERRORS,
    FieldError,
    add_field_errors,
    describe_field_errors,
    json_pointer,
    schema_field_errors,
    unknown_parameters,
)

LEAKY_URL = "https://user:s3cr3t@example.org/?token=s3cr3t"


def _errors(
    schema: dict[str, Any],
    instance: Any,
    parameter: str | None = None,
    validator: Any = Draft4RequestValidator,
) -> list[FieldError]:
    """Validate an instance and convert the errors.

    Args:
        schema: JSON Schema.
        instance: Instance to validate.
        parameter: Name of the parameter, if the instance is one.
        validator: Validator class.

    Returns:
        Field errors.
    """
    errors = validator(schema, format_checker=draft4_format_checker).iter_errors(
        instance
    )
    return schema_field_errors(errors, parameter=parameter)


class TestFieldError:
    """Tests of `FieldError`."""

    def test_pointer(self) -> None:
        """Entries with a pointer serialize in the form of RFC 9457."""
        error = FieldError(detail="Is required.", code="required", pointer="#/a")
        assert error.to_dict() == {
            "detail": "Is required.",
            "pointer": "#/a",
            "code": "required",
        }

    def test_parameter(self) -> None:
        """Entries with a parameter serialize with `parameter`."""
        error = FieldError(detail="Too long.", code="too-long", parameter="limit")
        assert error.to_dict() == {
            "detail": "Too long.",
            "parameter": "limit",
            "code": "too-long",
        }

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"pointer": "#/a", "parameter": "a"},
            {"pointer": "/a"},
            {"pointer": ""},
            {"pointer": "#abc"},
            {"pointer": "#/a b"},
            {"pointer": "#/a~2"},
            {"pointer": "#/%zz"},
        ],
    )
    def test_location(self, kwargs: dict[str, str]) -> None:
        """Entries have exactly one location: a URI-fragment pointer or a name."""
        with pytest.raises(ValueError, match=r"(?i)pointer|parameter"):
            FieldError(detail="d", code="c", **kwargs)

    @pytest.mark.parametrize("code", ["", "Required", "too_long", "-a", "a-", "a b"])
    def test_code(self, code: str) -> None:
        """Codes are kebab-case."""
        with pytest.raises(ValueError, match="kebab-case"):
            FieldError(detail="d", code=code, pointer="#")

    def test_empty_parameter(self) -> None:
        """Parameter names may be empty, as in requests (e.g., `?=1`)."""
        assert FieldError(detail="d", code="c", parameter="").parameter == ""

    def test_detail(self) -> None:
        """Entries have a detail."""
        with pytest.raises(ValueError, match="detail"):
            FieldError(detail="", code="c", pointer="#")

    def test_shortened(self) -> None:
        """Long details and parameter names are shortened."""
        error = FieldError(detail="d" * 50, code="c", parameter="p" * 50)
        data = error.to_dict(max_length=10)
        assert len(data["detail"]) == 10
        assert len(data["parameter"]) == 10

    def test_max_length(self) -> None:
        """Maximum lengths are at least 1."""
        with pytest.raises(ValueError, match="at least 1"):
            FieldError(detail="d", code="c", pointer="#").to_dict(max_length=0)

    @pytest.mark.parametrize("pointer", ["#", "#/", "#/a~0b~1c/0/%C3%A4", "#//x"])
    def test_valid_pointers(self, pointer: str) -> None:
        """Pointers as built by `json_pointer()` are accepted."""
        assert FieldError(detail="d", code="c", pointer=pointer).pointer == pointer

    def test_long_pointer(self) -> None:
        """Long pointers are replaced by the closest one that fits."""
        error = FieldError(detail="d", code="c", pointer=json_pointer("a", "b" * 50))
        assert error.to_dict(max_length=10)["pointer"] == "#/a"
        error = FieldError(detail="d", code="c", pointer=json_pointer("a" * 50))
        assert error.to_dict(max_length=10)["pointer"] == "#"


class TestJsonPointer:
    """Tests of `json_pointer()`."""

    @pytest.mark.parametrize(
        ("segments", "pointer"),
        [
            ((), "#"),
            (("organization", "url"), "#/organization/url"),
            (("items", 0, "name"), "#/items/0/name"),
            (("a/b", "c~d"), "#/a~1b/c~0d"),
            (("a b", "ä", "%", "#"), "#/a%20b/%C3%A4/%25/%23"),
            (("",), "#/"),
            (("$&'()*+,;=:@!",), "#/$&'()*+,;=:@!"),
        ],
    )
    def test_pointer(self, segments: tuple[str | int, ...], pointer: str) -> None:
        """Pointers are escaped as per RFC 6901 and as URI fragments."""
        assert json_pointer(*segments) == pointer


class TestSchemaFieldErrors:
    """Tests of `schema_field_errors()`."""

    @pytest.mark.parametrize(
        ("schema", "instance", "expected"),
        [
            (
                {"type": "object", "required": ["a", "b"]},
                {"c": 1},
                [
                    ("#/a", "required", "Is required."),
                    ("#/b", "required", "Is required."),
                ],
            ),
            (
                {"type": "string"},
                1,
                [("#", "invalid-type", "Must be of type string.")],
            ),
            (
                {"type": ["string", "null"]},
                1,
                [("#", "invalid-type", "Must be of type string or null.")],
            ),
            (
                {"enum": ["a", 1]},
                "b",
                [("#", "invalid-value", 'Must be one of: "a", 1.')],
            ),
            (
                {"enum": [date(2020, 1, 1)]},
                "x",
                [("#", "invalid-value", 'Must be one of: "2020-01-01".')],
            ),
            (
                {"format": "uri"},
                "not a uri",
                [("#", "invalid-format", "Must be a valid URI.")],
            ),
            (
                {"format": "date-time"},
                "x",
                [("#", "invalid-format", "Must be a valid date and time.")],
            ),
            (
                {"format": "date"},
                "x",
                [("#", "invalid-format", "Must be a valid date.")],
            ),
            (
                {"format": "email"},
                "x",
                [("#", "invalid-format", "Must be a valid email address.")],
            ),
            (
                {"format": "ipv4"},
                "x",
                [("#", "invalid-format", "Must be in the format 'ipv4'.")],
            ),
            (
                {"maxLength": 3},
                "abcd",
                [("#", "too-long", "Must be at most 3 characters long.")],
            ),
            (
                {"minLength": 3},
                "ab",
                [("#", "too-short", "Must be at least 3 characters long.")],
            ),
            (
                {"pattern": "^a+$"},
                "b",
                [("#", "pattern-mismatch", "Must match the pattern '^a+$'.")],
            ),
            ({"maximum": 3}, 4, [("#", "too-large", "Must be at most 3.")]),
            (
                {"maximum": 3, "exclusiveMaximum": True},
                3,
                [("#", "too-large", "Must be less than 3.")],
            ),
            ({"minimum": 3}, 2, [("#", "too-small", "Must be at least 3.")]),
            (
                {"minimum": 3, "exclusiveMinimum": True},
                3,
                [("#", "too-small", "Must be greater than 3.")],
            ),
            (
                {"multipleOf": 2},
                3,
                [("#", "not-multiple-of", "Must be a multiple of 2.")],
            ),
            (
                {"maxItems": 1},
                [1, 2],
                [("#", "too-many-items", "Must have at most 1 items.")],
            ),
            (
                {"minItems": 2},
                [1],
                [("#", "too-few-items", "Must have at least 2 items.")],
            ),
            (
                {"uniqueItems": True},
                [1, 1],
                [("#", "duplicate-items", "Must not have duplicate items.")],
            ),
            (
                {"maxProperties": 1},
                {"a": 1, "b": 2},
                [("#", "too-many-properties", "Must have at most 1 properties.")],
            ),
            (
                {"minProperties": 2},
                {"a": 1},
                [("#", "too-few-properties", "Must have at least 2 properties.")],
            ),
            (
                {
                    "properties": {"a": {}},
                    "patternProperties": {"^x-": {}},
                    "additionalProperties": False,
                },
                {"a": 1, "x-b": 2, "c": 3, "d/e": 4},
                [
                    ("#/c", "unknown-property", "Is not allowed."),
                    ("#/d~1e", "unknown-property", "Is not allowed."),
                ],
            ),
            (
                {"dependencies": {"a": ["b"]}},
                {"a": 1},
                [
                    (
                        "#",
                        "missing-dependency",
                        "Must meet the dependencies of its properties.",
                    )
                ],
            ),
            (
                {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                1.5,
                [("#", "no-match", "Must match one of the allowed schemas.")],
            ),
            (
                {"oneOf": [{"type": "number"}, {"type": "integer"}]},
                1,
                [("#", "no-match", "Must match exactly one of the allowed schemas.")],
            ),
            (
                {"not": {"type": "integer"}},
                1,
                [("#", "not-allowed", "Must not match the disallowed schema.")],
            ),
            (
                {"properties": {"id": {"readOnly": True}}},
                {"id": "x"},
                [("#/id", "read-only", "Is read-only.")],
            ),
        ],
    )
    def test_keywords(
        self,
        schema: dict[str, Any],
        instance: Any,
        expected: list[tuple[str, str, str]],
    ) -> None:
        """Each keyword gets a code and a message from the schema."""
        assert [
            (error.pointer, error.code, error.detail)
            for error in _errors(schema, instance)
        ] == expected

    def test_unknown_keyword(self) -> None:
        """Keywords without a message of their own get a generic one."""
        error = ValidationError("message", validator="x-custom", validator_value=1)
        assert schema_field_errors([error]) == [
            FieldError(
                detail="Must meet the 'x-custom' constraint.",
                code="invalid",
                pointer="#",
            )
        ]

    def test_required_read_only(self) -> None:
        """Required read-only properties are skipped, as by Connexion."""
        schema = {
            "type": "object",
            "required": ["id", "name"],
            "properties": {"id": {"readOnly": True}, "name": {}},
        }
        assert [e.pointer for e in _errors(schema, {})] == ["#/name"]

    def test_required_other_message(self) -> None:
        """Required errors with other messages point to all missing properties."""
        error = ValidationError(
            "other", validator="required", validator_value=["a", "b"], instance={}
        )
        assert [e.pointer for e in schema_field_errors([error])] == ["#/a", "#/b"]

    def test_nested(self) -> None:
        """Pointers locate nested fields; all errors are collected."""
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {
                "organization": {
                    "type": "object",
                    "properties": {"url": {"type": "string", "format": "uri"}},
                },
                "tags": {"type": "array", "items": {"maxLength": 2}},
            },
        }
        instance = {
            "organization": {"url": "not a uri"},
            "tags": ["ok", "long", "longer"],
        }
        assert [(e.pointer, e.code) for e in _errors(schema, instance)] == [
            ("#/name", "required"),
            ("#/organization/url", "invalid-format"),
            ("#/tags/1", "too-long"),
            ("#/tags/2", "too-long"),
        ]

    def test_parameter(self) -> None:
        """Errors of parameters name the parameter instead of a pointer."""
        errors = _errors(
            {"maxLength": 2}, "abc", parameter="q", validator=Draft4Validator
        )
        assert errors == [
            FieldError(
                detail="Must be at most 2 characters long.",
                code="too-long",
                parameter="q",
            )
        ]

    def test_duplicates(self) -> None:
        """Duplicate entries are dropped."""
        error = ValidationError("x", validator="maxLength", validator_value=1)
        assert len(schema_field_errors([error, error])) == 1

    def test_limit(self) -> None:
        """At most `MAX_FIELD_ERRORS` entries are collected."""
        schema = {"type": "array", "items": {"type": "string"}}
        errors = _errors(schema, list(range(MAX_FIELD_ERRORS + 10)))
        assert len(errors) == MAX_FIELD_ERRORS

    @pytest.mark.parametrize(
        ("schema", "instance"),
        [
            ({"properties": {"url": {"format": "uri"}}}, {"url": f"{LEAKY_URL} x"}),
            ({"properties": {"url": {"maxLength": 3}}}, {"url": LEAKY_URL}),
            ({"properties": {"url": {"pattern": "^a$"}}}, {"url": LEAKY_URL}),
            ({"properties": {"url": {"enum": ["a"]}}}, {"url": LEAKY_URL}),
            ({"properties": {"url": {"type": "integer"}}}, {"url": LEAKY_URL}),
            ({"properties": {"url": {"not": {}}}}, {"url": LEAKY_URL}),
            ({"additionalProperties": False}, {"url": LEAKY_URL}),
            ({"type": "array", "uniqueItems": True}, [LEAKY_URL, LEAKY_URL]),
            ({"properties": {"url": {"readOnly": True}}}, {"url": LEAKY_URL}),
        ],
    )
    def test_values_not_echoed(self, schema: dict[str, Any], instance: Any) -> None:
        """Messages never quote the rejected values, which may hold secrets."""
        errors = _errors(schema, instance)
        assert errors
        assert "s3cr3t" not in json.dumps([error.to_dict() for error in errors])

    def test_value_not_echoed_for_unknown_keyword(self) -> None:
        """Validators' messages aren't used, also for unknown keywords."""
        error = ValidationError(LEAKY_URL, validator="x-custom", instance=LEAKY_URL)
        assert "s3cr3t" not in str(schema_field_errors([error]))

    def test_draft4(self) -> None:
        """Errors of jsonschema's own validators are converted, too."""
        errors = _errors({"required": ["a"]}, {}, validator=Draft4Validator)
        assert [e.pointer for e in errors] == ["#/a"]


class TestUnknownParameters:
    """Tests of `unknown_parameters()`."""

    def test_unknown(self) -> None:
        """Unknown parameters get entries, sorted by name."""
        assert unknown_parameters(["b", "a"]) == [
            FieldError(
                detail="Is not allowed.", code="unknown-parameter", parameter="a"
            ),
            FieldError(
                detail="Is not allowed.", code="unknown-parameter", parameter="b"
            ),
        ]


class TestAddFieldErrors:
    """Tests of `add_field_errors()`."""

    def test_added(self) -> None:
        """Entries, type and code are added; other members stay."""
        problem = {"title": "Bad Request", "status": 400, "detail": "x"}
        error = FieldError(detail="Is required.", code="required", pointer="#/a")
        add_field_errors(problem, [error], max_length=500)
        assert problem == {
            "title": "Bad Request",
            "status": 400,
            "detail": "x",
            "type": INVALID_REQUEST_TYPE,
            "code": INVALID_REQUEST_CODE,
            "errors": [error.to_dict()],
        }

    def test_mapped_members_win(self) -> None:
        """Mapped `type` and `code` are kept."""
        problem = {"title": "T", "status": 400, "type": "about:blank", "code": "x"}
        error = FieldError(detail="d", code="c", pointer="#")
        add_field_errors(problem, [error], max_length=500)
        assert problem["type"] == "about:blank"
        assert problem["code"] == "x"
        assert problem["errors"] == [error.to_dict()]

    def test_none(self) -> None:
        """Without entries, nothing is added."""
        problem = {"title": "T", "status": 400}
        add_field_errors(problem, [], max_length=500)
        assert problem == {"title": "T", "status": 400}

    def test_limit(self) -> None:
        """At most `MAX_FIELD_ERRORS` entries are added, shortened."""
        errors = [
            FieldError(detail="d" * 20, code="c", pointer=json_pointer(i))
            for i in range(MAX_FIELD_ERRORS + 1)
        ]
        problem: dict[str, Any] = {}
        add_field_errors(problem, errors, max_length=10)
        assert len(problem["errors"]) == MAX_FIELD_ERRORS
        assert len(problem["errors"][0]["detail"]) == 10


def test_describe_field_errors() -> None:
    """Descriptions have locations and codes only."""
    errors = [
        FieldError(detail="secret", code="required", pointer="#/name"),
        FieldError(detail="secret", code="too-large", parameter="limit"),
    ]
    assert describe_field_errors(errors) == "#/name (required); limit (too-large)"
