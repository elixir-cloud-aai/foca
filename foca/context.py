"""Context of the current request, independent of the web framework."""

import codecs
import re
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, quote

from celery import current_task
from flask import has_request_context, request
from flask.globals import request_ctx

from foca.api.routes import RouteMatch, match

DEFAULT_REQUEST_ID_HEADER = "X-Request-ID"

# Request IDs taken from clients: can't forge log lines or headers
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")

_request_id: ContextVar[str | None] = ContextVar("foca_request_id", default=None)

# Characters of decoded paths kept as they are in URLs (sub-delimiters, `:`,
# `@`, `/`; cf. RFC 3986)
_PATH_SAFE = "/:@!$&'()*+,;=~"
# Characters of raw query strings kept as they are in URLs, also `%` of
# escapes
_QUERY_SAFE = _PATH_SAFE + "?%"
# Percent signs that don't start escapes
_INVALID_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
# Error handler leaving bytes that aren't UTF-8 percent-encoded, as Werkzeug
# does for query parameters
_INVALID_UTF8 = "foca.percent_encode"


def _percent_encode(exc: UnicodeError) -> tuple[str, int]:
    """Percent-encode bytes that can't be decoded.

    Args:
        exc: Error of decoding.

    Returns:
        The bytes, percent-encoded, and where to continue decoding.

    Raises:
        UnicodeError: Raised for errors other than of decoding.
    """
    if not isinstance(exc, UnicodeDecodeError):
        raise exc
    invalid = exc.object[exc.start : exc.end]
    return "".join(f"%{byte:02X}" for byte in invalid), exc.end


codecs.register_error(_INVALID_UTF8, _percent_encode)


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


class _MultiValues(Mapping[str, str]):
    """Read-only fields of a request that may have several values per name.

    As a mapping, names give their first value; :py:meth:`getlist` gives all
    values of a name.

    Args:
        items: Names and values of the fields, in order.
    """

    def __init__(self, items: Iterable[tuple[str, str]]) -> None:
        """Copy fields.

        Args:
            items: Names and values of the fields, in order.
        """
        self._values: dict[str, list[str]] = {}
        for name, value in items:
            self._values.setdefault(self._key(name), []).append(value)

    # Subclasses override it to normalize names, e.g., case-insensitive ones
    @staticmethod
    def _key(name: str) -> str:
        """Get the key of a name.

        Args:
            name: Name of a field.

        Returns:
            The name, as it is.
        """
        return name

    def __getitem__(self, name: str) -> str:
        """Get the first value of a field.

        Args:
            name: Name of the field.

        Returns:
            First value of the field.

        Raises:
            KeyError: Raised if there is no such field.
        """
        if not isinstance(name, str):
            raise KeyError(name)
        return self._values[self._key(name)][0]

    def __iter__(self) -> Iterator[str]:
        """Get the names of the fields.

        Returns:
            Iterator of the names, each once, in the order they first appear.
        """
        return iter(self._values)

    def __len__(self) -> int:
        """Count the fields.

        Returns:
            Number of names, each counted once.
        """
        return len(self._values)

    def getlist(self, name: str) -> list[str]:
        """Get all values of a field.

        Args:
            name: Name of the field.

        Returns:
            Values of the field, in order; empty if there is no such field.
        """
        if not isinstance(name, str):
            return []
        return list(self._values.get(self._key(name), []))

    def __repr__(self) -> str:
        """Represent the fields.

        Returns:
            Representation with the names only, as values may be credentials
            (e.g., `Authorization` or `Cookie` headers, or tokens in query
            parameters).
        """
        return f"{type(self).__name__}({list(self._values)!r})"


class Headers(_MultiValues):
    """Read-only headers of a request or response, with case-insensitive names.

    As a mapping, names are in lower case and give their first value;
    :py:meth:`getlist` gives all values of a name.

    Args:
        items: Names and values of the headers, in order.
    """

    @staticmethod
    def _key(name: str) -> str:
        """Get the key of a header name.

        Args:
            name: Name of a header, in any case.

        Returns:
            The name, in lower case.
        """
        return name.lower()


class QueryParams(_MultiValues):
    """Read-only query parameters of a request, as sent.

    Names are case-sensitive. As a mapping, names give their first value;
    :py:meth:`getlist` gives all values of a name. Names and values are
    decoded as by Werkzeug (`+` as space, blank values kept), bytes that
    aren't UTF-8 are left percent-encoded.

    Args:
        items: Names and values of the parameters, in order.
    """


