"""Map exceptions to problem details and register a JSON problem handler."""

import logging
from copy import deepcopy
from http import HTTPStatus
from json import dumps
from traceback import format_exception
from typing import Any

from connexion import App
from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    OAuthProblem,
    ProblemException,
)
from flask import Response
from werkzeug.datastructures import Headers
from werkzeug.exceptions import HTTPException

from foca.config.access import get_config
from foca.models.config import ExceptionConfig, ExceptionLoggingEnum
from foca.utils.logging import escape_log_value

# Get logger instance
logger = logging.getLogger(__name__)

# Problem details for exceptions without status, as per RFC 9457
DEFAULT_PROBLEM = {
    "title": HTTPStatus.INTERNAL_SERVER_ERROR.phrase,
    "status": HTTPStatus.INTERNAL_SERVER_ERROR.value,
}

# RFC 9110 reason phrases of the statuses whose `HTTPStatus` phrases differ
# before Python 3.13, so that titles do not depend on the Python version
_RFC_9110_PHRASES = {
    413: "Content Too Large",
    414: "URI Too Long",
    416: "Range Not Satisfiable",
    422: "Unprocessable Content",
}

# Exception attributes holding HTTP status codes, e.g., of werkzeug
# (`code`), Connexion (`status`) and Starlette (`status_code`) exceptions
_STATUS_ATTRIBUTES = ("code", "status", "status_code")

# Catch-all exception classes, only used if nothing more specific applies
_CATCH_ALL = (Exception, BaseException)

# Challenge of `401 Unauthorized` responses to Connexion's security
# exceptions without one, e.g., for a missing token, as per RFC 6750, §3.1
_BEARER_CHALLENGE = "Bearer"

# Headers describing the body of a response, which do not apply to the
# problem details body of error responses
_BODY_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Encoding",
    "Transfer-Encoding",
)

# Exceptions of Connexion's request validation, whose reasons are served as
# `detail` with `exceptions.validation_details`
_VALIDATION_EXCEPTIONS = (BadRequestProblem, ExtraParameterProblem)

# Logger of Connexion's request validation, which logs the validator's
# message in full and unescaped (validators quote rejected values, e.g., a
# whole request body)
_VALIDATION_LOGGER = "connexion.decorators.validation"

# Members of problem details set by `problem_response()` itself
_RESERVED_MEMBERS = ("title", "status", "detail")


def _keep_response_validation_records(record: logging.LogRecord) -> bool:
    """Filter out log records of Connexion's request validation.

    Added to Connexion's validation logger with
    `exceptions.validation_details` (cf. :py:func:`register_exception_handler`),
    as it logs the reasons of rejected requests in full and unescaped (e.g.,
    with the rejected values); the handler logs them shortened and escaped
    instead. Records of response validation (`validator` attribute
    `response`) are kept, as they log why a response is invalid.

    Args:
        record: Log record of Connexion's validation logger.

    Returns:
        Whether the record is of response validation.
    """
    return getattr(record, "validator", None) == "response"


def register_exception_handler(
    app: App,
    conf: ExceptionConfig | None = None,
) -> App:
    """Register generic JSON problem handler with Connexion app.

    With `conf.validation_details`, also registers a handler for Connexion's
    request validation exceptions (`BadRequestProblem` and
    `ExtraParameterProblem`; cf. :py:func:`_validation_handler_json`) and
    filters out the request validation messages of Connexion's validation
    logger (`connexion.decorators.validation`; for all apps of the process,
    cf. :py:func:`_keep_response_validation_records`).

    Args:
        app: Connexion application instance.
        conf: Exception handling settings. Defaults to
            :py:class:`foca.models.config.ExceptionConfig` defaults.

    Returns:
        Connexion application instance with registered generic JSON problem
        handler.
    """
    _conf = ExceptionConfig() if conf is None else conf

    def _handler(exception: Exception) -> Response:
        return _problem_handler_json(exception=exception, conf=_conf)

    app.add_error_handler(Exception, _handler)
    logger.debug("Registered generic JSON problem handler with Connexion app.")
    if _conf.validation_details:

        def _validation_handler(exception: ProblemException) -> Response:
            return _validation_handler_json(exception=exception, conf=_conf)

        for exception in _VALIDATION_EXCEPTIONS:
            app.add_error_handler(exception, _validation_handler)
        # The handler logs the reason shortened and escaped instead; added
        # once per process (`addFilter()` skips filters already added)
        logging.getLogger(_VALIDATION_LOGGER).addFilter(
            _keep_response_validation_records
        )
        logger.debug("Registered validation problem handler with Connexion app.")
    return app


