"""Utility functions for logging."""

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import has_request_context, request

logger = logging.getLogger(__name__)


def escape_log_value(value: object) -> str:
    """Escape non-printable characters of a value for logging.

    Control characters such as line breaks are replaced with their escape
    sequences (e.g., a line feed with backslash and ``n``), so that values
    from requests cannot forge log lines.

    Args:
        value: Value to be logged.

    Returns:
        String representation of `value` with non-printable characters
        escaped.
    """
    return "".join(
        char if char.isprintable() else repr(char)[1:-1] for char in str(value)
    )


def _describe_request() -> str:
    """Describe the current request for logging.

    Returns:
        Method, path and protocol of the current request and the address of
        the client, e.g., ``"GET /pets HTTP/1.1" from 127.0.0.1``, with
        non-printable characters escaped.
    """
    protocol = request.environ.get("SERVER_PROTOCOL", "")
    return escape_log_value(
        f'"{request.method} {request.path} {protocol}" from {request.remote_addr}'
    )


def _get_status(response: object) -> object:
    """Get the status code of a controller's return value.

    Args:
        response: Return value of a controller, e.g., a body, a tuple of
            body, status (code or string, e.g., ``201 CREATED``) and/or
            headers, or a response object.

    Returns:
        Status code, as sent by Flask (default: 200).
    """
    if isinstance(response, tuple):
        status = response[1] if len(response) > 1 else 200
        if isinstance(status, str):
            code = status.split(maxsplit=1)[0] if status.strip() else ""
            return int(code) if code.isdigit() else status
        # Second element may be headers, as in `(body, headers)`
        return status if isinstance(status, int) else 200
    return getattr(response, "status_code", 200)


def _get_body(response: object) -> object:
    """Get the body of a controller's return value, without headers.

    Args:
        response: Return value of a controller.

    Returns:
        Body of the return value; the data of response objects, unless
        streamed.
    """
    if isinstance(response, tuple):
        return response[0] if response else None
    if hasattr(response, "get_data"):
        if getattr(response, "is_streamed", False):
            return "<streamed>"
        return response.get_data(as_text=True)
    return response


def _describe_response(response: object) -> str:
    """Describe a controller's return value for logging, without its body.

    Args:
        response: Return value of a controller, e.g., a body, a tuple of
            body, status and/or headers, or a response object.

    Returns:
        Type of the body (or response object) and status code, e.g.,
        ``dict, status 200``.
    """
    body = response[0] if isinstance(response, tuple) and response else response
    return f"{type(body).__name__}, status {_get_status(response)}"


def log_traffic(
    _fn: Callable[..., Any] | None = None,
    log_request: bool = True,
    log_response: bool = True,
    log_response_body: bool = False,
    log_level: int = logging.INFO,
) -> Callable[..., Any]:
    """Decorator for logging service requests and responses.

    Request method, path, protocol and client address are logged with
    non-printable characters escaped. Responses are logged with their type
    and status code only, as bodies may contain sensitive data.

    Args:
        log_request: Whether or not the request should be logged.
        log_response: Whether or not the response should be logged.
        log_response_body: Whether to also log the response body (escaped).
            Only use if the bodies of the decorated controllers never
            contain secrets or personal data.
        log_level: Logging level, cf.
            https://docs.python.org/3/library/logging.html#logging-levels

    Returns:
        The decorated function.
    """

    def _decorator_log_traffic(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Logging decorator. Used to facilitate optional decorator arguments.

        Args:
            fn: The function to be decorated.

        Returns:
            The response returned from the input function.
        """

        @wraps(fn)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            """Wrapper for logging decorator.

            Args:
                args: positional arguments passed through from `log_traffic`.
                kwargs: keyword arguments passed through from `log_traffic`.

            Returns:
                Wrapper function.
            """
            req = _describe_request()
            if log_request:
                logger.log(log_level, "Incoming request: %s", req)

            response = fn(*args, **kwargs)
            if log_response:
                msg = f"Response to request {req}: {_describe_response(response)}"
                if log_response_body:
                    msg += f": {escape_log_value(_get_body(response))}"
                logger.log(level=log_level, msg=msg)
            return response

        return _wrapper

    if _fn is None:
        return _decorator_log_traffic
    else:
        return _decorator_log_traffic(_fn)


def log_traffic_if(
    enabled: Callable[[], bool],
    log_request: bool = True,
    log_response: bool = True,
    log_response_body: bool = False,
    log_level: int = logging.INFO,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for logging service requests and responses if enabled.

    Like :py:func:`log_traffic`, but whether traffic is logged is decided on
    each request, e.g., from the configuration of the current app. Outside
    of request contexts (e.g., when called in an app context by a CLI
    command), the decorated function is called without logging. The
    decorated function is available as `__wrapped__` of the returned
    function.

    Args:
        enabled: Function returning whether traffic should be logged; called
            without arguments on each request, in the request context (so
            that, e.g., :py:func:`foca.config.access.get_config` works).
        log_request: Whether or not the request should be logged.
        log_response: Whether or not the response should be logged.
        log_response_body: Whether to also log the response body (escaped).
            Only use if the bodies of the decorated controllers never
            contain secrets or personal data.
        log_level: Logging level, cf.
            https://docs.python.org/3/library/logging.html#logging-levels

    Returns:
        Decorator for logging the traffic of the decorated function if
        enabled.
    """

    def _decorator_log_traffic_if(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorate a function for logging its traffic if enabled.

        Args:
            fn: The function to be decorated.

        Returns:
            The decorated function.
        """
        logged = log_traffic(
            fn,
            log_request=log_request,
            log_response=log_response,
            log_response_body=log_response_body,
            log_level=log_level,
        )

        @wraps(fn)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            """Call the function, logging its traffic if enabled.

            Args:
                args: Positional arguments passed to the function.
                kwargs: Keyword arguments passed to the function.

            Returns:
                Return value of the function.
            """
            if has_request_context() and enabled():
                return logged(*args, **kwargs)
            return fn(*args, **kwargs)

        return _wrapper

    return _decorator_log_traffic_if
