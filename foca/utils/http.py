"""Guarded outbound HTTP: public addresses only, no redirects, bounded.

Plugins, service discovery and gateways send requests to URLs that come
from users or registries. Unguarded, such requests are server-side request
forgery (SSRF) vectors and can hang or read without bounds. The
:py:class:`GuardedClient` therefore:

- connects to public addresses only (cf. :py:func:`is_public_address`):
  each host is resolved once, within the deadline, all its addresses are
  checked, and the connection goes to a checked address (whose peer address
  is checked again before any data is sent), so that DNS rebinding cannot
  direct it to an internal host;
- never follows redirects (they are returned as responses);
- ignores proxy settings and `.netrc` files of the environment;
- uses `https` only, with TLS verification always on, unless `http` is
  allowed explicitly;
- bounds each request as a whole by a deadline (name resolution,
  connecting, sending and reading, also of servers that send slowly; the
  connect timeout is capped) and the size of each response;
- tells errors before sending (:py:class:`RequestNotSent`) from errors
  after (:py:class:`RequestMaybeSent`), so that gateways know whether a
  backend may have received a request.

Built on requests and urllib3; independent of the web framework.
"""

import contextlib
import ipaddress
import json
import re
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from http.cookiejar import DefaultCookiePolicy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NameResolutionError,
    NewConnectionError,
)
from urllib3.util import connection as urllib3_connection

MAX_RESPONSE_SIZE = 16 * 1024 * 1024
"""Default maximum size of response bodies, in bytes."""

MAX_CONNECT_TIMEOUT = 10.0
"""Maximum seconds to establish a connection."""

_CHUNK_SIZE = 65536

# HTTP methods are tokens (RFC 9110, section 9.1)
_METHOD = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,32}")

# Prefixes of IPv6 addresses that embed IPv4 addresses in their last 32
# bits: IPv4/IPv6 translation (RFC 6052) and IPv4-compatible addresses
# (deprecated, RFC 4291)
_EMBEDDING_PREFIXES = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("::/96"),
)


class HttpError(Exception):
    """Outbound request failed.

    Attributes:
        reason: Reason code, e.g., `connect_error` (`[a-z0-9_]`).
    """

    reason = "http_error"

    def __init__(self, message: str, reason: str | None = None) -> None:
        """Create the error.

        Args:
            message: Description, without credentials.
            reason: Reason code; defaults to that of the class.
        """
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class RequestNotSent(HttpError):
    """The request certainly did not reach the server."""

    reason = "connect_error"


class RequestMaybeSent(HttpError):
    """The server may have received, and processed, the request."""

    reason = "connection_lost"


class InvalidRequestURL(RequestNotSent):
    """The URL is invalid or its scheme is not allowed."""

    reason = "invalid_url"


class InvalidRequest(RequestNotSent):
    """The request is invalid, e.g., its JSON body or a header."""

    reason = "invalid_request"


class HostNotResolved(RequestNotSent):
    """The host has no address."""

    reason = "dns_error"


class AddressNotAllowed(RequestNotSent):
    """The host has an address that is not public, or a zone identifier."""

    reason = "not_public"


class DeadlineExceeded(RequestNotSent):
    """The deadline passed before the request was sent."""

    reason = "deadline_exceeded"


class ResponseTooLarge(RequestMaybeSent):
    """The response body exceeds the size limit."""

    reason = "response_too_large"


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check whether an IP address is public.

    Args:
        address: IP address.

    Returns:
        Whether the address is globally reachable (as per the IANA
        special-purpose address registries) and neither a multicast nor an
        IPv6 site-local address (`fec0::/10`, deprecated, but not marked as
        such in the registry); IPv4 addresses embedded in IPv6 addresses
        (IPv4-mapped, NAT64 and IPv4-compatible ones) are checked themselves.

    Examples:
        >>> is_public_address(ipaddress.ip_address("192.0.2.1"))
        False
        >>> is_public_address(ipaddress.ip_address("::ffff:8.8.8.8"))
        True
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif any(address in prefix for prefix in _EMBEDDING_PREFIXES):
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        elif address.is_site_local:
            return False
    return address.is_global and not address.is_multicast


