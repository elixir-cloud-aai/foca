"""Exception classes of apps and FOCA, independent of the web framework.

For now, the classes are subclasses of their Werkzeug counterparts, and the
names of Connexion's problems are aliases of Connexion 2's classes, so that
existing handlers and `except` clauses keep working. Releases on Connexion 3
change what they derive from, not their names or attributes.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    OAuthProblem,
)
from connexion.exceptions import (
    Forbidden as ConnexionForbidden,
)
from werkzeug import exceptions as _werkzeug

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
        **kwargs: Other arguments of the Werkzeug class, e.g., `valid_methods`
            of :py:class:`MethodNotAllowed`.

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
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        **kwargs: Any,
    ) -> None:
        """Create the error."""
        super().__init__(description=description, response=response, **kwargs)
        self.headers: list[tuple[str, str]] = list(
            headers.items() if isinstance(headers, Mapping) else headers or ()
        )

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


class Unauthorized(HTTPError, _werkzeug.Unauthorized):
    """`401 Unauthorized`."""


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
