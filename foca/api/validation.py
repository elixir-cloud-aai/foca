r"""Stricter checks of requests than those of Connexion 2.

- Unpaired surrogates in JSON bodies (e.g., from the escape `\ud800`) can't be
  encoded for storage, and are refused, wherever they are in a body. As with
  all checks of bodies by Connexion, only for operations whose only media types
  are JSON, with a schema.
- The JSON Schema formats `date-time`, `date` and `uri` are checked, which
  jsonschema only does with optional packages. Importing this module registers
  them on jsonschema's format checker of Draft 4, which Connexion uses, for the
  whole process.
- Patterns (`pattern`) of bodies and parameters match `$` only at the end of
  values, as in ECMA 262, to which JSON Schema and OpenAPI refer, not also
  before a newline at the end, as in Python (e.g., `^[0-9a-f]{24}$` refuses
  `"<24 hex digits>\n"`). Patterns are Python regular expressions otherwise.
- Query parameters of operations without parameters are refused with
  `strict_validation`, as Connexion only checks them for operations with
  parameters.
- All invalid fields of the body, and all invalid parameters, are collected,
  not only the first (cf. :py:mod:`foca.errors.fields`): rejected requests
  raise :py:class:`foca.errors.InvalidFieldsProblem`, with the message of
  Connexion's validator on the first error as `detail`, as before (served
  with `exceptions.validation_reasons`). The body is checked before the
  parameters, as by Connexion; if it is invalid, the parameters are then
  checked by FOCA only, and their invalid fields are listed after those of
  the body.

To be revisited when moving to Connexion 3 (#188).
"""

import logging
import re
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from functools import lru_cache, wraps
from typing import Any
from weakref import ReferenceType, WeakKeyDictionary, ref

from connexion.decorators.validation import (
    ParameterValidator as ConnexionParameterValidator,
)
from connexion.decorators.validation import (
    RequestBodyValidator,
    TypeValidationError,
    coerce_type,
)
from connexion.json_schema import Draft4RequestValidator
from connexion.resolver import Resolution, Resolver
from connexion.utils import is_null, is_nullable
from jsonschema import Draft4Validator, ValidationError, draft4_format_checker
from jsonschema.validators import extend

from foca.context import current_request
from foca.errors import (
    ExtraParameterProblem,
    InvalidFieldsProblem,
    RequestValidationProblem,
)
from foca.errors.fields import FieldError, schema_field_errors, unknown_parameters
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


@lru_cache(maxsize=1024)
def _end_anchored(pattern: str) -> str:
    r"""Make the anchors `$` of a pattern match only at the end of strings.

    In Python, `$` also matches before a newline at the end of a string, in
    ECMA 262 only at the end. Anchors are replaced by `\Z`; escaped dollar
    signs (`\$`) and those in sets (e.g., `[$]`) or comments (`(?#...)`) are
    kept. Sets are read as by Python, where a `]` first in a set (after an
    optional `^`) is literal. Comments of verbose patterns (`(?x)`) aren't
    recognized, so anchors after a `[` in them are kept (Python syntax only,
    not in ECMA 262).

    Args:
        pattern: Regular expression, e.g., of a JSON Schema `pattern`.

    Returns:
        The regular expression, with the anchors replaced.
    """
    parts: list[str] = []
    in_set = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            # An escape, with the escaped character
            parts.append(pattern[index : index + 2])
            index += 2
            continue
        index += 1
        if in_set:
            in_set = char != "]"
        elif char == "[":
            in_set = True
            start = index
            if pattern.startswith("^", index):
                index += 1
            if pattern.startswith("]", index):
                index += 1
            char += pattern[start:index]
        elif char == "(" and pattern.startswith("?#", index):
            # A comment, which ends at the first `)`, kept as it is
            end = pattern.find(")", index)
            end = len(pattern) if end == -1 else end + 1
            char += pattern[index:end]
            index = end
        elif char == "$":
            char = r"\Z"
        parts.append(char)
    return "".join(parts)


def _pattern(
    validator: Any, pattern: str, instance: Any, schema: dict[str, Any]
) -> Iterator[ValidationError]:
    """Check the JSON Schema keyword `pattern`, with `$` as in ECMA 262.

    As jsonschema's check of `pattern`, with the same message, but `$` only
    matches at the end of the string (cf. :py:func:`_end_anchored`).

    Args:
        validator: Validator checking the instance.
        pattern: Value of the keyword.
        instance: Value checked.
        schema: Schema with the keyword.

    Yields:
        An error, if the instance is a string that doesn't match.
    """
    if validator.is_type(instance, "string") and not re.search(
        _end_anchored(pattern), instance
    ):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


