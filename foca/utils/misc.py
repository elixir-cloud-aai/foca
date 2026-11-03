"""Miscellaneous utility functions."""

import re
import string
from secrets import choice

from pydantic import ValidationError

# Types of errors of pydantic whose messages quote parts of the input
_INPUT_IN_MESSAGE = frozenset({"import_error", "union_tag_invalid"})

# Matches references to constants of the `string` module (e.g.,
# `string.digits`), as used in charset expressions that were evaluated by
# previous versions of `generate_id()`
_STRING_EXPRESSION = re.compile(
    r"\bstring\s*\.\s*(?:"
    + "|".join(
        [
            "ascii_letters",
            "ascii_lowercase",
            "ascii_uppercase",
            "digits",
            "hexdigits",
            "octdigits",
            "printable",
            "punctuation",
            "whitespace",
        ]
    )
    + r")\b"
)


def describe_validation_error(exc: Exception) -> str:
    """Describe an error of validating settings, without the invalid input.

    The message of a :py:class:`pydantic.ValidationError` shows the input
    unless the model sets `hide_input_in_errors`, which models of plugins and
    apps need not do, and a missing field or a mistyped key may sit next to,
    or be, a secret that isn't masked yet. Callers that pass the description
    on should also not chain `exc`, as tracebacks show the messages of
    chained errors.

    Names of keys are shown (e.g., of unknown ones), as they tell what to
    correct, and so are messages of validators written for a model.

    Args:
        exc: Error raised by validating. Others than validation errors (e.g.,
            bugs of validators) are described by their type only.

    Returns:
        Locations and messages of the errors, e.g.,
        `api_key: Field required; limit: Input should be a valid integer`.
    """
    if not isinstance(exc, ValidationError):
        return type(exc).__name__
    parts = []
    for error in exc.errors(
        include_url=False, include_context=False, include_input=False
    ):
        message = (
            f"invalid value ({error['type']})"
            if error["type"] in _INPUT_IN_MESSAGE
            else error["msg"]
        )
        location = ".".join(str(part) for part in error["loc"])
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts)


def generate_id(
    charset: str = "".join([string.ascii_letters, string.digits]),
    length: int = 6,
) -> str:
    """Generate random string composed of specified character set.

    Characters are drawn with :py:func:`secrets.choice`, i.e., they are not
    predictable from previously generated identifiers. Whether identifiers
    are hard enough to guess for a given purpose still depends on `charset`
    and `length`: the defaults yield about 36 bits of entropy, which is fine
    for collision-resistant resource identifiers, but too little for
    secrets such as tokens.

    Args:
        charset: A string of allowed characters. Duplicate characters are
            ignored.
        length: Length of returned string.

    Returns:
        Random string of specified length and composed of specified character
        set.

    Raises:
        TypeError: Raised if `charset` is not a non-empty string or if
            `length` is not a positive integer.
        ValueError: Raised if `charset` looks like an expression using the
            `string` module (e.g., ``string.ascii_uppercase + string.digits``)
            that previous versions evaluated. Pass the characters themselves
            instead (e.g., ``ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789``).
    """
    if not isinstance(charset, str) or charset == "":
        raise TypeError(f"Argument to 'charset' is not a non-empty string: {charset}")
    if _STRING_EXPRESSION.search(charset):
        raise ValueError(
            f"Argument to 'charset' looks like an expression: '{charset}'. "
            "Expressions are no longer evaluated; pass a string of the "
            "allowed characters instead."
        )
    if not isinstance(length, int) or isinstance(length, bool) or not length > 0:
        raise TypeError(f"Argument to 'length' is not a positive integer: {length}")
    charset = "".join(sorted(set(charset)))
    return "".join(choice(charset) for __ in range(length))
