"""Exception classes of apps and FOCA, independent of the web framework.

For now, the classes are subclasses of their Werkzeug counterparts, and the
names of Connexion's problems are aliases of Connexion 2's classes, so that
existing handlers and `except` clauses keep working. Releases on Connexion 3
change what they derive from, not their names or attributes.
"""

import warnings
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    OAuthProblem,
)
from connexion.exceptions import (
    Forbidden as ConnexionForbidden,
)
from werkzeug import exceptions as _werkzeug
from werkzeug.datastructures import Headers, WWWAuthenticate

if TYPE_CHECKING:
    # Only for type hints, as `foca.errors.fields` imports this module
    from foca.errors.fields import FieldError

# Stable names of Connexion's problems, for the exception maps of apps
RequestValidationProblem = BadRequestProblem
AuthenticationProblem = OAuthProblem
AccessDeniedProblem = ConnexionForbidden

__all__ = [
    "AccessDeniedProblem",
    "AuthenticationProblem",
    "BadRequest",
    "Conflict",
    "ExtraParameterProblem",
    "Forbidden",
    "HTTPError",
    "InternalServerError",
    "InvalidFieldsProblem",
    "MethodNotAllowed",
    "NotFound",
    "PayloadTooLarge",
    "RequestValidationProblem",
    "ServiceUnavailable",
    "Unauthorized",
    "UnprocessableContent",
]


class HTTPError(_werkzeug.HTTPException):
    """Error with an HTTP status.

    Args:
        description: Description; by default, that of the class.
        response: Response to use instead of problem details, as in Werkzeug.
        headers: Headers of the error response, e.g., `Retry-After`.
        **kwargs: Other arguments of the Werkzeug class, by keyword only, e.g.,
            `valid_methods` of :py:class:`MethodNotAllowed`; give each header
            in one way, as both are sent.

    Attributes:
        code: HTTP status code.
        description: Description.
        headers: Headers of the error response.
    """

    def __init__(
        self,
        description: str | None = None,
        response: Any = None,
        *,
        headers: Mapping[str, Any] | Iterable[tuple[str, str]] | None = None,
        **kwargs: Any,
    ) -> None:
        """Create the error."""
        super().__init__(description=description, response=response, **kwargs)
        self.headers: list[tuple[str, str]] = Headers(headers).to_wsgi_list()

    def get_headers(self, *args: Any, **kwargs: Any) -> list[tuple[str, str]]:
        """Get the headers of the error response.

        Args:
            *args: Arguments of the method of Werkzeug.
            **kwargs: Keyword arguments of the method of Werkzeug.

        Returns:
            Headers of the Werkzeug class and those of `headers`.
        """
        return [*super().get_headers(*args, **kwargs), *self.headers]


class BadRequest(HTTPError, _werkzeug.BadRequest):
    """`400 Bad Request`."""


# Name of the header with the challenges of `401 Unauthorized` (RFC 9110)
_CHALLENGE_HEADER = "WWW-Authenticate"


class Unauthorized(HTTPError, _werkzeug.Unauthorized):
    """`401 Unauthorized`.

    Give challenges as headers, e.g.,
    `Unauthorized(headers={"WWW-Authenticate": "Bearer"})`, and read them
    with :py:meth:`get_headers`. Werkzeug's `www_authenticate` (argument and
    attribute) is deprecated; it reads and writes the challenges of
    `headers`.

    Args:
        description: Description; by default, that of the class.
        response: Response to use instead of problem details, as in Werkzeug.
        headers: Headers of the error response, e.g., `WWW-Authenticate`.
        www_authenticate: Deprecated; challenges, added to `headers`.
        **kwargs: Other arguments of the Werkzeug class, by keyword only.
    """

    def __init__(
        self,
        description: str | None = None,
        response: Any = None,
        *,
        headers: Mapping[str, Any] | Iterable[tuple[str, str]] | None = None,
        www_authenticate: WWWAuthenticate | Iterable[WWWAuthenticate] | None = None,
        **kwargs: Any,
    ) -> None:
        """Create the error."""
        super().__init__(description, response, headers=headers, **kwargs)
        if www_authenticate is not None:
            _warn_www_authenticate()
            self.headers.extend(_challenge_headers(www_authenticate))

    @property
    def www_authenticate(self) -> tuple[WWWAuthenticate, ...] | None:
        """Deprecated: challenges of `headers`; read :py:meth:`get_headers`.

        Returns:
            Challenges of the `WWW-Authenticate` headers that could be parsed,
            or `None` if there are none, as in Werkzeug.
        """
        warnings.warn(
            "'Unauthorized.www_authenticate' is deprecated; read the "
            "'WWW-Authenticate' headers of 'get_headers()'.",
            DeprecationWarning,
            stacklevel=2,
        )
        challenges = tuple(
            challenge
            for name, value in self.headers
            if name.lower() == _CHALLENGE_HEADER.lower()
            and (challenge := WWWAuthenticate.from_header(value)) is not None
        )
        return challenges or None

    @www_authenticate.setter
    def www_authenticate(
        self, value: WWWAuthenticate | Iterable[WWWAuthenticate] | None
    ) -> None:
        """Deprecated: replace the challenges of `headers`.

        Args:
            value: Challenges; `None` removes them.
        """
        # Werkzeug's initializer sets the attribute before `headers` exists,
        # always to `None`, as challenges are passed as headers
        if "headers" not in vars(self):
            return
        _warn_www_authenticate()
        self.headers = [
            (name, header)
            for name, header in self.headers
            if name.lower() != _CHALLENGE_HEADER.lower()
        ]
        if value is not None:
            self.headers.extend(_challenge_headers(value))

    def get_headers(self, *args: Any, **kwargs: Any) -> list[tuple[str, str]]:
        """Get the headers of the error response.

        Args:
            *args: Arguments of the method of Werkzeug.
            **kwargs: Keyword arguments of the method of Werkzeug.

        Returns:
            Headers of the Werkzeug base class (without its deprecated
            challenges, which are in `headers`) and those of `headers`.
        """
        return [
            *_werkzeug.HTTPException.get_headers(self, *args, **kwargs),
            *self.headers,
        ]