def _redact(url: str) -> str:
    """Describe a URL for messages, without secrets.

    Args:
        url: URL (or host).

    Returns:
        Scheme, host, port and path, without user info, query and fragment,
        with control and other non-ASCII characters escaped.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "(invalid URL)"
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    text = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return text.encode("unicode_escape").decode("ascii")


def _check_address(address: str, url: str) -> None:
    """Check that an address is public.

    Args:
        address: IP address, as text.
        url: URL or host, for the message.

    Raises:
        AddressNotAllowed: The address is not public or not an IP address.
    """
    try:
        public = is_public_address(ipaddress.ip_address(address))
    except ValueError:
        public = False
    if not public:
        raise AddressNotAllowed(
            f"Host of {_redact(url)} has an address that is not public: {address}"
        )


def _resolve(host: str, port: int, timeout: float, url: str) -> list[str]:
    """Resolve a host within a time limit.

    The system resolver cannot be interrupted, so it runs in a daemon
    thread that is abandoned when the time is up.

    Args:
        host: Host name or IP address.
        port: Port.
        timeout: Seconds for the resolution.
        url: URL, for messages.

    Returns:
        Addresses, in the resolver's order, without duplicates.

    Raises:
        DeadlineExceeded: The time is up before the host is resolved.
        HostNotResolved: The host cannot be resolved, or has no address.
    """
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    outcome: dict[str, Any] = {}
    done = threading.Event()

    def lookup() -> None:
        try:
            outcome["infos"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as exc:
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=lookup, name="foca-http-resolve", daemon=True).start()
    if not done.wait(max(timeout, 0)):
        raise DeadlineExceeded(f"Host of {_redact(url)} not resolved in time")
    if "error" in outcome:
        raise HostNotResolved(
            f"Host of {_redact(url)} not resolved: {type(outcome['error']).__name__}"
        )
    addresses = list(dict.fromkeys(str(info[4][0]) for info in outcome["infos"]))
    if not addresses:
        raise HostNotResolved(f"Host of {_redact(url)} has no address")
    return addresses


def _split(url: str) -> tuple[str, str, int]:
    """Get the scheme, host and port of a URL.

    Args:
        url: URL.

    Returns:
        Scheme (lowercase), host (without brackets) and port (the default
        of the scheme if not given).

    Raises:
        InvalidRequestURL: The URL cannot be parsed, or its port or host
            name is invalid.
        AddressNotAllowed: The host has a zone identifier.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise InvalidRequestURL(f"Invalid URL: {exc}") from exc
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if "%" in host:
        raise AddressNotAllowed(f"Host of {_redact(url)} has a zone identifier")
    if len(host) > 253 or any(len(label) > 63 for label in host.split(".")):
        raise InvalidRequestURL("Invalid URL: host name too long")
    return scheme, host, port or (80 if scheme == "http" else 443)


def check_public_host(url: str, timeout: float = MAX_CONNECT_TIMEOUT) -> None:
    """Check that the host of a URL has public addresses only.

    Host names are resolved; all their addresses must be public (cf.
    :py:func:`is_public_address`). Use it to check URLs before they are
    stored (e.g., discovered backends); requests of the
    :py:class:`GuardedClient` check the addresses of each connection
    themselves.

    Args:
        url: URL.
        timeout: Seconds for the resolution.

    Raises:
        InvalidRequestURL: The URL cannot be parsed.
        DeadlineExceeded: The host was not resolved in time.
        HostNotResolved: The host cannot be resolved, or has no address.
        AddressNotAllowed: The host has an address that is not public, or a
            zone identifier.
    """
    _, host, port = _split(url)
    for address in _resolve(host, port, timeout, url):
        _check_address(address, url)


