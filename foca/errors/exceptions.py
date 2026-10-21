"""Map exceptions to problem details and register a JSON problem handler."""

import logging
from copy import deepcopy
from http import HTTPStatus
from json import dumps
from traceback import format_exception

from connexion import App
from connexion.exceptions import OAuthProblem, ProblemException
from flask import Response
from werkzeug.datastructures import Headers
from werkzeug.exceptions import HTTPException

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


def register_exception_handler(
    app: App,
    conf: ExceptionConfig | None = None,
) -> App:
    """Register generic JSON problem handler with Connexion app.

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


def _resolve_problem(exc: BaseException, conf: ExceptionConfig) -> dict:
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
) -> tuple[dict, int]:
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
    status = problem["status"]
    if conf.public_members is not None:
        problem = {
            key: value
            for key, value in problem.items()
            if key in conf.public_members
        }
    return problem, status


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
    problem: dict | None = None,
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
    _log_exception(
        exc=exception,
        format=conf.logging,
        problem=_resolve_problem(exc=exception, conf=conf),
    )
    problem, status = build_problem(exc=exception, conf=conf)
    return Response(
        response=dumps(problem),
        status=status,
        headers=_build_headers(exc=exception, status=status),
        # exactly as configured (`mimetype` adds a charset to `text/*`)
        content_type=conf.media_type,
    )
