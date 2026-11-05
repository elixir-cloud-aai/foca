"""Invalid fields of requests, for problem details, without a web framework.

Responses to invalid requests list each invalid field in the extension member
`errors` of their problem details, in the form of the example of RFC 9457
(section 3): a readable `detail`, the location of the field (`pointer`, a JSON
Pointer into the request body as a URI fragment, or `parameter`, the name of
a query, path, header or cookie parameter) and a kebab-case `code`.

Messages are written from the failed keyword of the JSON Schema and its value
in the schema, never from the rejected value, which may hold secrets (e.g.,
credentials in URLs). Locations may have keys of the body and names of
parameters of the request (e.g., of unknown properties and parameters).

Adapters convert the errors of their validators with
:py:func:`schema_field_errors` and add the entries to problem details with
:py:func:`add_field_errors` (cf. :py:mod:`foca.errors.exceptions` and
:py:mod:`foca.api.validation` for Connexion 2).
"""

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from itertools import islice
from typing import Any
from urllib.parse import quote

from jsonschema import ValidationError

from foca.errors.problems import _shorten

__all__ = [
    "INVALID_REQUEST_CODE",
    "INVALID_REQUEST_TYPE",
    "MAX_FIELD_ERRORS",
    "FieldError",
    "add_field_errors",
    "describe_field_errors",
    "json_pointer",
    "schema_field_errors",
    "unknown_parameters",
]

# Problem type of responses with invalid fields (documented there)
INVALID_REQUEST_TYPE = "https://foca.readthedocs.io/en/latest/problems/invalid-request/"

# Top-level `code` of responses with invalid fields
INVALID_REQUEST_CODE = "invalid-request"

# Maximum number of entries of `errors`, so that responses stay small
MAX_FIELD_ERRORS = 100

_CODE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

# Characters of URI fragments (RFC 3986) kept in pointers, besides letters,
# digits and `_.-~`; `/` is escaped as per RFC 6901 before
_FRAGMENT_SAFE = "!$&'()*+,;=:@"

# JSON Pointers as URI fragments: `#`, then segments of fragment characters
# (percent-encoded otherwise), with `~` only in the escapes `~0` and `~1`
_SEGMENT_CHAR = r"(?:[A-Za-z0-9_.\-!$&'()*+,;=:@]|%[0-9A-Fa-f]{2}|~[01])"
_POINTER = re.compile(rf"#(?:/{_SEGMENT_CHAR}*)*")

# Names of formats in messages
_FORMATS = {
    "date-time": "date and time",
    "date": "date",
    "uri": "URI",
    "email": "email address",
}

_UNKNOWN = "Is not allowed."


@dataclass(frozen=True)
class FieldError:
    """Invalid field of a request.

    Args:
        detail: Readable message, without the rejected value.
        code: Stable, kebab-case code, e.g., ``too-long``.
        pointer: JSON Pointer to the field in the request body, as a URI
            fragment (cf. :py:func:`json_pointer`), e.g.,
            ``#/organization/url``.
        parameter: Name of the query, path, header or cookie parameter,
            instead of `pointer`.

    Attributes:
        detail: Readable message.
        code: Stable, kebab-case code.
        pointer: JSON Pointer to the field in the request body.
        parameter: Name of the parameter.

    Raises:
        ValueError: The detail is empty, the code is not kebab-case, or there
            is not exactly one of `pointer` (a JSON Pointer as a URI
            fragment, cf. :py:func:`json_pointer`) and
            `parameter`.

    Examples:
        >>> FieldError(
        ...     detail="Must use HTTPS.",
        ...     code="scheme-not-allowed",
        ...     pointer=json_pointer("organization", "url"),
        ... ).to_dict()
        {'detail': 'Must use HTTPS.', 'pointer': '#/organization/url', 'code': 'scheme-not-allowed'}
    """  # noqa: E501

    detail: str
    code: str
    pointer: str | None = None
    parameter: str | None = None

    def __post_init__(self) -> None:
        """Check the entry."""
        if not self.detail:
            raise ValueError("Field errors need a detail.")
        if not _CODE.fullmatch(self.code):
            raise ValueError(f"Code of field error is not kebab-case: '{self.code}'")
        if (self.pointer is None) is (self.parameter is None):
            raise ValueError("Field errors need either a pointer or a parameter.")
        if self.pointer is not None and not _POINTER.fullmatch(self.pointer):
            raise ValueError(
                "Pointer of field error is not a JSON Pointer as a URI fragment "
                f"(cf. json_pointer()): '{self.pointer}'"
            )

    def to_dict(self, max_length: int | None = None) -> dict[str, str]:
        """Serialize the entry for the member `errors` of problem details.

        Args:
            max_length: Maximum length of the detail, the pointer and the
                parameter, if any: longer details and parameters are shortened
                in the middle, longer pointers replaced by the closest pointer
                to a containing field that fits.

        Returns:
            Entry with `detail`, `pointer` or `parameter`, and `code`.

        Raises:
            ValueError: The maximum length is less than 1.
        """
        if max_length is not None and max_length < 1:
            raise ValueError("Maximum length must be at least 1.")
        data = {"detail": self._fit(self.detail, max_length)}
        if self.pointer is not None:
            pointer = self.pointer
            while max_length is not None and len(pointer) > max_length:
                pointer = pointer.rsplit("/", 1)[0]
            data["pointer"] = pointer
        else:
            data["parameter"] = self._fit(str(self.parameter), max_length)
        data["code"] = self.code
        return data

    @staticmethod
    def _fit(text: str, max_length: int | None) -> str:
        """Shorten a text, if there is a maximum length.

        Args:
            text: Text.
            max_length: Maximum length, or `None` for none.

        Returns:
            The text, shortened in the middle to `max_length`, if given.
        """
        return text if max_length is None else _shorten(text, max_length)