def _get_status(exc: BaseException) -> int | None:
    """Get HTTP error status code of an exception, if any.

    Args:
        exc: Exception.

    Returns:
        Value of the first attribute of `exc` among `code`, `status` and
        `status_code` that is an HTTP error status code (400 to 599), or
        `None` if there is none.
    """
    for attribute in _STATUS_ATTRIBUTES:
        status = getattr(exc, attribute, None)
        if (
            isinstance(status, int)
            and not isinstance(status, bool)
            and 400 <= status <= 599
        ):
            return status
    return None


def _resolve_problem(exc: BaseException, conf: ExceptionConfig) -> dict[str, Any]:
    """Resolve problem details of an exception (cf. :py:func:`build_problem`).

    Args:
        exc: Exception.
        conf: Exception handling settings.

    Returns:
        Copy of the problem details, unfiltered.
    """
    problems = conf.problems
    problem = next(
        (
            problems[cls]
            for cls in type(exc).__mro__
            if cls in problems and cls not in _CATCH_ALL
        ),
        None,
    )
    if problem is None:
        status = _get_status(exc)
        if status is not None:
            title = _RFC_9110_PHRASES.get(status)
            if title is None:
                try:
                    title = HTTPStatus(status).phrase
                except ValueError:
                    title = "Error"
            problem = {"title": title, "status": status}
    if problem is None:
        problem = next(
            (problems[cls] for cls in _CATCH_ALL if cls in problems),
            DEFAULT_PROBLEM,
        )
    return deepcopy(problem)


def build_problem(
    exc: BaseException,
    conf: ExceptionConfig,
) -> tuple[dict[str, Any], int]:
    """Build problem details for an exception.

    Problem details are resolved as follows:

    1. The problem details of the closest class in the method resolution
       order of the exception that is listed in the configured mapping
       (except for the catch-alls `Exception` and `BaseException`), i.e.,
       exceptions that are not listed are handled like their closest listed
       base class.
    2. Otherwise, if the exception has an HTTP error status code (attribute
       `code`, `status` or `status_code`, e.g., werkzeug and Connexion
       exceptions), problem details with that status and its standard
       reason phrase as title (as per RFC 9110 for `413`, `414`, `416` and
       `422` on all Python versions).
    3. Otherwise, the problem details listed for `Exception` or
       `BaseException`, if any.
    4. Otherwise, `500 Internal Server Error`.

    Args:
        exc: Exception.
        conf: Exception handling settings.

    Returns:
        Problem details, filtered by `conf.public_members`, if set, and HTTP
        status code.
    """
    problem = _resolve_problem(exc=exc, conf=conf)
    return _filter_problem(problem=problem, conf=conf), problem["status"]


def _filter_problem(problem: dict[str, Any], conf: ExceptionConfig) -> dict[str, Any]:
    """Filter problem details by their public members.

    Args:
        problem: Problem details.
        conf: Exception handling settings.

    Returns:
        Problem details with the members of `conf.public_members` only, if
        set, otherwise all.
    """
    if conf.public_members is None:
        return problem
    return {key: value for key, value in problem.items() if key in conf.public_members}