class _Guard:
    """Limits of one request: its end, its timer and its sockets.

    Attributes:
        allow_private_addresses: Whether addresses that are not public are
            allowed.
        end: `time.monotonic()` value by which the request must end.
        expired: Whether the request was stopped at its end.
    """

    def __init__(
        self, allow_private_addresses: bool, lock: "threading.Lock | None" = None
    ) -> None:
        """Create the guard.

        Args:
            allow_private_addresses: Whether private addresses are allowed.
            lock: Lock shared by the guards of one client, under which
                connections change their request; a new one if not given.
        """
        self.allow_private_addresses = allow_private_addresses
        self.end = float("inf")
        self.expired = False
        self._conns: list[Any] = []
        self._lock = lock or threading.Lock()
        self._timer: threading.Timer | None = None
        self._finished = False

    def left(self) -> float:
        """Get the seconds left for the request.

        Returns:
            Seconds; `0` or less once the end passed.
        """
        return self.end - time.monotonic()

    def track(self, conn: Any, sock: socket.socket | None = None) -> None:
        """Track a connection of the request, to stop it at the end.

        The connection then belongs to this request: timers of requests
        that used it before no longer stop it. A reused connection that
        such a timer stopped meanwhile is closed, so that it reconnects.

        Args:
            conn: urllib3 connection (new or reused).
            sock: New socket of the connection, before urllib3 sets it
                (and wraps it for TLS).
        """
        with self._lock:
            if sock is not None:
                conn._foca_raw = sock
                conn._foca_stopped = False
            elif getattr(conn, "_foca_stopped", False):
                conn._foca_stopped = False
                conn.close()
            conn._foca_guard = self
            if conn not in self._conns:
                self._conns.append(conn)
            if self.expired:
                _stop_conn(conn)

    def start(self, seconds: float) -> None:
        """Start the request, which must end within some seconds.

        Args:
            seconds: Seconds.
        """
        self.end = time.monotonic() + seconds
        self._timer = threading.Timer(seconds, self._stop)
        self._timer.daemon = True
        self._timer.start()

    def finish(self) -> None:
        """End the request: cancel the timer, forget the sockets.

        Call it before the connection goes back to the pool, so that the
        timer cannot stop a socket that another request reuses.
        """
        with self._lock:
            self._finished = True
            self._conns = []
        if self._timer is not None:
            self._timer.cancel()

    def _stop(self) -> None:
        """Stop the request at its end: shut its sockets down.

        Blocked reads and writes then return at once, whatever the server
        sends (e.g., one byte at a time).
        """
        with self._lock:
            if self._finished:
                return
            self.expired = True
            for conn in self._conns:
                if getattr(conn, "_foca_guard", None) is self:
                    _stop_conn(conn)


class _Guards:
    """Guards of the requests of one client, by thread.

    urllib3 connects, sends and reads in the thread of the request, so the
    connections of a client find the guard of their request here.

    Attributes:
        allow_private_addresses: Whether addresses that are not public are
            allowed.
    """

    def __init__(self, allow_private_addresses: bool) -> None:
        """Create the registry.

        Args:
            allow_private_addresses: Whether private addresses are allowed.
        """
        self.allow_private_addresses = allow_private_addresses
        self._local = threading.local()
        self._lock = threading.Lock()

    def current(self) -> _Guard:
        """Get the guard of the request of this thread.

        Returns:
            Guard.

        Raises:
            RuntimeError: No request runs in this thread.
        """
        guard: _Guard | None = getattr(self._local, "guard", None)
        if guard is None:
            raise RuntimeError("No guarded request runs in this thread")
        return guard

    @contextlib.contextmanager
    def request(self, seconds: float) -> Iterator[_Guard]:
        """Guard a request of this thread.

        Args:
            seconds: Seconds within which the request must end.

        Yields:
            Guard of the request; finished afterwards.
        """
        guard = _Guard(self.allow_private_addresses, self._lock)
        guard.start(seconds)
        self._local.guard = guard
        try:
            yield guard
        finally:
            guard.finish()
            self._local.guard = None


def _stop_conn(conn: Any) -> None:
    """Shut the sockets of a connection down (current and new one).

    Args:
        conn: urllib3 connection; `sock` may be a TLS socket wrapping the
            new one (which is then detached).
    """
    conn._foca_stopped = True
    for sock in (getattr(conn, "sock", None), getattr(conn, "_foca_raw", None)):
        if sock is not None:
            _shutdown(sock)