@dataclass(frozen=True)
class RequestSnapshot:
    """Read-only snapshot of the request being handled.

    Attributes:
        method: HTTP method, in upper case.
        protocol: Protocol of the request, as the server gives it, e.g.,
            `HTTP/1.1`, or empty if unknown.
        path: Request path below the root path of the app, not
            percent-encoded, e.g., `/ga4gh/tes/v1/tasks/123`; access control
            policies and the ownership of resources are keyed by it.
        root_path: Root path of the app (`SCRIPT_NAME`), empty if there is
            none, e.g., `/tes`.
        url: Full URL of the request, with root path and query string,
            percent-encoded: the host as the server gives it, the path
            encoded anew from `root_path` and `path`, the query string as
            sent, with non-ASCII and unsafe characters encoded.
        query: Query parameters of the request, as sent, read-only; for
            building URLs, e.g., links to other pages of a list. Controllers
            take the parameters of their operation from their arguments, as
            Connexion converts and checks them.
        headers: Headers of the request, case-insensitive and read-only.
        content_length: Length of the request body in bytes, as declared, or
            `None` if not declared.
        client_address: Address of the client, as seen by the server (that of
            the last proxy, unless the app trusts proxies' headers), or `None`
            if unknown.
        route: Path template of the operation serving the request, with the
            base path of the API, e.g., `/ga4gh/tes/v1/tasks/{id}`, or `None`
            if no operation serves it (cf. :py:func:`foca.api.routes.match`).
        operation_id: Identifier of that operation, as in the specification
            (`operationId`), or as resolved if the specification has none, or
            `None` if no operation serves the request.
        path_params: Values of the path parameters of the route, as passed to
            controllers, i.e., decoded and converted to their types, under the
            names of the route's rule; empty without a route.

    The representation of a snapshot, e.g., in logs, leaves out the path, URL,
    query and path parameters, and the values of headers, as they may hold
    credentials; the route shows what was requested.
    """

    method: str
    protocol: str
    path: str = field(repr=False)
    root_path: str
    url: str = field(repr=False)
    query: QueryParams = field(repr=False)
    headers: Headers
    content_length: int | None
    client_address: str | None
    route: str | None
    operation_id: str | None
    path_params: Mapping[str, Any] = field(repr=False)


def _get_url() -> str:
    """Build the percent-encoded URL of the current request.

    Returns:
        The URL: the scheme and host as the server gives them, as clients
        choose the host and not all hosts can be IDNA-encoded; the decoded
        root path and path encoded anew, as Werkzeug's URL keeps their `%`;
        the raw query string, with non-ASCII and unsafe characters and `%`
        that don't start escapes encoded.
    """
    prefix = request.host_url.rstrip("/")
    path = quote(request.script_root + request.path, safe=_PATH_SAFE)
    query = _get_query_string()
    return f"{prefix}{path}?{query}" if query else f"{prefix}{path}"


def _get_query_string() -> str:
    """Get the percent-encoded query string of the current request.

    Returns:
        The raw query string, with non-ASCII and unsafe characters and `%`
        that don't start escapes encoded.
    """
    return _INVALID_ESCAPE.sub("%25", quote(request.query_string, safe=_QUERY_SAFE))


def current_request() -> RequestSnapshot:
    """Get a snapshot of the request being handled.

    It means the same whatever the web framework that FOCA builds on, so use
    it rather than the request objects of Flask or Connexion, and take path,
    query and body parameters from the arguments that Connexion passes to
    controllers.

    Returns:
        Snapshot of the current request; it doesn't change with the request.
        Each call takes a new one, matching the route again.

    Raises:
        RuntimeError: Raised if no request is being handled, e.g., on startup
            or in Celery tasks.
    """
    if not _has_request():
        raise RuntimeError(
            "No request: current_request() works only while a request is handled"
        )
    found = _match_request()
    return RequestSnapshot(
        method=request.method,
        protocol=str(request.environ.get("SERVER_PROTOCOL", "")),
        path=request.path,
        root_path=request.script_root,
        url=_get_url(),
        query=QueryParams(
            parse_qsl(_get_query_string(), keep_blank_values=True, errors=_INVALID_UTF8)
        ),
        headers=Headers(request.headers.items()),
        content_length=request.content_length,
        client_address=request.remote_addr,
        route=None if found is None else found.route.path,
        operation_id=None if found is None else found.route.operation_id,
        path_params=MappingProxyType({} if found is None else found.params),
    )


