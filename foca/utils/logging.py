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

    Args:
        _fn (Optional[Callable], optional): Function to be decorated. Defaults
            to None.
        log_request (bool, optional): Whether or not the request should be
            logged. Defaults to True.
        log_response (bool, optional): Whether or not the response should be
            logged. Defaults to True.
        log_level (int, optional): Logging level, cf.
            https://docs.python.org/3/library/logging.html#logging-levels.
            Defaults to logging.INFO.

    Returns:
        Callable: The decorated function.
    """

    def decorator_log_traffic(fn):
        """Logging decorator. Used to facilitate optional decorator arguments.

        Args:
            fn (function): The function to be decorated.

        Returns:
            [type]: The response returned from the input function.
        """
        @wraps(fn)
        def wrapper(*args, **kwargs):
            """Wrapper for logging decorator.

            Args:
                *args: Positional arguments passed through from ``log_traffic``.
                **kwargs: Keyword arguments passed through from ``log_traffic``.

            Returns:
                [type]: Wrapper function.
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
