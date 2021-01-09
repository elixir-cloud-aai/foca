"""Utility functions for logging."""

import logging
from connexion import request
from functools import wraps
from typing import (Callable, Optional)

logger = logging.getLogger(__name__)


def log_traffic(
    _fn: Optional[Callable] = None,
    log_request: bool = True,
    log_response: bool = True,
    log_level: int = logging.INFO,
) -> Callable:
    """Decorator for logging service requests and responses.

    :param _fn: The function to be decorated, defaults to ``None``
    :type _fn: Optional[Callable], optional
    :param log_request: Whether or not the request should be logged, defaults
        to ``True``
    :type log_request: bool, optional
    :param log_response: Whether or not the response should be logged,
        defaults to ``True``
    :type log_response: bool, optional
    :param log_level: Logging level, cf.
        https://docs.python.org/3/library/logging.html#logging-levels, defaults
        to ``logging.INFO``
    :type log_level: int, optional
    :return: The decorated function
    :rtype: Callable
    """

    def decorator_log_traffic(fn):
        """Logging decorator. Used to facilitate optional decorator arguments.

        :param fn: The function to be decorated
        :type fn: function
        :return: The response returned from the input function
        :rtype: [type]
        """
        @wraps(fn)
        def wrapper(*args, **kwargs):
            """Wrapper for logging decorator.

            :return: Wrapper function
            :rtype: [type]
            """
            req = (
                f"\"{request.environ['REQUEST_METHOD']} "
                f"{request.environ['PATH_INFO']} "
                f"{request.environ['SERVER_PROTOCOL']}\" from "
                f"{request.environ['REMOTE_ADDR']}"
            )
            if log_request:
                logger.log(
                    level=log_level,
                    msg=f"Incoming request: {req}",
                )

            response = fn(*args, **kwargs)
            if log_response:
                logger.log(
                    level=log_level,
                    msg=f"Response to request {req}: {response}",
                )
            return response

        return wrapper

    if _fn is None:
        return decorator_log_traffic
    else:
        return decorator_log_traffic(_fn)
