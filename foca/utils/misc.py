"""Miscellaneous utility functions."""

import re
import string
from secrets import choice

# Matches charsets that look like expressions using the `string` module,
# which were evaluated by previous versions of `generate_id()`
_STRING_EXPRESSION = re.compile(r"\bstring\.[a-z_]+")


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
        raise TypeError(
            f"Argument to 'charset' is not a non-empty string: {charset}"
        )
    if _STRING_EXPRESSION.search(charset):
        raise ValueError(
            f"Argument to 'charset' looks like an expression: '{charset}'. "
            "Expressions are no longer evaluated; pass a string of the "
            "allowed characters instead."
        )
    if (
        not isinstance(length, int)
        or isinstance(length, bool)
        or not length > 0
    ):
        raise TypeError(
            f"Argument to 'length' is not a positive integer: {length}"
        )
    charset = "".join(sorted(set(charset)))
    return "".join(choice(charset) for __ in range(length))
