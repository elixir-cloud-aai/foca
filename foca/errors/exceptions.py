"""Define and register exceptions raised in app context with Connexion app."""

import logging
from typing import Type

from connexion import App
from connexion.exceptions import (
    ExtraParameterProblem,
    Forbidden,
    Unauthorized,
)
from flask import (current_app, Response)
from json import dumps
from werkzeug.exceptions import (
    BadRequest,
    InternalServerError,
    NotFound,
)

# Get logger instance
logger = logging.getLogger(__name__)

# Default exceptions
exceptions = {
    Exception: {
        "title": "An unexpected error occurred.",
        "status": 500,
    },
    InternalServerError: {
        "title": "An unexpected error occurred.",
        "status": 500,
    },
    BadRequest: {
        "title": "The request is malformed.",
        "status": 400,
    },
    ExtraParameterProblem: {
        "title": "The request is malformed.",
        "status": 400,
    },
    Forbidden: {
        "title": "The requester is not authorized to perform this action.",
        "status": 403,
    },
    NotFound: {
        "title": "The requested resource was not found.",
        "status": 404,
    },
    Unauthorized: {
        "title": "The request is unauthorized.",
        "status": 401,
    },
}


def register_exception_handler(app: App) -> App:
    """Register generic JSON problem handlers with Connexion app.

    Args:
        app: Connexion app.

    Returns:
        Connexion app with registered generic JSON problem handler.
    """
    app.add_error_handler(Exception, handle_problem)
    logger.debug('Registered generic JSON problem handler with Connexion app.')
    return app


# Custom exception handlers
def handle_problem(exception: Type[Exception]) -> Response:
    """Generic JSON problem handler.

    Args:
        exception: The raised exception.

    Returns:
        JSON-formatted error response.
    """
    conf = current_app.config['FOCA'].exceptions
    if not exception in conf.mapping:
        logger.warning("############################### HERE!")
        exception = Exception
    status = conf.mapping[exception][conf.status_member]
    if conf.status_member not in conf.required_members:
        del conf.mapping[exception][conf.status_member]
    return Response(
        response=dumps(conf.mapping[exception]),
        status=status,
        mimetype="application/problem+json"
    )