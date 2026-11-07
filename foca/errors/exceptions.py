"""Connexion 2 adapter of problem details: handlers and Flask responses.

The core is :py:mod:`foca.errors.problems`; what it defines is available here
too.
"""

import logging
from json import dumps
from traceback import format_exception
from typing import Any

from connexion import App
from connexion.exceptions import NonConformingResponse, ProblemException
from flask import Response
from werkzeug.datastructures import Headers
from werkzeug.exceptions import HTTPException

from foca.config.access import get_config
from foca.context import _get_operation_id
from foca.errors.classes import (
    AccessDeniedProblem,
    ExtraParameterProblem,
    InvalidFieldsProblem,
    RequestValidationProblem,
)
from foca.errors.fields import (
    FieldError,
    add_field_errors,
    describe_field_errors,
    unknown_parameters,
)
from foca.errors.problems import (
    _RFC_9110_PHRASES,
    DEFAULT_PROBLEM,
    _filter_problem,
    _get_status,
    _resolve_problem,
    _response_validation_error,
    _shorten,
    _validation_reason,
    build_headers,
    build_problem,
)
from foca.models.config import ExceptionConfig, ExceptionLoggingEnum
from foca.utils.logging import escape_log_value

# Get logger instance
logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROBLEM",
    "_RFC_9110_PHRASES",
    "build_problem",
    "problem_response",
    "register_exception_handler",
    "shorten_detail",
]

# Exceptions of Connexion's request validation, whose reasons are served as
# `detail` with `exceptions.validation_details`
_VALIDATION_EXCEPTIONS = (RequestValidationProblem, ExtraParameterProblem)

# `detail` of rejected requests without `exceptions.validation_reasons`
_INVALID_REQUEST = "The request is invalid."

# Logger of Connexion's request validation, which logs the validator's
# message in full and unescaped (validators quote rejected values, e.g., a
# whole request body)
_VALIDATION_LOGGER = "connexion.decorators.validation"

# Members of problem details set by `problem_response()` itself or mapped
# to the exception class (`type`)
_RESERVED_MEMBERS = ("title", "status", "detail", "type")


# Werkzeug classes whose mapping was warned about
_warned_mappings: set[type[BaseException]] = set()


def _warn_werkzeug_mappings(conf: ExceptionConfig) -> None:
    """Warn of exception maps with raw Werkzeug classes, once per class.

    `AccessDeniedProblem` is one of them for now, as a name of `foca.errors`.

    Args:
        conf: Exception handling settings.
    """
    for cls in conf.problems:
        if (
            cls.__module__ == "werkzeug.exceptions"
            and cls is not AccessDeniedProblem
            and cls not in _warned_mappings
        ):
            _warned_mappings.add(cls)
            logger.warning(
                "Exception map uses 'werkzeug.exceptions.%s', which stops working "
                "with the first release candidate on ASGI: use the classes of "
                "'foca.errors' instead.",
                cls.__name__,
            )


def _drop_request_validation_records(record: logging.LogRecord) -> bool:
    """Filter out log records of Connexion's request validation.

    Added to Connexion's validation logger with
    `exceptions.validation_details` (cf. :py:func:`register_exception_handler`),
    as it logs the reasons of rejected requests in full and unescaped (e.g.,
    with the rejected values); the handler logs them shortened and escaped
    instead. Records of response validation (`validator` attribute
    `response`) are left to :py:func:`_drop_response_validation_records`.

    Args:
        record: Log record of Connexion's validation logger.

    Returns:
        Whether the record is not of request validation, i.e., is of response
        validation.
    """
    return getattr(record, "validator", None) == "response"


def _drop_response_validation_records(record: logging.LogRecord) -> bool:
    """Filter out log records of Connexion's response validation.

    Added to Connexion's validation logger (cf.
    :py:func:`register_exception_handler`), as it logs the validator's message
    with the whole response body; the handler logs the operation and the
    validation error instead (cf. :py:func:`_response_validation_handler_json`).

    Args:
        record: Log record of Connexion's validation logger.

    Returns:
        Whether the record is not of response validation.
    """
    return getattr(record, "validator", None) != "response"


