"""Petstore exceptions."""

from connexion.exceptions import BadRequestProblem
from werkzeug.exceptions import (
    InternalServerError,
    NotFound,
)

exceptions = {
    Exception: {
        "message": "Oops, something unexpected happened.",
        "code": 500,
    },
    BadRequestProblem: {
        "message": "We don't quite understand what it is you are looking for.",
        "code": 400,
    },
    NotFound: {
        "message": "We have never heard of this pet! :-(",
        "code": 404,
    },
    InternalServerError: {
        "message": "We seem to be having a problem here in the petstore.",
        "code": 500,
    },
}