def _has_request() -> bool:
    """Check whether a request is being handled.

    Returns:
        Whether a request is being handled, i.e., whether
        :py:func:`current_request` gives one.
    """
    return has_request_context()


def _match_request() -> RouteMatch | None:
    """Find the route of the request being handled.

    For FOCA's reads of the route alone, e.g., for each log record, without
    taking a whole snapshot.

    Returns:
        The route and path parameters of the operation serving the request
        (cf. :py:func:`foca.api.routes.match`), or `None` if no operation
        serves it or no request is being handled.
    """
    if not _has_request():
        return None
    return match(request.path, request.method)


def _get_operation_id() -> str:
    """Get the identifier of the operation serving the request being handled.

    Returns:
        Identifier of the operation, as in
        :py:attr:`RequestSnapshot.operation_id`, or an empty string if no
        operation serves the request or no request is being handled.
    """
    found = _match_request()
    return "" if found is None else found.route.operation_id or ""


def _get_request_key() -> object | None:
    """Get an object identifying the request being handled.

    For FOCA's marks that must not apply to other requests, e.g., in an app
    context that outlives a request.

    Returns:
        The same object throughout a request and another one for each
        request, or `None` if no request is being handled.
    """
    if not _has_request():
        return None
    return request._get_current_object()  # type: ignore[attr-defined]


def _get_token_info() -> dict[str, Any]:
    """Get the token info of the current request, without copying it.

    For FOCA's own reads; callers must not change it (cf.
    :py:func:`foca.security.auth.current_token_info`).

    Returns:
        Token info that Connexion's security handler got for the current
        request, or an empty dictionary if there is no request, the request
        was not handled by Connexion, or no token was validated.
    """
    if not _has_request():
        return {}
    # Connexion stores the request's context (including validated token
    # info) on Flask's request context (cf. `connexion.context`)
    connexion_context = getattr(request_ctx, "connexion_context", None)
    if connexion_context is None:
        return {}
    token_info = connexion_context.get("token_info")
    return token_info if isinstance(token_info, dict) else {}


def _set_token_info(token_info: Mapping[str, Any]) -> None:
    """Set the token info of the current request, as Connexion does.

    For :py:func:`foca.testing.request_context`, which sets up requests
    without HTTP.

    Args:
        token_info: Token info, as Connexion's security handler gives it.

    Raises:
        RuntimeError: Raised if no request is being handled.
    """
    if not _has_request():
        raise RuntimeError("No request: token info can be set only in a request")
    # Other entries of Connexion's context are kept
    connexion_context = getattr(request_ctx, "connexion_context", None) or {}
    request_ctx.connexion_context = {  # type: ignore[attr-defined]
        **connexion_context,
        "token_info": dict(token_info),
    }


class _ResponseBody:
    """Body of a response that calls a function when it's closed.

    Args:
        body: Body of the response.
        on_close: Function to call, also if closing the body fails.
    """

    def __init__(self, body: Iterable[bytes], on_close: Callable[[], None]) -> None:
        """Wrap a body.

        Args:
            body: Body of the response.
            on_close: Function to call, also if closing the body fails.
        """
        self._body = body
        self._iterator = iter(body)
        self._on_close = on_close

    def __iter__(self) -> Iterator[bytes]:
        """Get the iterator of the body.

        Returns:
            Iterator of the body.
        """
        return self._iterator

    def close(self) -> None:
        """Close the body."""
        try:
            close = getattr(self._body, "close", None)
            if close is not None:
                close()
        finally:
            self._on_close()


class RequestIdMiddleware:
    """WSGI middleware giving each request an identifier.

    The identifier is available from :py:func:`request_id` until the response
    is closed, including while it's streamed, and sent in a response header.

    Args:
        app: WSGI application.
        header: Name of the request and response header.
        trust_incoming: Whether to take the identifier from the request header,
            if it's valid (at most 128 letters, digits and `._:-`); only for
            apps behind a proxy that replaces clients' values or rejects them.

    Attributes:
        app: WSGI application.
        header: Name of the request and response header.
        trust_incoming: Whether to take the identifier from the request header.
    """

    def __init__(
        self,
        app: Callable[..., Iterable[bytes]],
        header: str = DEFAULT_REQUEST_ID_HEADER,
        trust_incoming: bool = False,
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
            return _ResponseBody(body, _clear)
        except BaseException:
            _clear()
            raise