def _warn_www_authenticate() -> None:
    """Warn that `www_authenticate` of `Unauthorized` is deprecated."""
    warnings.warn(
        "'www_authenticate' of 'Unauthorized' is deprecated; pass challenges "
        "as headers, e.g., headers={'WWW-Authenticate': 'Bearer'}.",
        DeprecationWarning,
        stacklevel=3,
    )


def _challenge_headers(
    challenges: WWWAuthenticate | Iterable[WWWAuthenticate],
) -> list[tuple[str, str]]:
    """Build `WWW-Authenticate` headers of challenges.

    Args:
        challenges: Challenge or challenges.

    Returns:
        A header per challenge, as in Werkzeug.
    """
    if isinstance(challenges, WWWAuthenticate):
        challenges = (challenges,)
    return [(_CHALLENGE_HEADER, str(challenge)) for challenge in challenges]


class Forbidden(HTTPError, _werkzeug.Forbidden):
    """`403 Forbidden`."""


class NotFound(HTTPError, _werkzeug.NotFound):
    """`404 Not Found`."""


class MethodNotAllowed(HTTPError, _werkzeug.MethodNotAllowed):
    """`405 Method Not Allowed`."""


class Conflict(HTTPError, _werkzeug.Conflict):
    """`409 Conflict`."""


class PayloadTooLarge(HTTPError, _werkzeug.RequestEntityTooLarge):
    """`413 Content Too Large`."""


class UnprocessableContent(HTTPError, _werkzeug.UnprocessableEntity):
    """`422 Unprocessable Content`."""


class InternalServerError(HTTPError, _werkzeug.InternalServerError):
    """`500 Internal Server Error`."""


class ServiceUnavailable(HTTPError, _werkzeug.ServiceUnavailable):
    """`503 Service Unavailable`."""


class InvalidFieldsProblem(BadRequestProblem):
    """`400 Bad Request` for invalid fields of a request.

    Raise it for checks of your own, e.g., of URLs, so that clients get the
    invalid fields in the member `errors` of the problem details, like for
    requests that fail FOCA's schema validation. It is a
    `RequestValidationProblem`, so it is mapped and handled as one (cf.
    `exceptions.validation_details`).

    Args:
        errors: Invalid fields (at least one).
        detail: Description, served as `detail`; by default, the locations
            and details of the invalid fields.

    Attributes:
        field_errors: Invalid fields.

    Raises:
        ValueError: No invalid field is given.

    Examples:
        >>> from foca.errors.fields import FieldError, json_pointer
        >>> exc = InvalidFieldsProblem(
        ...     [
        ...         FieldError(
        ...             detail="Must use HTTPS.",
        ...             code="scheme-not-allowed",
        ...             pointer=json_pointer("organization", "url"),
        ...         )
        ...     ]
        ... )
        >>> exc.detail
        "'#/organization/url': Must use HTTPS."
    """

    def __init__(
        self, errors: Iterable["FieldError"], detail: str | None = None
    ) -> None:
        """Create the problem."""
        self.field_errors = tuple(errors)
        if not self.field_errors:
            raise ValueError("InvalidFieldsProblem needs at least one invalid field.")
        if detail is None:
            detail = "; ".join(
                f"'{error.pointer or error.parameter}': {error.detail}"
                for error in self.field_errors
            )
        super().__init__(detail=detail)