# Validators of the schemas of bodies (Connexion's, by default) and of
# parameters, with FOCA's check of `pattern`
_BodySchemaValidator = extend(Draft4RequestValidator, {"pattern": _pattern})
_ParameterSchemaValidator = extend(Draft4Validator, {"pattern": _pattern})


def _has_end_anchor(schema: object) -> bool:
    """Check a schema for patterns with a `$`, also in nested schemas.

    Without recursion, as schemas can be nested deeply. May find false
    positives (e.g., a `$` in an example), which only cost another check.

    Args:
        schema: Schema, or a value in it.

    Returns:
        Whether a `pattern` with a `$` is found.
    """
    pending = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pattern = value.get("pattern")
            if isinstance(pattern, str) and "$" in pattern:
                return True
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


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


# FOCA's parameter validators, by the functions they return, so that the body
# validator, which Connexion wraps around such a function, can list the
# invalid parameters too (cf. `BodyValidator.__call__()`). Keys and values are
# weak, so that apps can be collected: the functions refer to the validators,
# and the validators, through their API, to the functions.
_parameter_validators: WeakKeyDictionary[
    Callable[..., Any], "ReferenceType[ParameterValidator]"
] = WeakKeyDictionary()


class BodyValidator(RequestBodyValidator):
    """Validator of request bodies that also refuses unpaired surrogates.

    Collects all invalid fields of bodies that don't match their schema, and,
    for operations whose parameters FOCA's `ParameterValidator` checks, those
    of the parameters too. Checks patterns with `$` as in ECMA 262 (cf.
    :py:func:`_pattern`), unless another validator class is passed.
    """

    def __init__(
        self,
        schema: dict[str, Any],
        consumes: list[str],
        api: Any,
        is_null_value_valid: bool = False,
        validator: type[Any] | None = None,
        strict_validation: bool = False,
    ) -> None:
        """Create a validator, as Connexion's.

        The signature of Connexion 2's `RequestBodyValidator`, which is
        untyped (`api` is its API object); to be revisited with Connexion 3
        (#188).

        Args:
            schema: Schema of the body.
            consumes: Media types of the operation.
            api: API of the operation.
            is_null_value_valid: Whether `null` is accepted.
            validator: Validator class for the schema; by default Connexion's,
                with FOCA's check of `pattern`.
            strict_validation: Whether unknown parameters are refused.
        """
        super().__init__(
            schema,
            consumes,
            api,
            is_null_value_valid,
            validator=_BodySchemaValidator if validator is None else validator,
            strict_validation=strict_validation,
        )

    def __call__(self, function: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a function with the validation of the body.

        Args:
            function: Function to wrap; with parameters, the function returned
                by `ParameterValidator`.

        Returns:
            Function validating the body of a request first.
        """
        reference = _parameter_validators.get(function)
        parameters = reference() if reference is not None else None
        if parameters is None:
            return super().__call__(function)
        # Connexion's validation of the body, around a stand-in that does
        # nothing, so that only the body's problems are caught below, not
        # those of the function
        check_body = super().__call__(_body_valid)

        @wraps(function)
        def wrapper(request: Any) -> Any:
            """Validate the body of a request, as Connexion does.

            Args:
                request: Connexion request.

            Returns:
                What the function returns.

            Raises:
                foca.errors.InvalidFieldsProblem: The body doesn't match its
                    schema; with the invalid fields of the body, followed by
                    those of the parameters, if they can be listed.
            """
            try:
                check_body(request)
            except InvalidFieldsProblem as exc:
                # Best effort: the body's problem is raised in any case
                errors = _list_fields(lambda: parameters.field_errors(request))
                if errors:
                    exc.field_errors = (*exc.field_errors, *errors)
                raise
            return function(request)

        return wrapper

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


def _body_valid(request: Any) -> None:
    """Stand in for the function wrapped by Connexion's body validator.

    Args:
        request: Connexion request, with a valid body.
    """


def _list_fields(
    convert: Callable[[], list[FieldError]],
) -> list[FieldError] | None:
    """List invalid fields, without failing the rejection of the request.

    Listing invalid fields only adds to responses: if it fails (e.g., for
    an unexpected schema), the request is rejected as before, without them.

    Args:
        convert: Function listing the invalid fields.

    Returns:
        The invalid fields, or `None` if listing them failed.
    """
    try:
        return convert()
    except Exception as exc:
        logger.warning(
            "Invalid fields of a rejected request could not be listed: %s",
            type(exc).__name__,
        )
        return None


def _parameter_errors(
    parameter_type: str, value: Any, param: dict[str, Any]
) -> list[FieldError]:
    """Get the invalid fields of a parameter, without logging.

    Checks the parameter as Connexion's `ParameterValidator.validate_parameter()`
    does, but collects all errors and logs nothing.

    Args:
        parameter_type: Location of the parameter, e.g., `query`.
        value: Value of the parameter in the request, if any.
        param: Parameter object of the specification.

    Returns:
        Entries naming the parameter, if it is invalid.
    """
    name = param["name"]
    if value is None:
        if not param.get("required"):
            return []
        return [FieldError(detail="Is required.", code="required", parameter=name)]
    if is_nullable(param) and is_null(value):
        return []
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
    if schema.get("type") == "file":
        return []
    return schema_field_errors(_schema_errors(schema, converted), parameter=name)


def _schema_errors(schema: dict[str, Any], converted: Any) -> Iterator[ValidationError]:
    """Check a converted parameter against its schema, as Connexion does.

    Without `required`, which Connexion checks before, and with FOCA's check
    of `pattern` (cf. :py:func:`_pattern`).

    Args:
        schema: Schema of the parameter.
        converted: Value of the parameter, converted to its type.

    Returns:
        The errors.
    """
    schema = {key: item for key, item in schema.items() if key != "required"}
    validator = _ParameterSchemaValidator(schema, format_checker=draft4_format_checker)
    return validator.iter_errors(converted)


def _pattern_reason(
    parameter_type: str, value: Any, param: dict[str, Any]
) -> str | None:
    """Check the patterns of a parameter that Connexion accepted.

    Connexion checks patterns as Python does, where `$` also matches before a
    newline at the end; FOCA checks parameters with a `$` in a pattern again,
    as in ECMA 262 (cf. :py:func:`_pattern`).

    Args:
        parameter_type: Location of the parameter, e.g., `query`.
        value: Value of the parameter in the request, if any.
        param: Parameter object of the specification.

    Returns:
        The validator's message, as Connexion's reason, if a pattern doesn't
        match, otherwise `None`.
    """
    if value is None or (is_nullable(param) and is_null(value)):
        return None
    schema = param.get("schema", param)
    if schema.get("type") == "file" or not _has_end_anchor(schema):
        return None
    # Converted by Connexion already, without error
    converted = coerce_type(param, value, parameter_type, param["name"])
    error = next(iter(_schema_errors(schema, converted)), None)
    return None if error is None else str(error)


class _Reason(str):
    """Reason of Connexion's validator for an invalid parameter.

    Attributes:
        field_errors: Invalid fields of the parameter, or `None` if they
            could not be listed.
    """

    field_errors: list[FieldError] | None


# Whether the parameters of the current request are checked after the first
# invalid one, by FOCA only (cf. `ParameterValidator`)
_after_first_reason: ContextVar[bool] = ContextVar("_after_first_reason", default=False)


class ParameterValidator(ConnexionParameterValidator):
    """Validator of parameters that collects all invalid parameters.

    Checks parameters with Connexion's validator, in its order, until the
    first invalid one, as before. The remaining parameters are then checked
    by FOCA only (cf. :py:func:`_parameter_errors`), which logs nothing and
    never fails the rejection, so that requests are rejected with all invalid
    parameters, not only the first.
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
            Connexion's reason (after the first invalid parameter of the
            request, an empty one), with the invalid fields as
            `field_errors`, if the parameter is invalid, otherwise `None`.
        """
        if _after_first_reason.get():
            errors = _list_fields(
                lambda: _parameter_errors(parameter_type, value, param)
            )
            if errors == []:
                return None
            reason = _Reason()
        else:
            error = ConnexionParameterValidator.validate_parameter(
                parameter_type, value, param, param_name
            ) or _pattern_reason(parameter_type, value, param)
            if not error:
                return None
            reason = _Reason(error)
            errors = _list_fields(
                lambda: (
                    _parameter_errors(parameter_type, value, param)
                    or [
                        FieldError(
                            detail="Is invalid.",
                            code="invalid",
                            parameter=param["name"],
                        )
                    ]
                )
            )
        reason.field_errors = errors
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
                    # Declared parameters are checked by FOCA only, as
                    # Connexion didn't check them for these requests
                    unknown, errors = self._listed_by_foca(request)
                    exc = ExtraParameterProblem(formdata, query)
                    if errors is not None:
                        exc.field_errors = (*unknown, *errors)
                    raise exc
            first, errors = self._check(request)
            if first is None:
                return function(request)
            if not errors:
                raise RequestValidationProblem(detail=first)
            raise InvalidFieldsProblem(errors, detail=first)

        _parameter_validators[wrapper] = ref(self)
        return wrapper

    def field_errors(self, request: Any) -> list[FieldError]:
        """List the invalid parameters of a request, without rejecting it.

        Used for requests with an invalid body (cf. `BodyValidator`), which
        Connexion rejects before checking the parameters. The parameters are
        checked by FOCA only, which logs nothing.

        Args:
            request: Connexion request.

        Returns:
            The unknown parameters (with `strict_validation`), followed by
            the invalid declared ones, if they could all be listed, as for
            `ExtraParameterProblem`.
        """
        unknown, errors = self._listed_by_foca(request)
        return [*unknown, *(errors or [])]

    def _listed_by_foca(
        self, request: Any
    ) -> tuple[list[FieldError], list[FieldError] | None]:
        """List the unknown and invalid declared parameters, by FOCA only.

        Args:
            request: Connexion request.

        Returns:
            The unknown parameters (with `strict_validation`, otherwise none),
            and the invalid declared ones (`None` if they could not be
            listed).
        """
        unknown: list[FieldError] = []
        if self.strict_validation:
            unknown = unknown_parameters(
                [
                    *self.validate_query_parameter_list(request),
                    *self.validate_formdata_parameter_list(request),
                ]
            )
        token = _after_first_reason.set(True)
        try:
            _, errors = self._check(request)
        finally:
            _after_first_reason.reset(token)
        return unknown, errors

    def _check(self, request: Any) -> tuple[str | None, list[FieldError] | None]:
        """Check the declared parameters of a request.

        Connexion checks them until the first invalid one, FOCA the rest (cf.
        `validate_parameter()`).

        Args:
            request: Connexion request.

        Returns:
            Connexion's reason for the first invalid parameter (`None` if all
            are valid), and the invalid fields (`None` if any could not be
            listed).
        """
        checks = [
            *(
                (validate, param)
                for location, validate in (
                    ("query", self.validate_query_parameter),
                    ("path", self.validate_path_parameter),
                    ("header", self.validate_header_parameter),
                    ("cookie", self.validate_cookie_parameter),
                )
                for param in self.parameters.get(location, [])
            ),
            *(
                (self._validate_formdata, param)
                for param in self.parameters.get("formData", [])
            ),
        ]
        first: str | None = None
        errors: list[FieldError] | None = []
        token = None
        try:
            for validate, param in checks:
                reason = validate(param, request)
                if reason is None:
                    continue
                listed = getattr(reason, "field_errors", None)
                # Fields of all parameters, or none (cf. `_list_fields()`)
                errors = None if listed is None or errors is None else errors + listed
                if first is None:
                    first = str(reason)
                    if not _after_first_reason.get():
                        token = _after_first_reason.set(True)
        finally:
            if token is not None:
                _after_first_reason.reset(token)
        return first, errors

    def _validate_formdata(self, param: dict[str, Any], request: Any) -> str | None:
        """Validate a form data parameter (Swagger 2), as Connexion does.

        Args:
            param: Parameter object of the specification.
            request: Connexion request.

        Returns:
            Reason, if the parameter is invalid (cf. `validate_parameter()`).
        """
        return self.validate_formdata_parameter(param["name"], param, request)


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
                extra = set(current_request().query) - _api_key_names(operation)
                if extra:
                    raise ExtraParameterProblem([], sorted(extra))
            return function(*args, **kwargs)

        return Resolution(function=_wrapper, operation_id=resolution.operation_id)