def _shutdown(sock: socket.socket) -> None:
    """Shut a socket down, ignoring errors (e.g., if it is closed).

    Args:
        sock: Socket.
    """
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


def _connect_any(conn: Any, addresses: list[str], end: float) -> socket.socket:
    """Connect to the first address that accepts, in order, by an end.

    Like urllib3's fallback across the addresses of a host, but only to
    checked addresses, within one connect budget.

    Args:
        conn: urllib3 connection (for its port, options and messages).
        addresses: Checked addresses, in the resolver's order.
        end: `time.monotonic()` value by which connecting must end.

    Returns:
        Connected socket.

    Raises:
        urllib3.exceptions.ConnectTimeoutError: The last attempt timed out.
        urllib3.exceptions.NewConnectionError: The last attempt failed.
    """
    error: OSError | None = None
    for address in addresses:
        timeout = end - time.monotonic()
        if timeout <= 0:
            break
        try:
            return urllib3_connection.create_connection(
                (address, conn.port),
                timeout,
                source_address=conn.source_address,
                socket_options=conn.socket_options,
            )
        except OSError as exc:  # also `TimeoutError`
            error = exc
    if error is None or isinstance(error, TimeoutError):
        raise ConnectTimeoutError(conn, f"Connection to {conn.host} timed out") from (
            error
        )
    raise NewConnectionError(
        conn, f"Failed to establish a new connection: {error}"
    ) from error


class _GuardedConnectionMixin:
    """Connect to checked addresses within the request's time left."""

    guards: _Guards

    def _new_conn(self) -> socket.socket:
        """Resolve the host once, check its addresses, and connect.

        Returns:
            Socket, connected to a checked address.

        Raises:
            AddressNotAllowed: An address of the host, or the peer's
                address, is not public.
            DeadlineExceeded: No time is left to resolve or connect.
            urllib3.exceptions.NameResolutionError: The host cannot be
                resolved.
            urllib3.exceptions.ConnectTimeoutError: Connecting timed out.
            urllib3.exceptions.NewConnectionError: Connecting failed.
        """
        conn: Any = self
        guard = self.guards.current()
        host = str(conn._dns_host).strip("[]")
        try:
            addresses = _resolve(host, conn.port, guard.left(), conn.host)
        except HostNotResolved as exc:
            error = socket.gaierror(str(exc))
            raise NameResolutionError(conn.host, conn, error) from exc
        if not guard.allow_private_addresses:
            for address in addresses:
                _check_address(address, conn.host)
        budget = min(guard.left(), MAX_CONNECT_TIMEOUT)
        if budget <= 0:
            raise DeadlineExceeded(f"No time left to connect to {conn.host}")
        sock = _connect_any(conn, addresses, time.monotonic() + budget)
        try:
            if not guard.allow_private_addresses:
                _check_address(str(sock.getpeername()[0]), conn.host)
        except BaseException:
            sock.close()
            raise
        guard.track(conn, sock)
        return sock


class _GuardedPoolMixin:
    """Track reused connections in the guard of their new request."""

    guards: _Guards

    def _get_conn(self, timeout: float | None = None) -> Any:
        """Get a connection of the pool, tracking its socket, if open.

        Args:
            timeout: Seconds to wait for a free connection.

        Returns:
            Connection.
        """
        conn = super()._get_conn(timeout)  # type: ignore[misc]
        if getattr(conn, "sock", None) is not None:
            self.guards.current().track(conn)
        return conn


