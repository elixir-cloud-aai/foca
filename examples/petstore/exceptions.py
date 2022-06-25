"""Petstore exceptions."""

from connexion.exceptions import BadRequestProblem
from werkzeug.exceptions import (
    InternalServerError,
    NotFound,
)

exceptions = {
    Exception: {
        "message": "An unexpected error occurred.",
        "code": 500,
    },
    BadRequestProblem: {
        "message": "The request is malformed.",
        "code": 400,
    },
    NotFound: {
        "message": "The requested resource wasn't found.",
        "code": 404,
    },
    InternalServerError: {
        "message": "An unexpected error occurred.",
        "code": 500,
    },
}
