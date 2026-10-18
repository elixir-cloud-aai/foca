"""Petstore exceptions, mapped to problem details (RFC 9457).

Exceptions that are not listed are handled like their closest listed base
class (e.g., `connexion.exceptions.OAuthProblem` like `Unauthorized`), or
else according to their HTTP status code.
"""

from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    Forbidden,
    OAuthProblem,
    Unauthorized,
)
from werkzeug.exceptions import (
    BadRequest,
    InternalServerError,
    NotFound,
)

BAD_REQUEST = {
    "title": "Bad Request",
    "status": 400,
    "detail": "We don't quite understand what it is you are looking for.",
}

exceptions = {
    Exception: {
        "title": "Internal Server Error",
        "status": 500,
        "detail": "Oops, something unexpected happened.",
    },
    BadRequestProblem: BAD_REQUEST,
    BadRequest: BAD_REQUEST,
    ExtraParameterProblem: BAD_REQUEST,
    OAuthProblem: {
        "title": "Unauthorized",
        "status": 401,
        "detail": (
            "I will need to see some identification first, before I let you "
            "play with the pets."
        ),
    },
    Unauthorized: {
        "title": "Unauthorized",
        "status": 401,
        "detail": (
            "We will need to see some identification before we let you play "
            "with the pets."
        ),
    },
    Forbidden: {
        "title": "Forbidden",
        "status": 403,
        "detail": (
            "Sorry, but you don't have permission to play with the pets."
        ),
    },
    NotFound: {
        "title": "Not Found",
        "status": 404,
        "detail": "We have never heard of this pet! :-(",
    },
    InternalServerError: {
        "title": "Internal Server Error",
        "status": 500,
        "detail": "We seem to be having a problem here in the petstore.",
    },
}
