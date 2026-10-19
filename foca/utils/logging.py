"""Utility functions for logging."""

import logging
from collections.abc import Callable
from functools import wraps

from flask import request

logger = logging.getLogger(__name__)


def _describe_request() -> str:
    """Describe the current request for logging.

    Returns:
        Method, path and protocol of the current request and the address of
        the client, e.g., ``"GET /pets HTTP/1.1" from 127.0.0.1``.
    """
    protocol = request.environ.get("SERVER_PROTOCOL", "")
    return (
        f'"{request.method} {request.path} {protocol}" from '
        f"{request.remote_addr}"
    )


def log_traffic(
    _fn: Callable | None = None,
    log_request: bool = True,
    log_response: bool = True,
    log_level: int = logging.INFO,
) -> Callable:
    """Decorator for logging service requests and responses.

    Args:
        log_request: Whether or not the request should be logged.
        log_response: Whether or not the response should be logged.
        log_level: Logging level, cf.
            https://docs.python.org/3/library/logging.html#logging-levels

    Returns:
        The decorated function.
    """

    def _decorator_log_traffic(fn):
        """Logging decorator. Used to facilitate optional decorator arguments.

        Args:
            fn: The function to be decorated.

        Returns:
            The response returned from the input function.
        """

        @wraps(fn)
        def _wrapper(*args, **kwargs):
            """Wrapper for logging decorator.

            Args:
                args: positional arguments passed through from `log_traffic`.
                kwargs: keyword arguments passed through from `log_traffic`.

            Returns:
                Wrapper function.
            """
            req = _describe_request()
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

        return _wrapper

    if _fn is None:
        return _decorator_log_traffic
    else:
        return _decorator_log_traffic(_fn)
