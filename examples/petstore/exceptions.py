"""Petstore exceptions, mapped to problem details (RFC 9457)."""

from connexion.exceptions import BadRequestProblem
from werkzeug.exceptions import (
    InternalServerError,
    NotFound,
)

exceptions = {
    Exception: {
        "title": "Internal Server Error",
        "status": 500,
        "detail": "Oops, something unexpected happened.",
    },
    BadRequestProblem: {
        "title": "Bad Request",
        "status": 400,
        "detail": "We don't quite understand what it is you are looking for.",
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
