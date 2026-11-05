"""Problem details of exceptions, without a web framework.

Works on plain data: exceptions, status codes, headers as lists of pairs, and
problem details as dictionaries. Adapters turn the results into responses (cf.
:py:mod:`foca.errors.exceptions` for Connexion 2).
"""

from copy import deepcopy
from http import HTTPStatus
from typing import Any

from connexion.exceptions import (
    NonConformingResponse,
    NonConformingResponseHeaders,
    ProblemException,
)
from jsonschema import ValidationError
from werkzeug import exceptions as _werkzeug
from werkzeug.datastructures import Headers

from foca.errors.classes import (
    AuthenticationProblem,
    BadRequest,
    Conflict,
    Forbidden,
    HTTPError,
    InternalServerError,
    MethodNotAllowed,
    NotFound,
    PayloadTooLarge,
    ServiceUnavailable,
    Unauthorized,
    UnprocessableContent,
)
from foca.models.config import ExceptionConfig

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

_BASES = (HTTPError, _werkzeug.HTTPException)

# Classes of `foca.errors` for the Werkzeug classes they derive from, so that
# a mapping of either applies to both, e.g., to the `404` of unknown routes
_COUNTERPARTS: dict[type, type] = {
    _werkzeug.HTTPException: HTTPError,
    _werkzeug.BadRequest: BadRequest,
    _werkzeug.Unauthorized: Unauthorized,
    _werkzeug.Forbidden: Forbidden,
    _werkzeug.NotFound: NotFound,
    _werkzeug.MethodNotAllowed: MethodNotAllowed,
    _werkzeug.Conflict: Conflict,
    _werkzeug.RequestEntityTooLarge: PayloadTooLarge,
    _werkzeug.UnprocessableEntity: UnprocessableContent,
    _werkzeug.InternalServerError: InternalServerError,
    _werkzeug.ServiceUnavailable: ServiceUnavailable,
}


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
    # Bases of all HTTP errors last, as they come before Werkzeug's classes in
    # the method resolution order of the classes of `foca.errors`
    keys = [
        key
        for cls in type(exc).__mro__
        for key in (cls, _COUNTERPARTS.get(cls))
        if key in problems and key not in _CATCH_ALL
    ]
    problem = next(
        (problems[key] for key in keys if key not in _BASES),
        next((problems[key] for key in keys), None),
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
       base class. A class of `foca.errors` and the Werkzeug class it derives
       from count as one. The bases `HTTPError` and `HTTPException` come last,
       so listing them doesn't override a more specific entry.
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


def _response_validation_error(exc: NonConformingResponse) -> str:
    """Describe why a response failed validation, without its content.

    The message of the validator quotes the invalid values, up to the whole
    response body, and locations in the body may have keys of the body. So
    body errors are described by what comes from the specification only: the
    path of the failed keyword in the schema and, for `required`, the names
    of the missing properties.

    Args:
        exc: Exception of Connexion's response validation.

    Returns:
        The path of the failed keyword for invalid bodies, the reason of the
        exception for invalid headers (naming the missing ones), otherwise its
        title.
    """
    # Connexion raises the exception while handling the validator's error
    error = exc.__context__
    if isinstance(error, ValidationError):
        path = "/".join(str(item) for item in error.absolute_schema_path)
        message = f"failed at schema path '/{path}'"
        if error.validator == "required" and isinstance(error.instance, dict):
            missing = sorted(set(error.validator_value) - error.instance.keys())
            message = f"{message}: missing {', '.join(missing)}"
        return message
    if isinstance(exc, NonConformingResponseHeaders):
        return str(exc.detail)
    return str(exc.title)


def build_headers(exc: BaseException, status: int) -> list[tuple[str, str]]:
    """Build headers of the error response for an exception.

    Headers are only kept if the response has the status code of the
    exception, as they may not apply to other statuses. Headers are taken
    from HTTP exceptions (e.g., `Allow` for `405 Method Not Allowed`,
    `WWW-Authenticate` for `401 Unauthorized` and `Retry-After` for `503
    Service Unavailable`, if set) and Connexion problem exceptions (attribute
    `headers`), except for headers describing the body (e.g., `Content-Type`
    and `Content-Length`), as error responses have their own body, with the
    configured media type. `401 Unauthorized` responses to Connexion's
    security exceptions without `WWW-Authenticate` (e.g., for a missing token)
    get `WWW-Authenticate: Bearer`, as per RFC 6750.

    Args:
        exc: Exception.
        status: HTTP status code of the error response.

    Returns:
        Names and values of the headers of the error response.
    """
    if status != _get_status(exc):
        return []
    headers: list[tuple[str, str]] = []
    if isinstance(exc, _werkzeug.HTTPException):
        headers.extend(exc.get_headers())
    elif isinstance(exc, ProblemException) and exc.headers:
        headers.extend(Headers(exc.headers).to_wsgi_list())
    body_headers = {name.lower() for name in _BODY_HEADERS}
    headers = [item for item in headers if item[0].lower() not in body_headers]
    if (
        status == 401
        and isinstance(exc, AuthenticationProblem)
        and not any(name.lower() == "www-authenticate" for name, _ in headers)
    ):
        headers.append(("WWW-Authenticate", _BEARER_CHALLENGE))
    return headers