def register_exception_handler(
    app: App,
    conf: ExceptionConfig | None = None,
) -> App:
    """Register generic JSON problem handler with Connexion app.

    Registers a handler for responses that fail validation, which logs them
    without their bodies, and filters out the messages on them of Connexion's
    validation logger (`connexion.decorators.validation`; for all apps of the
    process; cf. :py:func:`_response_validation_handler_json`).

    With `conf.validation_details`, also registers a handler for Connexion's
    request validation exceptions (`BadRequestProblem` and
    `ExtraParameterProblem`; cf. :py:func:`_validation_handler_json`) and
    filters out the request validation messages of Connexion's validation
    logger (`connexion.decorators.validation`; for all apps of the process,
    cf. :py:func:`_drop_request_validation_records`).

    Args:
        app: Connexion application instance.
        conf: Exception handling settings. Defaults to
            :py:class:`foca.models.config.ExceptionConfig` defaults.

    Returns:
        Connexion application instance with registered generic JSON problem
        handler.
    """
    _conf = ExceptionConfig() if conf is None else conf
    _warn_werkzeug_mappings(_conf)

    def _handler(exception: Exception) -> Response:
        return _problem_handler_json(exception=exception, conf=_conf)

    app.add_error_handler(Exception, _handler)
    logger.debug("Registered generic JSON problem handler with Connexion app.")

    def _response_validation_handler(exception: NonConformingResponse) -> Response:
        return _response_validation_handler_json(exception=exception, conf=_conf)

    app.add_error_handler(NonConformingResponse, _response_validation_handler)
    # Added once per process (`addFilter()` skips filters already added)
    logging.getLogger(_VALIDATION_LOGGER).addFilter(_drop_response_validation_records)
    if _conf.validation_details:

        def _validation_handler(exception: ProblemException) -> Response:
            return _validation_handler_json(exception=exception, conf=_conf)

        for exception in _VALIDATION_EXCEPTIONS:
            app.add_error_handler(exception, _validation_handler)
        # The handler logs the reason shortened and escaped instead; added
        # once per process (`addFilter()` skips filters already added)
        logging.getLogger(_VALIDATION_LOGGER).addFilter(
            _drop_request_validation_records
        )
        logger.debug("Registered validation problem handler with Connexion app.")
    return app


def shorten_detail(detail: str) -> str:
    """Shorten a detail to the configured maximum length.

    Use it for request-specific details in log messages (e.g., quoting
    values of the request), as responses of FOCA (cf.
    :py:func:`problem_response`) do. Shortened details keep their start and
    end (e.g., the names of fields at the end of a validator's message).

    Args:
        detail: Description of a problem.

    Returns:
        The detail, if it is not longer than `exceptions.max_detail_length`
        of the current FOCA app's config, otherwise its first and last
        characters, joined by "…", with `exceptions.max_detail_length`
        characters in total.

    Raises:
        RuntimeError: No FOCA app has been created.
    """
    return _shorten(detail, get_config().exceptions.max_detail_length)


def problem_response(
    exc: HTTPException, *, title: str | None = None, **extensions: Any
) -> Response:
    """Build an error response with the description of an exception.

    The problem details mapped to an exception class are the same for all
    its exceptions, so controllers can use this function to respond with
    details specific to a request instead, e.g., which identifier conflicts:

    ```python
    return problem_response(Conflict(f"Exists: {service_id}"), serviceId=service_id)
    ```

    Must be called after a FOCA app has been created (cf.
    :py:func:`foca.config.access.get_config`). Like all error responses, the
    response has the configured media type (`exceptions.media_type`), only
    the public members (`exceptions.public_members`; without `detail`, the
    description is not sent) and the headers of the exception (cf.
    :py:func:`_build_headers`, e.g., `WWW-Authenticate`). The exception is
    not logged.

    Args:
        exc: werkzeug HTTP exception with an HTTP error status code (400 to
            599) and a request-specific description.
        title: Title replacing the mapped one, e.g., a title specific to the
            request (cf. :py:func:`foca.plugins.adapter.problem`).
        extensions: Extension members of the problem details (e.g.,
            ``serviceId="..."``); values must be JSON-serializable. With
            `exceptions.public_members`, only listed members are sent.

    Returns:
        Error response with the problem details mapped to the class of the
        exception (cf. :py:func:`build_problem`), with its status code as
        `status`, its description as `detail` (shortened, cf.
        :py:func:`shorten_detail`) and the extension members.

    Raises:
        RuntimeError: No FOCA app has been created.
        ValueError: The exception is not a werkzeug HTTP exception (e.g., a
            Connexion `ProblemException`), has no HTTP error status code or
            no description (e.g., a plain `HTTPException`), or the extension
            members include `status`, `detail` or `type`.
    """
    status = _get_status(exc) if isinstance(exc, HTTPException) else None
    if status is None or exc.description is None:
        raise ValueError(
            "Problem details need an exception with an HTTP error status code "
            f"and a description: {type(exc).__name__}"
        )
    reserved = sorted(set(_RESERVED_MEMBERS) & extensions.keys())
    if reserved:
        raise ValueError(
            f"Extension members must not set {', '.join(reserved)}: "
            "they are set from the exception or its mapping"
        )
    conf = get_config().exceptions
    problem = _resolve_problem(exc=exc, conf=conf)
    problem.update(
        status=status,
        detail=_shorten(exc.description, conf.max_detail_length),
        **extensions,
    )
    if title is not None:
        problem["title"] = title
    return _response(exc=exc, problem=problem, conf=conf)