class _GuardedAdapter(HTTPAdapter):
    """Transport adapter whose connections the guards of requests limit."""

    def __init__(self, guards: _Guards) -> None:
        """Create the adapter.

        Args:
            guards: Guards of the client's requests.
        """
        self.guards = guards
        super().__init__()

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        """Create the pool manager, with guarded connection pools.

        Args:
            args: Positional arguments of `HTTPAdapter.init_poolmanager()`.
            kwargs: Keyword arguments of it.
        """
        super().init_poolmanager(*args, **kwargs)
        attributes = {"guards": self.guards}
        http = type(
            "_GuardedHTTPConnection",
            (_GuardedConnectionMixin, HTTPConnection),
            attributes,
        )
        https = type(
            "_GuardedHTTPSConnection",
            (_GuardedConnectionMixin, HTTPSConnection),
            attributes,
        )
        self.poolmanager.pool_classes_by_scheme = {
            "http": type(
                "_GuardedHTTPConnectionPool",
                (_GuardedPoolMixin, HTTPConnectionPool),
                {"ConnectionCls": http, **attributes},
            ),
            "https": type(
                "_GuardedHTTPSConnectionPool",
                (_GuardedPoolMixin, HTTPSConnectionPool),
                {"ConnectionCls": https, **attributes},
            ),
        }


@dataclass
class HttpResponse:
    """Response to a guarded request.

    Attributes:
        status: HTTP status.
        headers: Headers, with lowercase names.
        content: Body.
    """

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""

    def json(self) -> Any:
        """Parse the body as JSON.

        Returns:
            Parsed body.

        Raises:
            ValueError: The body is not valid JSON (or nested too deeply).
        """
        try:
            return json.loads(self.content)
        except RecursionError as exc:
            raise ValueError("JSON nested too deeply") from exc


def _caused_by(exc: BaseException, types: tuple[type[BaseException], ...]) -> bool:
    """Check whether an exception or one of its causes has one of the types.

    Args:
        exc: Exception, e.g., of `requests`.
        types: Exception types.

    Returns:
        Whether the exception, an exception it was raised from or during, or
        one it wraps (e.g., the `reason` of urllib3 errors) has a type.
    """
    seen: list[BaseException] = []
    pending: list[Any] = [exc]
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or any(
            current is other for other in seen
        ):
            continue
        if isinstance(current, types):
            return True
        seen.append(current)
        pending.extend(
            [
                current.__cause__,
                current.__context__,
                getattr(current, "reason", None),
                *current.args,
            ]
        )
    return False


def _classify(exc: requests.RequestException, url: str) -> HttpError:
    """Map an error of requests onto a guarded error.

    Args:
        exc: Error.
        url: URL of the request.

    Returns:
        :py:class:`RequestNotSent` for errors before the request was sent
        (connection refused or timed out, host not resolved, certificate not
        accepted, request not prepared), :py:class:`RequestMaybeSent`
        otherwise.
    """
    name = type(exc).__name__
    if _caused_by(exc, (urllib3.exceptions.NameResolutionError,)):
        return HostNotResolved(f"Host of {_redact(url)} not resolved")
    if isinstance(exc, requests.exceptions.SSLError) or _caused_by(
        exc, (urllib3.exceptions.SSLError,)
    ):
        return RequestNotSent(
            f"TLS error for {_redact(url)}: {name}", reason="tls_error"
        )
    if isinstance(exc, requests.exceptions.ConnectTimeout) or _caused_by(
        exc, (urllib3.exceptions.ConnectTimeoutError,)
    ):
        # `NewConnectionError` (e.g., connection refused) is a subclass
        return RequestNotSent(f"{_redact(url)} not reached: {name}")
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return RequestMaybeSent(
            f"{_redact(url)} timed out: {name}", reason="read_timeout"
        )
    return RequestMaybeSent(f"{_redact(url)} failed: {name}")


