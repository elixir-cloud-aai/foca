"""Petstore exceptions, mapped to problem details (RFC 9457)."""

from foca.errors import InternalServerError, NotFound, RequestValidationProblem

exceptions = {
    Exception: {
        "title": "Internal Server Error",
        "status": 500,
        "detail": "Oops, something unexpected happened.",
    },
    # Responses to requests rejected by Connexion's validation get the invalid
    # fields as `detail` instead (`exceptions.validation_details`, on by
    # default)
    RequestValidationProblem: {
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
