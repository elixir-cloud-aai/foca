"""Miscellaneous utility functions."""

from random import choice
import string


def generate_id(
    charset: str = ''.join([string.ascii_letters, string.digits]),
    length: int = 6
) -> str:
    """Generate random string based on allowed set of characters.

    :param charset: A string of allowed characters or an expression evaluating
        to a string of allowed characters, defaults to
        ``''.join([string.ascii_letters, string.digits])``
    :type charset: str, optional
    :param length: Length of returned string, defaults to ``6``
    :type length: int, optional
    :raises TypeError: Raised if ``charset`` cannot be evaluated to a string or
        if ``length`` is not a positive integer
    :return: Random string of specified length and composed of defined set of
        allowed characters
    :rtype: str
    """
    try:
        charset = eval(charset)
    except (NameError, SyntaxError):
        pass
    except Exception as e:
        raise TypeError(f"Could not evaluate 'charset': {charset}") from e
    if not isinstance(charset, str) or charset == "":
        raise TypeError(
            f"Could not evaluate 'charset' to non-empty string: {charset}"
        )
    if not isinstance(length, int) or not length > 0:
        raise TypeError(
            f"Argument to 'length' is not a positive integer: {length}"
        )
    charset = ''.join(sorted(set(charset)))
    return ''.join(choice(charset) for __ in range(length))