def _check_json_body(body: Any, url: str) -> None:
    """Check that a body can be sent as JSON, as `requests` sends it.

    Args:
        body: Body.
        url: URL of the request, for messages.

    Raises:
        InvalidRequest: The body is no JSON (e.g., `NaN`, a set, an
            object), or nested too deeply.
    """
    try:
        json.dumps(body, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidRequest(
            f"Body for {_redact(url)} is no JSON: {type(exc).__name__}"
        ) from exc


class GuardedClient:
    """HTTP client for untrusted URLs (cf. :py:mod:`foca.utils.http`).

    Attributes:
        deadline: `time.monotonic()` value by which requests must end, if
            any.
        timeout: Seconds per request, if no earlier deadline applies.
        allow_private_addresses: Whether addresses that are not public are
            allowed (e.g., for tests and private deployments).
        allow_http: Whether `http` URLs are allowed.
        max_response_size: Maximum size of response bodies, in bytes.

    Examples:
        >>> client = GuardedClient(timeout=5)
        >>> client.get("ftp://example.org/")
        Traceback (most recent call last):
        ...
        foca.utils.http.InvalidRequestURL: Scheme not allowed: ftp
    """

    def __init__(
        self,
        *,
        deadline: float | None = None,
        timeout: float = 30.0,
        allow_private_addresses: bool = False,
        allow_http: bool = False,
        max_response_size: int = MAX_RESPONSE_SIZE,
    ) -> None:
        """Create the client.

        Args:
            deadline: `time.monotonic()` value by which requests must end.
            timeout: Seconds per request, if no earlier deadline applies.
            allow_private_addresses: Whether addresses that are not public
                are allowed.
            allow_http: Whether `http` URLs are allowed.
            max_response_size: Maximum size of response bodies, in bytes.
        """
        self.deadline = deadline
        self.timeout = timeout
        self.allow_private_addresses = allow_private_addresses
        self.allow_http = allow_http
        self.max_response_size = max_response_size
        self._guards = _Guards(allow_private_addresses)
        self._session: requests.Session | None = None
        self._session_lock = threading.Lock()

    @property
    def session(self) -> requests.Session:
        """Get the session of the client, created on first use.

        Returns:
            Session without environment settings (proxies, `.netrc`) and
            without cookies (requests do not share state).
        """
        with self._session_lock:
            if self._session is None:
                session = requests.Session()
                session.trust_env = False
                session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
                adapter = _GuardedAdapter(self._guards)
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                self._session = session
            return self._session

    def remaining(self) -> float:
        """Get the seconds left for a request.

        Returns:
            Seconds until the deadline or the per-request timeout, whichever
            is earlier.
        """
        if self.deadline is None:
            return self.timeout
        return min(self.timeout, self.deadline - time.monotonic())

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        """Send a `GET` request.

        Args:
            url: URL.
            kwargs: Other arguments of :py:meth:`request`.

        Returns:
            Response.
        """
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HttpResponse:
        """Send a `POST` request.

        Args:
            url: URL.
            kwargs: Other arguments of :py:meth:`request`.

        Returns:
            Response.
        """
        return self.request("POST", url, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: bytes | str | None = None,
        auth: Any = None,
    ) -> HttpResponse:
        """Send a request; redirects are returned, not followed.

        Args:
            method: HTTP method.
            url: URL.
            params: Query parameters.
            headers: Headers.
            json: Body to send as JSON.
            data: Body to send as is.
            auth: Authentication of requests (e.g., a `(user, password)`
                tuple).

        Returns:
            Response, with the whole body.

        The request ends within :py:meth:`remaining` seconds as a whole:
        resolving, connecting, sending and reading.

        Raises:
            InvalidRequestURL: The URL or method is invalid, or the scheme
                is not allowed.
            InvalidRequest: `json` is no JSON (e.g., `NaN`, a set;
                `invalid_request`), or a header is invalid (e.g., with a
                line break; `invalid_header`).
            DeadlineExceeded: The deadline passed before sending.
            RequestNotSent: The request certainly did not reach the server
                (e.g., :py:class:`AddressNotAllowed`).
            RequestMaybeSent: The server may have received the request
                (e.g., read timeout, :py:class:`ResponseTooLarge`, deadline
                passed while reading).
        """
        scheme, _, _ = _split(url)
        allowed = ("https", "http") if self.allow_http else ("https",)
        if scheme not in allowed:
            raise InvalidRequestURL(f"Scheme not allowed: {scheme or '(none)'}")
        if not isinstance(method, str) or not _METHOD.fullmatch(method):
            raise InvalidRequestURL("Invalid HTTP method")
        if json is not None:
            _check_json_body(json, url)
        remaining = self.remaining()
        if remaining <= 0:
            raise DeadlineExceeded(f"No time left for {_redact(url)}")
        session = self.session
        with self._guards.request(remaining) as guard:
            try:
                response = session.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=json,
                    data=data,
                    auth=auth,
                    timeout=(min(remaining, MAX_CONNECT_TIMEOUT), remaining),
                    allow_redirects=False,
                    stream=True,
                    verify=True,
                )
            except (AddressNotAllowed, DeadlineExceeded):
                raise
            except requests.exceptions.InvalidHeader as exc:
                raise InvalidRequest(
                    f"Invalid header for {_redact(url)}", reason="invalid_header"
                ) from exc
            except (requests.exceptions.InvalidURL, ValueError) as exc:
                if guard.expired:
                    raise self._expired(url) from exc
                raise InvalidRequestURL(f"Invalid URL: {type(exc).__name__}") from exc
            except requests.RequestException as exc:
                error = _classify(exc, url)
                if guard.expired and isinstance(error, RequestMaybeSent):
                    raise self._expired(url) from exc
                raise error from exc
            try:
                content = self._read(response, url, guard)
            finally:
                guard.finish()  # before the connection goes back to the pool
                response.close()
        return HttpResponse(
            status=response.status_code,
            headers={name.lower(): value for name, value in response.headers.items()},
            content=content,
        )

    @staticmethod
    def _expired(url: str) -> RequestMaybeSent:
        """Describe a request stopped at its deadline.

        Args:
            url: URL of the request.

        Returns:
            Error.
        """
        return RequestMaybeSent(
            f"{_redact(url)}: deadline passed", reason="read_timeout"
        )

    def _read(self, response: requests.Response, url: str, guard: _Guard) -> bytes:
        """Read a response body within the size limit and the deadline.

        Args:
            response: Streamed response.
            url: URL of the request.
            guard: Guard of the request.

        Returns:
            Body.

        Raises:
            ResponseTooLarge: The body exceeds the size limit.
            RequestMaybeSent: Reading failed, or the deadline passed (the
                guard then shut the connection down, which may look like a
                complete body).
        """
        content = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                content.extend(chunk)
                if len(content) > self.max_response_size:
                    raise ResponseTooLarge(
                        f"{_redact(url)}: response larger than"
                        f" {self.max_response_size} bytes"
                    )
                if guard.expired:
                    raise self._expired(url)
        except requests.RequestException as exc:
            if guard.expired:
                raise self._expired(url) from exc
            raise _classify(exc, url) from exc
        if guard.expired:
            raise self._expired(url)
        return bytes(content)

    def close(self) -> None:
        """Close the connections of the client."""
        with self._session_lock:
            if self._session is not None:
                self._session.close()
                self._session = None


