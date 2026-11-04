r"""Stricter checks of requests than those of Connexion 2.

- Unpaired surrogates in JSON bodies (e.g., from the escape `\ud800`) can't be
  encoded for storage, and are refused, wherever they are in a body.
- The JSON Schema formats `date-time`, `date` and `uri` are checked, which
  jsonschema only does with optional packages. As Connexion uses jsonschema's
  format checker of Draft 4, this applies to all its validators.
- Query parameters of operations without parameters are refused with
  `strict_validation`, as Connexion only checks them for operations with
  parameters.

To be revisited when moving to Connexion 3 (#188).
"""

from collections.abc import Callable
from functools import wraps
from typing import Any

from connexion.decorators.validation import RequestBodyValidator
from connexion.exceptions import BadRequestProblem, ExtraParameterProblem
from connexion.resolver import Resolution, Resolver
from flask import request
from jsonschema import Draft4Validator

from foca.security.resolvers import _normalize_resolver
from foca.utils.service_info import _is_date_time, _is_uri, is_full_date


def _is_date(value: object) -> bool:
    """Check whether a value is an RFC 3339 full date (non-strings pass).

    Args:
        value: Value.

    Returns:
        Whether the value is a valid date, or isn't a string.
    """
    return not isinstance(value, str) or is_full_date(value)


# The format checker that Connexion uses
for _name, _check in (
    ("date-time", _is_date_time),
    ("date", _is_date),
    ("uri", _is_uri),
):
    Draft4Validator.FORMAT_CHECKER.checks(_name)(_check)


def _has_unpaired_surrogates(data: object) -> bool:
    """Check a value of a JSON document for unpaired surrogates.

    Without recursion, as bodies can be nested deeply.

    Args:
        data: Value: strings, as well as those in lists and in the keys and
            values of dictionaries, are checked.

    Returns:
        Whether there are any.
    """
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


class BodyValidator(RequestBodyValidator):
    """Validator of request bodies that also refuses unpaired surrogates."""

    def validate_schema(self, data: Any, url: str) -> Any:
        """Validate a body.

        Args:
            data: Body.
            url: URL of the request.

        Returns:
            What Connexion's validator returns.

        Raises:
            connexion.exceptions.BadRequestProblem: The body has unpaired
                surrogates. Doesn't quote them, as the response couldn't be
                encoded.
        """
        if _has_unpaired_surrogates(data):
            raise BadRequestProblem(
                detail="Request body must not contain unpaired surrogates."
            )
        return super().validate_schema(data, url)


class StrictParametersResolver(Resolver):
    """Resolver refusing query parameters of operations without parameters.

    Args:
        resolver: Resolver to wrap, as accepted by Connexion's `add_api()`.
    """

    def __init__(self, resolver: Resolver | Callable[..., Any] | None = None) -> None:
        """Wrap a resolver.

        Args:
            resolver: Resolver to wrap, as accepted by Connexion's `add_api()`.
        """
        self.resolver = _normalize_resolver(resolver)

    def resolve(self, operation: Any) -> Resolution:
        """Resolve an operation.

        Args:
            operation: Connexion operation to resolve.

        Returns:
            Resolution of the operation, whose function refuses query parameters
            with `strict_validation` if the operation has no parameters.
        """
        resolution = self.resolver.resolve(operation)
        if not operation.strict_validation:
            return resolution
        function = resolution.function

        @wraps(function)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            """Refuse query parameters, unless Connexion checks them.

            Args:
                *args: Arguments of the function.
                **kwargs: Keyword arguments of the function.

            Returns:
                What the function returns.

            Raises:
                connexion.exceptions.ExtraParameterProblem: The request has query
                    parameters, but the operation has none.
            """
            # Operations only know their parameters after being resolved
            if request.args and not operation.parameters:
                raise ExtraParameterProblem([], list(request.args))
            return function(*args, **kwargs)

        return Resolution(function=_wrapper, operation_id=resolution.operation_id)
