"""Decorator for logging requests and responses."""

import logging
from connexion import request
from functools import wraps
from typing import (Callable, Optional, )

logger = logging.getLogger(__name__)


def log_traffic(_fn: Callable = None,
                log_request: Optional[bool] = True,
                log_response: Optional[bool] = True,
                log_level: Optional[int] = 20,
                ) -> Callable:
    """Customizable decorator for logging requests and responses on
    configurable log level.

    Args:
        _fn: The function to be decorated (gets passed implicitly).
        log_request: A boolean flag to set whether or not the request should
            be logged. Set to 'True' by default.
        log_response: A boolean flag to set whether or not the response should
            be logged. Set to 'True' by default.
        log_level: The numeric values of logging levels. See
            https://docs.python.org/3/library/logging.html#logging-levels

    Returns:
        The decorator function
    """

    def decorator_log_traffic(fn):
        """Logging decorator. Used to facilitate optional decorator arguments.

        Args:
            fn: The function to be decorated.

        Returns:
            The response returned from the input function.
        """
        @wraps(fn)
        def wrapper(*args, **kwargs):
            """Wrapper for logging decorator.

            Args:
                args: positional arguments passed through from `log_traffic`.
                kwargs: keyword arguments passed through from `log_traffic`.

            Returns:
                Wrapper function.
            """
            if log_request:
                logger.log(level=log_level,
                           msg=f"Incoming request: "
                               f"\"{request.environ['REQUEST_METHOD']} "
                               f"{request.environ['PATH_INFO']} "
                               f"{request.environ['SERVER_PROTOCOL']}\" from "
                               f"{request.environ['REMOTE_ADDR']}"
                           )

            response = fn(*args, **kwargs)
            if log_response:
                logger.log(level=log_level,
                           msg=f"Response to request "
                               f"\"{request.environ['REQUEST_METHOD']} "
                               f"{request.environ['PATH_INFO']} "
                               f"{request.environ['SERVER_PROTOCOL']}\" from "
                               f"{request.environ['REMOTE_ADDR']}: "
                               f"{response}"
                           )
            return response

        return wrapper

    if _fn is None:
        return decorator_log_traffic
    else:
        return decorator_log_traffic(_fn)