def fetch_json(
    url: str,
    timeout: float,
    *,
    allow_private_addresses: bool = False,
    allow_http: bool = False,
    max_response_size: int = MAX_RESPONSE_SIZE,
) -> Any:
    """Fetch a JSON document (e.g., of a service registry), without token.

    Args:
        url: URL of the document (`https`).
        timeout: Seconds for the request.
        allow_private_addresses: Whether addresses that are not public are
            allowed.
        allow_http: Whether `http` URLs are allowed (e.g., for tests).
        max_response_size: Maximum size of the document, in bytes.

    Returns:
        The document.

    Raises:
        HttpError: The request failed, the status is not `200` (also for
            redirects, which are not followed; reason `http_<status>`), or
            the body is not JSON (reason `invalid_json`).
    """
    client = GuardedClient(
        timeout=timeout,
        allow_private_addresses=allow_private_addresses,
        allow_http=allow_http,
        max_response_size=max_response_size,
    )
    try:
        response = client.get(url, headers={"Accept": "application/json"})
    finally:
        client.close()
    if response.status != 200:
        raise HttpError(
            f"{_redact(url)} unavailable: status {response.status}",
            reason=f"http_{response.status}",
        )
    try:
        return response.json()
    except ValueError as exc:
        raise HttpError(f"{_redact(url)}: invalid JSON", reason="invalid_json") from exc
