"""Context of the current request, independent of the web framework."""

import re
import uuid
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from typing import Any

from celery import current_task
from werkzeug.wsgi import ClosingIterator

DEFAULT_REQUEST_ID_HEADER = "X-Request-ID"

# Request IDs taken from clients: can't forge log lines or headers
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")

_request_id: ContextVar[str | None] = ContextVar("foca_request_id", default=None)


def request_id() -> str:
    """Get the identifier of the current request or Celery task, for logs.

    Returns:
        Identifier of the current request (cf. :py:class:`RequestIdMiddleware`),
        otherwise that of the current Celery task, otherwise an empty string.
    """
    value = _request_id.get()
    if value is not None:
        return value
    if current_task:
        return str(current_task.request.id or "")
    return ""


class RequestIdMiddleware:
    """WSGI middleware giving each request an identifier.

    The identifier is available from :py:func:`request_id` until the response
    is closed, including while it's streamed, and sent in a response header.

    Args:
        app: WSGI application.
        header: Name of the request and response header.
        trust_incoming: Whether to take the identifier from the request header,
            if it's valid (at most 128 letters, digits and `._:-`).

    Attributes:
        app: WSGI application.
        header: Name of the request and response header.
        trust_incoming: Whether to take the identifier from the request header.
    """

    def __init__(
        self,
        app: Callable[..., Iterable[bytes]],
        header: str = DEFAULT_REQUEST_ID_HEADER,
        trust_incoming: bool = True,
    ) -> None:
        """Wrap a WSGI application.

        Args:
            app: WSGI application.
            header: Name of the request and response header.
            trust_incoming: Whether to take the identifier from the request
                header, if it's valid.
        """
        self.app = app
        self.header = header
        self.trust_incoming = trust_incoming
        self._environ_key = f"HTTP_{header.upper().replace('-', '_')}"

    def _get_id(self, environ: dict[str, Any]) -> str:
        """Get the identifier of a request.

        Args:
            environ: WSGI environment of the request.

        Returns:
            Identifier from the request header, if trusted and valid, otherwise
            a new random one.
        """
        incoming = environ.get(self._environ_key, "") if self.trust_incoming else ""
        return incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex

    def __call__(
        self, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes]:
        """Handle a request.

        Args:
            environ: WSGI environment of the request.
            start_response: Function starting the response.

        Returns:
            Response body. File wrappers of the server are passed on as they
            are, so that it can send them efficiently; for them, the identifier
            is only available until the body is sent.
        """
        value = self._get_id(environ)

        def _start_response(
            status: str, headers: list[tuple[str, str]], *args: Any
        ) -> Any:
            """Start the response, with the identifier.

            Args:
                status: Status of the response.
                headers: Headers of the response.
                *args: Further arguments of `start_response` (`exc_info`).

            Returns:
                What `start_response` returns.
            """
            name = self.header.lower()
            headers = [item for item in headers if item[0].lower() != name]
            headers.append((self.header, value))
            return start_response(status, headers, *args)

        previous = _request_id.get()

        def _clear() -> None:
            """Forget the identifier, for the request that an app may be called in."""
            _request_id.set(previous)

        _request_id.set(value)
        try:
            body = self.app(environ, _start_response)
            file_wrapper = environ.get("wsgi.file_wrapper")
            if isinstance(file_wrapper, type) and isinstance(body, file_wrapper):
                _clear()
                return body
            return ClosingIterator(body, _clear)
        except BaseException:
            _clear()
            raise
