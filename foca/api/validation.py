r"""Stricter checks of requests than those of Connexion 2.

- Unpaired surrogates in JSON bodies (e.g., from the escape `\ud800`) can't be
  encoded for storage, and are refused, wherever they are in a body. As with
  all checks of bodies by Connexion, only for operations whose only media types
  are JSON, with a schema.
- The JSON Schema formats `date-time`, `date` and `uri` are checked, which
  jsonschema only does with optional packages. Importing this module registers
  them on jsonschema's format checker of Draft 4, which Connexion uses, for the
  whole process.
- Query parameters of operations without parameters are refused with
  `strict_validation`, as Connexion only checks them for operations with
  parameters.
- All invalid fields of the body, and all invalid parameters, are collected,
  not only the first (cf. :py:mod:`foca.errors.fields`): rejected requests
  raise :py:class:`foca.errors.InvalidFieldsProblem`, with the message of
  Connexion's validator on the first error as `detail`, as before. Parameters
  are checked before the body, as by Connexion.

To be revisited when moving to Connexion 3 (#188).
"""

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from connexion.decorators.validation import (
    ParameterValidator as ConnexionParameterValidator,
)
from connexion.decorators.validation import (
    RequestBodyValidator,
    TypeValidationError,
    coerce_type,
)
from connexion.resolver import Resolution, Resolver
from flask import request
from jsonschema import Draft4Validator, draft4_format_checker

from foca.errors import (
    ExtraParameterProblem,
    InvalidFieldsProblem,
    RequestValidationProblem,
)
from foca.errors.fields import FieldError, schema_field_errors
from foca.security.resolvers import _normalize_resolver
from foca.utils.service_info import is_date, is_date_time, is_uri

logger = logging.getLogger(__name__)

