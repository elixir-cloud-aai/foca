"""Values of the configuration that come from the environment.

Config files can reference environment variables (:py:func:`interpolate`), and FOCA's
own variables with credentials can come from files, e.g., mounted secrets
(:py:func:`get_env`).
"""

import os
import re
from pathlib import Path
from typing import Any, overload

_REFERENCE = re.compile(
    r"\$(?:"
    r"(?P<escaped>\$)"
    r"|\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<operator>:-|:\?)(?P<argument>[^{}]*))?\}"
    r"|(?P<invalid>\{)"
    r")"
)


def _replace_references(value: str, key: str) -> str:
    """Replace the references to environment variables of a string.

    Args:
        value: String.
        key: Key of the string, for messages.

    Returns:
        The string with its references replaced.

    Raises:
        ValueError: A variable without default isn't set, or a reference isn't valid.
    """

    def _replace(match: re.Match[str]) -> str:
        """Get the replacement of a reference.

        Args:
            match: Match of the reference.

        Returns:
            Replacement.

        Raises:
            ValueError: A variable without default isn't set, or a reference isn't
                valid.
        """
        if match["escaped"]:
            return "$"
        if match["invalid"]:
            raise ValueError(
                f"Key '{key}' has an invalid reference to an environment variable: "
                "use ${VAR}, ${VAR:-default} or ${VAR:?message}, not nested, and "
                "write a literal $ as $$"
            )
        name, operator = match["name"], match["operator"]
        variable = os.environ.get(name)
        if operator is None:
            if variable is None:
                raise ValueError(
                    f"Key '{key}' references environment variable '{name}', which "
                    "is not set: set it, give a default (${VAR:-default}), or write "
                    "a literal $ as $$"
                )
            return variable
        # Empty as unset, as in the specification of Compose
        if variable:
            return variable
        if operator == ":?":
            raise ValueError(
                f"Key '{key}' requires environment variable '{name}', which is not "
                f"set or empty: {match['argument']}"
            )
        return match["argument"].replace("$$", "$")

    return _REFERENCE.sub(_replace, value)


def interpolate(data: Any, key: str = "") -> Any:
    """Replace references to environment variables in parsed YAML.

    As in the specification of Compose, strings may have `${VAR}`, `${VAR:-default}`
    (also if empty), `${VAR:?message}` (error if unset or empty) and `$$` for a literal
    `$`. Other uses of `$` stay as they are. Only string values are changed, not keys,
    and values stay strings, also if the value of a variable looks like a number.

    Args:
        data: Parsed YAML.
        key: Key of `data`, for messages.

    Returns:
        A copy of `data` with the references replaced.

    Raises:
        ValueError: A variable without default isn't set, or a reference isn't valid;
            the message names the key, not the values.
    """
    if isinstance(data, str):
        return _replace_references(data, key)
    if isinstance(data, dict):
        return {
            name: interpolate(value, f"{key}.{name}" if key else str(name))
            for name, value in data.items()
        }
    if isinstance(data, list):
        return [interpolate(value, f"{key}[{i}]") for i, value in enumerate(data)]
    return data


def _read_secret_file(name: str, path: str) -> str:
    """Read the value of an environment variable from a file.

    Args:
        name: Name of the variable with the path, for messages.
        path: Path of the file.

    Returns:
        Content of the file, without the final line break.

    Raises:
        ValueError: The file can't be read.
    """
    error = f"File named in environment variable '{name}' could not be read"
    try:
        # Bytes, as reading text would translate line breaks within the value
        return Path(path).read_bytes().decode().removesuffix("\n").removesuffix("\r")
    except OSError as exc:
        raise ValueError(error) from exc
    except UnicodeError:
        # Not chained: the error has a byte of the content
        raise ValueError(error) from None


@overload
def get_env(name: str, default: str) -> str: ...


@overload
def get_env(name: str, default: None = None) -> str | None: ...


def get_env(name: str, default: str | None = None) -> str | None:
    """Get an environment variable that can also come from a file.

    With `<name>_FILE`, the convention of Docker's official images, the value is read
    from the named file, e.g., a mounted secret. Empty variables count as not set when
    checking for both.

    Args:
        name: Name of the variable.
        default: Value if neither variable is set.

    Returns:
        Value of the variable, the content of the file, without the final line break, or
        `default`.

    Raises:
        ValueError: Both `<name>` and `<name>_FILE` are set, or the file can't be read.
    """
    file_name = f"{name}_FILE"
    value = os.environ.get(name)
    path = os.environ.get(file_name)
    if not path:
        return default if value is None else value
    if value:
        raise ValueError(
            f"Environment variables '{name}' and '{file_name}' must not both be set"
        )
    return _read_secret_file(file_name, path)