def _shorten(detail: str, max_length: int) -> str:
    """Shorten a detail in the middle, keeping its start and end.

    Args:
        detail: Description of a problem.
        max_length: Maximum length (at least 1).

    Returns:
        The detail, if it is not longer than `max_length`, otherwise its
        first and last characters, joined by "…", with `max_length`
        characters in total (the start gets the extra character, if any).
    """
    if len(detail) <= max_length:
        return detail
    kept = max_length - 1
    tail = kept // 2
    head = kept - tail
    return f"{detail[:head]}…{detail[len(detail) - tail :]}"


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


def problem_response(exc: HTTPException, **extensions: Any) -> Response:
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
            members include `title`, `status` or `detail`.
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
            "they are set from the exception"
        )
    conf = get_config().exceptions
    problem = _resolve_problem(exc=exc, conf=conf)
    problem.update(
        status=status,
        detail=_shorten(exc.description, conf.max_detail_length),
        **extensions,
    )
    return _response(exc=exc, problem=problem, conf=conf)


def _validation_reason(exc: ProblemException) -> str | None:
    """Get the reason of a validation exception.

    Args:
        exc: Exception of Connexion's request validation.

    Returns:
        The `detail` of the exception (items of a list, e.g., for form data,
        joined by "; "), or `None` if it is empty or not a string or list.
    """
    detail = exc.detail
    if isinstance(detail, list):
        detail = "; ".join(str(item) for item in detail)
    if isinstance(detail, str) and detail:
        return detail
    return None


def _validation_handler_json(
    exception: ProblemException,
    conf: ExceptionConfig,
) -> Response:
    """JSON problem handler for Connexion's request validation exceptions.

    Registered with `conf.validation_details` (cf.
    :py:func:`register_exception_handler`). Logs the reason at `INFO`,
    shortened and escaped (unless disabled via `conf.logging`), instead of
    the exception, and responds like :py:func:`_problem_handler_json`, with
    the reason as `detail`.

    Args:
        exception: Exception of Connexion's request validation.
        conf: Exception handling settings.

    Returns:
        JSON-formatted error response, with the problem details mapped to
        the exception (cf. :py:func:`build_problem`) and the reason as
        `detail` (shortened to `conf.max_detail_length`), if any.
    """
    problem = _resolve_problem(exc=exception, conf=conf)
    reason = _validation_reason(exception)
    if reason is not None:
        problem["detail"] = _shorten(reason, conf.max_detail_length)
    if conf.logging is not ExceptionLoggingEnum.none:
        logger.info(
            "Request rejected: %s",
            escape_log_value(problem.get("detail", problem["title"])),
        )
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

    Headers are only kept if the response has the status code of the
    exception, as they may not apply to other statuses. Headers are taken
    from werkzeug HTTP exceptions (e.g., `Allow` for `405 Method Not
    Allowed`, `WWW-Authenticate` for `401 Unauthorized` and `Retry-After`
    for `503 Service Unavailable`, if set) and Connexion problem exceptions
    (attribute `headers`), except for headers describing the body (e.g.,
    `Content-Type` and `Content-Length`), as error responses have their own
    body, with the configured media type. `401 Unauthorized` responses to
    Connexion's security exceptions without `WWW-Authenticate` (e.g., for a
    missing token) get `WWW-Authenticate: Bearer`, as per RFC 6750.

    Args:
        exc: Exception.
        status: HTTP status code of the error response.

    Returns:
        Headers of the error response, without headers describing the
        body.
    """
    headers = Headers()
    if status != _get_status(exc):
        return headers
    if isinstance(exc, HTTPException):
        headers.extend(exc.get_headers())
    elif isinstance(exc, ProblemException) and exc.headers:
        headers.extend(exc.headers)
    for name in _BODY_HEADERS:
        headers.remove(name)
    if (
        status == 401
        and isinstance(exc, OAuthProblem)
        and "WWW-Authenticate" not in headers
    ):
        headers.add("WWW-Authenticate", _BEARER_CHALLENGE)
    return headers


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