# The format checker that Connexion uses
for _name, _check in (
    ("date-time", is_date_time),
    ("date", is_date),
    ("uri", is_uri),
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
    """Validator of request bodies that also refuses unpaired surrogates.

    Collects all invalid fields of bodies that don't match their schema.
    """

    def validate_schema(self, data: Any, url: str) -> Any:
        """Validate a body.

        Args:
            data: Body.
            url: URL of the request.

        Returns:
            What Connexion's validator returns.

        Raises:
            foca.errors.RequestValidationProblem: The body has unpaired
                surrogates. Doesn't quote them, as the response couldn't be
                encoded.
            foca.errors.InvalidFieldsProblem: The body doesn't match its
                schema; with all invalid fields.
        """
        if _has_unpaired_surrogates(data):
            raise RequestValidationProblem(
                detail="Request body must not contain unpaired surrogates."
            )
        try:
            return super().validate_schema(data, url)
        except RequestValidationProblem as exc:
            # Validated again for all errors only if the body is invalid; the
            # detail stays Connexion's message on the first error
            errors = _list_fields(
                lambda: schema_field_errors(self.validator.iter_errors(data))
            )
            if not errors:
                raise
            raise InvalidFieldsProblem(errors, detail=exc.detail) from None


def _list_fields(convert: Callable[[], list[FieldError]]) -> list[FieldError]:
    """List invalid fields, without failing the rejection of the request.

    Listing invalid fields only adds to responses: if it fails (e.g., for
    an unexpected schema), the request is rejected as before, without them.

    Args:
        convert: Function listing the invalid fields.

    Returns:
        The invalid fields, or none if listing them failed.
    """
    try:
        return convert()
    except Exception as exc:
        logger.warning(
            "Invalid fields of a rejected request could not be listed: %s",
            type(exc).__name__,
        )
        return []


def _parameter_errors(
    parameter_type: str, value: Any, param: dict[str, Any]
) -> list[FieldError]:
    """Get the invalid fields of an invalid parameter.

    Checks the parameter as Connexion's `ParameterValidator.validate_parameter()`
    does, but collects all errors.

    Args:
        parameter_type: Location of the parameter, e.g., `query`.
        value: Value of the parameter in the request, if any.
        param: Parameter object of the specification.

    Returns:
        Entries naming the parameter; a generic one if no error is found
        (e.g., for files).
    """
    name = param["name"]
    if value is None:
        return [FieldError(detail="Is required.", code="required", parameter=name)]
    schema = param.get("schema", param)
    try:
        converted = coerce_type(param, value, parameter_type, name)
    except TypeValidationError:
        # Only raised for the types of Connexion's `TYPE_MAP`, so `type` is set
        return [
            FieldError(
                detail=f"Must be of type {schema['type']}.",
                code="invalid-type",
                parameter=name,
            )
        ]
    errors: list[FieldError] = []
    if schema.get("type") != "file":
        schema = {key: item for key, item in schema.items() if key != "required"}
        validator = Draft4Validator(schema, format_checker=draft4_format_checker)
        errors = schema_field_errors(validator.iter_errors(converted), parameter=name)
    return errors or [FieldError(detail="Is invalid.", code="invalid", parameter=name)]


class _Reason(str):
    """Reason of Connexion's validator for an invalid parameter.

    Attributes:
        field_errors: Invalid fields of the parameter.
    """

    field_errors: list[FieldError]


class ParameterValidator(ConnexionParameterValidator):
    """Validator of parameters that collects all invalid parameters.

    Checks parameters with Connexion's validator, in its order, but rejects
    requests with all invalid parameters, not only the first.
    """

    def validate_parameter(
        self,
        parameter_type: str,
        value: Any,
        param: dict[str, Any],
        param_name: str | None = None,
    ) -> str | None:
        """Validate a parameter as Connexion does, adding its invalid fields.

        Called by Connexion's `validate_*_parameter()` methods.

        Args:
            parameter_type: Location of the parameter, e.g., `query`.
            value: Value of the parameter in the request, if any.
            param: Parameter object of the specification.
            param_name: Name of the parameter, as for Connexion.

        Returns:
            Connexion's reason, with the invalid fields as `field_errors`, if
            the parameter is invalid, otherwise `None`.
        """
        error = ConnexionParameterValidator.validate_parameter(
            parameter_type, value, param, param_name
        )
        if not error:
            return None
        reason = _Reason(error)
        reason.field_errors = _list_fields(
            lambda: _parameter_errors(parameter_type, value, param)
        )
        return reason

    def __call__(self, function: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a function with the validation of parameters.

        Args:
            function: Function to wrap.

        Returns:
            Function validating the parameters of a request first.
        """

        @wraps(function)
        def wrapper(request: Any) -> Any:
            """Validate the parameters of a request, as Connexion does.

            Args:
                request: Connexion request.

            Returns:
                What the function returns.

            Raises:
                foca.errors.ExtraParameterProblem: The request has unknown
                    parameters, with `strict_validation`.
                foca.errors.InvalidFieldsProblem: Parameters are invalid; with
                    Connexion's message on the first as `detail`.
            """
            if self.strict_validation:
                query = self.validate_query_parameter_list(request)
                formdata = self.validate_formdata_parameter_list(request)
                if formdata or query:
                    raise ExtraParameterProblem(formdata, query)
            checks = [
                *(
                    validate(param, request)
                    for location, validate in (
                        ("query", self.validate_query_parameter),
                        ("path", self.validate_path_parameter),
                        ("header", self.validate_header_parameter),
                        ("cookie", self.validate_cookie_parameter),
                    )
                    for param in self.parameters.get(location, [])
                ),
                *(
                    self.validate_formdata_parameter(param["name"], param, request)
                    for param in self.parameters.get("formData", [])
                ),
            ]
            reasons = [reason for reason in checks if reason]
            if not reasons:
                return function(request)
            errors = [
                error
                for reason in reasons
                for error in getattr(reason, "field_errors", ())
            ]
            if not errors:
                raise RequestValidationProblem(detail=str(reasons[0]))
            raise InvalidFieldsProblem(errors, detail=str(reasons[0]))

        return wrapper


def _api_key_names(operation: Any) -> set[str]:
    """Get the names of query parameters holding API keys of an operation.

    Connexion takes these out of the parameters it checks when it uses them.

    Args:
        operation: Connexion operation.

    Returns:
        Names of the query parameters.
    """
    schemes = operation._security_schemes or {}
    return {
        scheme["name"]
        for requirement in operation.security or []
        for scheme in (schemes.get(name, {}) for name in requirement)
        if scheme.get("type") == "apiKey" and scheme.get("in") == "query"
    }


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
            if not operation.parameters:
                extra = set(request.args) - _api_key_names(operation)
                if extra:
                    raise ExtraParameterProblem([], sorted(extra))
            return function(*args, **kwargs)

        return Resolution(function=_wrapper, operation_id=resolution.operation_id)
