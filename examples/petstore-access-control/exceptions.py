"""Petstore exceptions."""

from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    Forbidden,
    Unauthorized,
    OAuthProblem,
)
from werkzeug.exceptions import (
    BadRequest,
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
    BadRequest: {
        "message": "We don't quite understand what it is you are looking for.",
        "code": 400,
    },
    ExtraParameterProblem: {
        "message": "We don't quite understand what it is you are looking for.",
        "code": 400,
    },
    OAuthProblem: {
        "message": (
            "I will need to see some identification first, before I let you "
            "play with the pets."
        ),
        "code": 401,
    },
    Unauthorized: {
        "message": (
            "We will need to see some identification before we let you play "
            "with the pets."
        ),
        "code": 401,
    },
    Forbidden: {
        "message": (
            "Sorry, but you don't have permission to play with the pets."
        ),
        "code": 403,
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