def _validation_handler_json(
    exception: ProblemException,
    conf: ExceptionConfig,
) -> Response:
    """JSON problem handler for Connexion's request validation exceptions.

    Registered with `conf.validation_details` (cf.
    :py:func:`register_exception_handler`). Logs the invalid fields by
    location and code, if known, otherwise the reason, at `INFO`, shortened
    and escaped (unless disabled via `conf.logging`), instead of the
    exception, and responds like :py:func:`_problem_handler_json`, with the
    reason as `detail` and the invalid fields, if known, as `errors` (cf.
    :py:func:`foca.errors.fields.add_field_errors`). Without
    `conf.validation_reasons`, the reason isn't served, as it may quote
    rejected values: `detail` then names the invalid fields by location and
    code (cf. :py:func:`foca.errors.fields.describe_field_errors`), if they
    are listed and public, otherwise it is the mapped `detail`, if any, or a
    fixed message.

    Args:
        exception: Exception of Connexion's request validation.
        conf: Exception handling settings.

    Returns:
        JSON-formatted error response, with the problem details mapped to
        the exception (cf. :py:func:`build_problem`), the reason as
        `detail` (shortened to `conf.max_detail_length`), if any (with
        `conf.validation_reasons`; otherwise the invalid fields or a fixed
        message, see above),
        and the invalid fields, if any.
    """
    problem = _resolve_problem(exc=exception, conf=conf)
    reason = _validation_reason(exception)
    if reason is not None and conf.validation_reasons:
        problem["detail"] = _shorten(reason, conf.max_detail_length)
    errors: tuple[FieldError, ...]
    try:
        errors = _field_errors(exception)
        add_field_errors(
            problem=problem, errors=errors, max_length=conf.max_detail_length
        )
    except Exception as exc:
        # Only adds to the response, which keeps its status without them
        logger.warning(
            "Invalid fields of a rejected request could not be listed: %s",
            type(exc).__name__,
        )
        errors = ()
    if not conf.validation_reasons:
        # Reasons may quote rejected values; the invalid fields don't
        public = conf.public_members is None or "errors" in conf.public_members
        if errors and public:
            problem["detail"] = _shorten(
                f"Invalid fields: {describe_field_errors(errors)}",
                conf.max_detail_length,
            )
        else:
            problem.setdefault("detail", _INVALID_REQUEST)
    if conf.logging is not ExceptionLoggingEnum.none:
        # Escaped before shortening, so that the logged reason stays within
        # the maximum length; invalid fields are logged without the reason,
        # which may quote the rejected values
        logged: str | None
        if errors:
            logged = describe_field_errors(errors)
        elif reason is not None:
            logged = reason
        else:
            logged = problem.get("detail")
        logger.info(
            "Request rejected: %s",
            _shorten(
                escape_log_value(problem["title"] if logged is None else logged),
                conf.max_detail_length,
            ),
        )
    return _response(exc=exception, problem=problem, conf=conf)


def _field_errors(exception: ProblemException) -> tuple[FieldError, ...]:
    """Get the invalid fields of a request validation exception.

    Args:
        exception: Exception of request validation.

    Returns:
        The invalid fields of an :py:class:`foca.errors.InvalidFieldsProblem`,
        the unknown parameters of an `ExtraParameterProblem`, otherwise none.
    """
    if isinstance(exception, InvalidFieldsProblem):
        return exception.field_errors
    if isinstance(exception, ExtraParameterProblem):
        # Set by FOCA's parameter validator, with the declared parameters
        listed = getattr(exception, "field_errors", None)
        if listed is not None:
            return tuple(listed)
        return tuple(
            unknown_parameters([*exception.extra_query, *exception.extra_formdata])
        )
    return ()