def json_pointer(*segments: str | int) -> str:
    """Build a JSON Pointer as a URI fragment.

    Segments are escaped as per RFC 6901 (``~`` as ``~0``, ``/`` as ``~1``)
    and characters not allowed in URI fragments are percent-encoded (UTF-8).

    Args:
        *segments: Keys and indices of the path to the field, from the root.

    Returns:
        Pointer, e.g., ``#/organization/url``; ``#`` for the whole document.

    Examples:
        >>> json_pointer("services", 0, "url")
        '#/services/0/url'
    """
    escaped = (
        quote(str(segment).replace("~", "~0").replace("/", "~1"), safe=_FRAGMENT_SAFE)
        for segment in segments
    )
    return "#" + "".join(f"/{segment}" for segment in escaped)


def _type_message(error: ValidationError) -> str:
    """Describe the expected type."""
    types = error.validator_value
    names = types if isinstance(types, list) else [types]
    return f"Must be of type {' or '.join(str(name) for name in names)}."


def _enum_message(error: ValidationError) -> str:
    """Describe the allowed values (of the schema)."""
    # `default`: specifications in YAML may have, e.g., dates
    values = ", ".join(
        json.dumps(value, default=str) for value in error.validator_value
    )
    return f"Must be one of: {values}."


def _format_message(error: ValidationError) -> str:
    """Describe the expected format."""
    name = _FORMATS.get(error.validator_value)
    if name is None:
        return f"Must be in the format '{error.validator_value}'."
    return f"Must be a valid {name}."


def _maximum_message(error: ValidationError) -> str:
    """Describe the maximum (Draft 4: `exclusiveMaximum` is a flag)."""
    if error.schema.get("exclusiveMaximum") is True:
        return f"Must be less than {error.validator_value}."
    return f"Must be at most {error.validator_value}."


def _minimum_message(error: ValidationError) -> str:
    """Describe the minimum (Draft 4: `exclusiveMinimum` is a flag)."""
    if error.schema.get("exclusiveMinimum") is True:
        return f"Must be greater than {error.validator_value}."
    return f"Must be at least {error.validator_value}."


def _fixed(message: str) -> Callable[[ValidationError], str]:
    """Message that does not depend on the schema.

    Args:
        message: Message.

    Returns:
        Function returning the message.
    """
    return lambda _error: message


def _bound(template: str) -> Callable[[ValidationError], str]:
    """Message with the value of the keyword in the schema.

    Args:
        template: Message, with ``{}`` for the value.

    Returns:
        Function returning the message for an error.
    """
    return lambda error: template.format(error.validator_value)


# Codes and messages by keyword; `required` and `additionalProperties` point
# to the properties instead (cf. `_entries()`)
_KEYWORDS: dict[str, tuple[str, Callable[[ValidationError], str]]] = {
    "type": ("invalid-type", _type_message),
    "enum": ("invalid-value", _enum_message),
    "format": ("invalid-format", _format_message),
    "maxLength": ("too-long", _bound("Must be at most {} characters long.")),
    "minLength": ("too-short", _bound("Must be at least {} characters long.")),
    "pattern": ("pattern-mismatch", _bound("Must match the pattern '{}'.")),
    "maximum": ("too-large", _maximum_message),
    "minimum": ("too-small", _minimum_message),
    "multipleOf": ("not-multiple-of", _bound("Must be a multiple of {}.")),
    "maxItems": ("too-many-items", _bound("Must have at most {} items.")),
    "minItems": ("too-few-items", _bound("Must have at least {} items.")),
    "uniqueItems": ("duplicate-items", _fixed("Must not have duplicate items.")),
    "maxProperties": (
        "too-many-properties",
        _bound("Must have at most {} properties."),
    ),
    "minProperties": (
        "too-few-properties",
        _bound("Must have at least {} properties."),
    ),
    "dependencies": (
        "missing-dependency",
        _fixed("Must meet the dependencies of its properties."),
    ),
    "anyOf": ("no-match", _fixed("Must match one of the allowed schemas.")),
    "oneOf": ("no-match", _fixed("Must match exactly one of the allowed schemas.")),
    "not": ("not-allowed", _fixed("Must not match the disallowed schema.")),
    "readOnly": ("read-only", _fixed("Is read-only.")),
}


