"""Custom errors, error handler functions and function to register error
handlers with a Connexion app instance."""

import logging

from connexion import App
from connexion.exceptions import (
    ExtraParameterProblem,
    Forbidden,
    Unauthorized
)
from flask import Response
from json import dumps
from typing import Union
from werkzeug.exceptions import (BadRequest, InternalServerError, NotFound)


# Get logger instance
logger = logging.getLogger(__name__)


def register_error_handlers(app: App) -> App:
    """Adds custom handlers for exceptions to Connexion app instance."""
    # Add error handlers
    app.add_error_handler(BadRequest, handle_bad_request)
    app.add_error_handler(ExtraParameterProblem, handle_bad_request)
    app.add_error_handler(Forbidden, __handle_forbidden)
    app.add_error_handler(InternalServerError, __handle_internal_server_error)
    app.add_error_handler(NotFound, __handle_not_found)
    app.add_error_handler(Unauthorized, __handle_unauthorized)
    logger.info('Registered custom error handlers with Connexion app.')

    # Return Connexion app instance
    return app


# CUSTOM ERROR HANDLERS
def handle_bad_request(exception: Union[Exception, int]) -> Response:
    return Response(
        response=dumps({
            'msg': 'The request is malformed.',
            'status_code': '400'
            }),
        status=400,
        mimetype="application/problem+json"
    )


def __handle_unauthorized(exception: Exception) -> Response:
    return Response(
        response=dumps({
            'msg': 'The request is unauthorized.',
            'status_code': '401'
            }),
        status=401,
        mimetype="application/problem+json"
    )


def __handle_forbidden(exception: Exception) -> Response:
    return Response(
        response=dumps({
            'msg': 'The requester is not authorized to perform this action.',
            'status_code': '403'
            }),
        status=403,
        mimetype="application/problem+json"
    )


def __handle_not_found(exception: Exception) -> Response:
    return Response(
        response=dumps({
            'msg': 'The requested resource was not found.',
            'status_code': '404'
            }),
        status=404,
        mimetype="application/problem+json"
    )


def __handle_internal_server_error(exception: Exception) -> Response:
    return Response(
        response=dumps({
            'msg': 'An unexpected error occurred.',
            'status_code': '500'
            }),
        status=500,
        mimetype="application/problem+json"
    )