def _response_validation_handler_json(
    exception: NonConformingResponse,
    conf: ExceptionConfig,
) -> Response:
    """JSON problem handler for Connexion's response validation exceptions.

    Logs the operation and the validation error, shortened and escaped
    (unless disabled via `conf.logging`), instead of the exception, as the
    message of the validator, also in the traceback, has the response body.
    Responds like :py:func:`_problem_handler_json`.

    Args:
        exception: Exception of Connexion's response validation.
        conf: Exception handling settings.

    Returns:
        JSON-formatted error response, with the problem details mapped to
        the exception (cf. :py:func:`build_problem`).
    """
    problem = _resolve_problem(exc=exception, conf=conf)
    if conf.logging is not ExceptionLoggingEnum.none:
        message = escape_log_value(
            f"Response of operation '{_get_operation_id()}' does not conform to "
            f"its specification: {_response_validation_error(exception)}"
        )
        logged = f"{dumps(problem)} {_shorten(message, conf.max_detail_length)}"
        logger.error(logged)
    return _response(exc=exception, problem=problem, conf=conf)


def _response(
    exc: BaseException,
    problem: dict[str, Any],
    conf: ExceptionConfig,
) -> Response:
    """Build the error response for an exception.

    Args:
        exc: Exception.
        problem: Problem details, unfiltered.
        conf: Exception handling settings.

    Returns:
        JSON-formatted error response, with the problem details filtered by
        `conf.public_members`, the headers of the exception (cf.
        :py:func:`_build_headers`) and media type `conf.media_type`.
    """
    status = problem["status"]
    return Response(
        response=dumps(_filter_problem(problem=problem, conf=conf)),
        status=status,
        headers=_build_headers(exc=exc, status=status),
        # exactly as configured (`mimetype` adds a charset to `text/*`)
        content_type=conf.media_type,
    )


def _build_headers(exc: BaseException, status: int) -> Headers:
    """Build headers of the error response for an exception.

    Args:
        exc: Exception.
        status: HTTP status code of the error response.

    Returns:
        Headers as in :py:func:`foca.errors.problems.build_headers`.
    """
    return Headers(build_headers(exc=exc, status=status))


def _exc_to_str(
    exc: BaseException,
    delimiter: str = "\\n",
) -> str:
    """Convert exception, including traceback, to string representation.

    Args:
        exc: The exception to convert to a string.
        delimiter: The delimiter used to join different lines of the exception
            stack.

    Returns:
        String representation of exception.
    """
    exc_lines = format_exception(exc.__class__, exc, exc.__traceback__)
    exc_stripped = [e.rstrip("\n") for e in exc_lines]
    exc_split = []
    for item in exc_stripped:
        exc_split.extend(item.splitlines())
    return delimiter.join(exc_split)


def _log_exception(
    exc: BaseException,
    format: ExceptionLoggingEnum = ExceptionLoggingEnum.oneline,
    problem: dict[str, Any] | None = None,
) -> None:
    """Log exception with indicated format.

    Requires a `logging` logger to be set up and configured.

    Args:
        exc: The exception to log.
        format: How to log the exception (cf.
            :py:class:`foca.models.config.ExceptionLoggingEnum`).
        problem: Problem details the exception is mapped to; logged in the
            same record as the exception, if given.
    """
    if format is ExceptionLoggingEnum.none:
        return
    # Escaped in single-line formats, as messages may contain request values
    if format is ExceptionLoggingEnum.oneline:
        exc_str = escape_log_value(_exc_to_str(exc=exc))
    elif format is ExceptionLoggingEnum.minimal:
        exc_str = escape_log_value(f"{type(exc).__name__}: {exc!s}")
    else:
        exc_str = _exc_to_str(exc=exc, delimiter="\n")
    if problem is not None:
        exc_str = f"{dumps(problem)} {exc_str}"
    logger.error(exc_str)


def _problem_handler_json(
    exception: Exception,
    conf: ExceptionConfig,
) -> Response:
    """Generic JSON problem handler.

    Logs the exception with its unfiltered problem details (unless disabled
    via `conf.logging`) and responds with the filtered problem details and
    the headers of the exception (cf. :py:func:`_build_headers`).

    Args:
        exception: Raised exception.
        conf: Exception handling settings.

    Returns:
        JSON-formatted error response, with media type `conf.media_type`.
    """
    problem = _resolve_problem(exc=exception, conf=conf)
    _log_exception(exc=exception, format=conf.logging, problem=problem)
    return _response(exc=exception, problem=problem, conf=conf)