def _missing_properties(error: ValidationError) -> list[str]:
    """Get the missing properties of a `required` error.

    Validators yield an error per missing property, with the message
    ``'<name>' is a required property``; Connexion's skips read-only
    properties. Names are taken from the schema only.

    Args:
        error: Error of the keyword `required`.

    Returns:
        The property of the message, if it is one of the required
        properties, otherwise all required properties missing in the
        instance.
    """
    required = [name for name in error.validator_value if isinstance(name, str)]
    for name in required:
        if error.message == f"{name!r} is a required property":
            return [name]
    instance = error.instance if isinstance(error.instance, dict) else {}
    return [name for name in required if name not in instance]


def _additional_properties(error: ValidationError) -> list[str]:
    """Get the properties of an object that its schema doesn't allow.

    Args:
        error: Error of the keyword `additionalProperties` (with value
            `false`).

    Returns:
        Keys of the instance that are neither properties of the schema nor
        match its pattern properties, sorted.
    """
    instance = error.instance if isinstance(error.instance, dict) else {}
    properties = error.schema.get("properties", {})
    patterns = list(error.schema.get("patternProperties", {}))
    return sorted(
        key
        for key in instance
        if key not in properties
        and not any(re.search(pattern, key) for pattern in patterns)
    )


def _entries(error: ValidationError, parameter: str | None) -> Iterator[FieldError]:
    """Convert a validation error to entries.

    Args:
        error: Validation error.
        parameter: Name of the parameter, if the instance is one.

    Yields:
        Entries of the error.
    """
    path = list(error.absolute_path)

    def entry(detail: str, code: str, *extra: str) -> FieldError:
        if parameter is not None:
            return FieldError(detail=detail, code=code, parameter=parameter)
        return FieldError(detail=detail, code=code, pointer=json_pointer(*path, *extra))

    keyword = str(error.validator)
    if keyword == "required":
        for name in _missing_properties(error):
            yield entry("Is required.", "required", name)
    elif keyword == "additionalProperties":
        for name in _additional_properties(error):
            yield entry(_UNKNOWN, "unknown-property", name)
    elif keyword in _KEYWORDS:
        code, message = _KEYWORDS[keyword]
        yield entry(message(error), code)
    else:
        yield entry(f"Must meet the '{keyword}' constraint.", "invalid")


def schema_field_errors(
    errors: Iterable[ValidationError], *, parameter: str | None = None
) -> list[FieldError]:
    """Convert JSON Schema validation errors to entries of `errors`.

    Messages are written from the failed keyword and its value in the schema
    only, never from the instance (cf. the module's docstring). Each missing
    or unknown property gets an entry of its own.

    Args:
        errors: Validation errors, e.g., of a validator's `iter_errors()`;
            iterated only until `MAX_FIELD_ERRORS` entries are collected.
        parameter: Name of the parameter, if the validated instance is a
            parameter; entries then name it instead of pointing into the body.

    Returns:
        Entries, in the order of the errors, without duplicates; at most
        `MAX_FIELD_ERRORS`.
    """
    entries: dict[FieldError, None] = {}
    for error in errors:
        for item in _entries(error, parameter):
            entries[item] = None
            if len(entries) >= MAX_FIELD_ERRORS:
                return list(entries)
    return list(entries)


def unknown_parameters(names: Iterable[str]) -> list[FieldError]:
    """Build entries for parameters that the specification doesn't define.

    Args:
        names: Names of the unknown parameters, e.g., of the query or form
            data.

    Returns:
        Entries with code ``unknown-parameter``, sorted by name.
    """
    return [
        FieldError(detail=_UNKNOWN, code="unknown-parameter", parameter=name)
        for name in sorted(names)
    ]


def add_field_errors(
    problem: dict[str, Any], errors: Iterable[FieldError], max_length: int
) -> None:
    """Add invalid fields to problem details.

    Sets `errors` and, unless the problem details have them already (e.g.,
    mapped to the exception class), `type` (:py:data:`INVALID_REQUEST_TYPE`)
    and `code` (:py:data:`INVALID_REQUEST_CODE`). Other members are kept.

    Args:
        problem: Problem details, changed in place.
        errors: Entries; nothing is added without any. At most
            `MAX_FIELD_ERRORS` are added.
        max_length: Maximum length of details, pointers and parameters (cf.
            :py:meth:`FieldError.to_dict`).
    """
    entries = [error.to_dict(max_length) for error in islice(errors, MAX_FIELD_ERRORS)]
    if not entries:
        return
    problem.setdefault("type", INVALID_REQUEST_TYPE)
    problem.setdefault("code", INVALID_REQUEST_CODE)
    problem["errors"] = entries


def describe_field_errors(errors: Iterable[FieldError]) -> str:
    """Describe invalid fields by their locations and codes, e.g., for logs.

    Args:
        errors: Entries; at most `MAX_FIELD_ERRORS` are described.

    Returns:
        Locations and codes of the entries, e.g.,
        ``#/name (required); limit (too-large)``, without their details.
    """
    return "; ".join(
        f"{error.pointer or error.parameter} ({error.code})"
        for error in islice(errors, MAX_FIELD_ERRORS)
    )
