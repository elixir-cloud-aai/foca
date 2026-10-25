"""Tests of the guarded HTTP client (`foca.utils.http`).

Requests go to a local server only (loopback), never to the network.
"""

import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import requests
import urllib3

from foca.utils import http
from foca.utils.http import (
    AddressNotAllowed,
    DeadlineExceeded,
    GuardedClient,
    HostNotResolved,
    HttpError,
    InvalidRequest,
    InvalidRequestURL,
    RequestMaybeSent,
    RequestNotSent,
    ResponseTooLarge,
    check_public_host,
    fetch_json,
    is_public_address,
)


class Handler(BaseHTTPRequestHandler):
    """Handler of the local test server."""

    def log_message(self, format: str, *args: Any) -> None:
        """Don't log.

        Args:
            format: Format.
            args: Arguments.
        """

    def do_GET(self) -> None:
        """Answer by path."""
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/")
            self.end_headers()
            return
        if self.path == "/slow":
            self.send_response(200)
            self.end_headers()
            for _ in range(20):
                self.wfile.write(b"x" * 10)
                self.wfile.flush()
                time.sleep(0.02)
            return
        body = b"[1, 2]" if self.path != "/large" else b"x" * 2048
        if self.path == "/text":
            body = b"not json"
        self.send_response(200 if self.path != "/missing" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """Echo the body."""
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    """Run a local HTTP server.

    Yields:
        Base URL of the server.
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def local(**kwargs: Any) -> GuardedClient:
    """Create a client for the local server.

    Args:
        kwargs: Other arguments of the client.

    Returns:
        Client allowing `http` and private addresses.
    """
    return GuardedClient(allow_http=True, allow_private_addresses=True, **kwargs)


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("10.0.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("192.0.2.1", False),
        ("224.0.0.1", False),
        ("2001:4860:4860::8888", True),
        ("::1", False),
        ("fe80::1", False),
        ("fd00::1", False),
        ("ff0e::1", False),
        ("::ffff:10.0.0.1", False),
        ("::ffff:8.8.8.8", True),
        ("64:ff9b::a00:1", False),
        ("64:ff9b::808:808", True),
        ("::a00:1", False),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    """Only globally reachable unicast addresses are public."""
    assert is_public_address(ipaddress.ip_address(address)) is public


def test_check_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts must resolve to public addresses only."""
    check_public_host("https://8.8.8.8/")
    with pytest.raises(AddressNotAllowed, match="zone"):
        check_public_host("https://[fe80::1%25eth0]/")
    with pytest.raises(AddressNotAllowed):
        check_public_host("https://10.0.0.1/")

    def resolve(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        if host == "unknown.example.org":
            raise socket.gaierror("unknown")
        if host == "none.example.org":
            return []
        if host == "odd.example.org":
            return [(None, None, None, "", ("not-an-ip", 443))]
        return [
            (None, None, None, "", ("8.8.8.8", 443)),
            (None, None, None, "", ("10.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(HostNotResolved, match="gaierror"):
        check_public_host("https://unknown.example.org/")
    with pytest.raises(HostNotResolved, match="no address"):
        check_public_host("https://none.example.org/")
    with pytest.raises(AddressNotAllowed, match="not-an-ip"):
        check_public_host("https://odd.example.org/")
    with pytest.raises(AddressNotAllowed, match=r"10\.0\.0\.1"):
        check_public_host("https://mixed.example.org:8443/")


def test_request(server: str) -> None:
    """Requests return the whole response."""
    client = local()
    response = client.get(f"{server}/ok", params={"a": "1"})
    assert response.status == 200
    assert response.json() == [1, 2]
    assert response.headers["content-type"] == "application/json"
    response = client.post(f"{server}/echo", json={"a": 1})
    assert response.status == 201
    assert response.json() == {"a": 1}
    client.close()
    client.close()


def test_private_addresses_refused(server: str) -> None:
    """Connections to addresses that are not public are refused."""
    client = GuardedClient(allow_http=True)
    with pytest.raises(AddressNotAllowed) as info:
        client.get(f"{server}/ok")
    assert info.value.reason == "not_public"
    assert isinstance(info.value, RequestNotSent)


def test_https_connections_checked(server: str) -> None:
    """HTTPS connections are checked before the TLS handshake."""
    port = server.rsplit(":", 1)[1]
    with pytest.raises(AddressNotAllowed):
        GuardedClient().get(f"https://127.0.0.1:{port}/")


def test_redirects_not_followed(server: str) -> None:
    """Redirects are returned as responses."""
    response = local().get(f"{server}/redirect")
    assert response.status == 302
    assert response.headers["location"] == "http://127.0.0.1:1/"


def test_schemes() -> None:
    """Only `https` is allowed by default."""
    with pytest.raises(InvalidRequestURL, match="http"):
        GuardedClient().get("http://example.org/")
    with pytest.raises(InvalidRequestURL, match="none"):
        GuardedClient().get("example.org")
    with pytest.raises(InvalidRequestURL, match="InvalidURL"):
        GuardedClient().get("https://")


def test_size_limit(server: str) -> None:
    """Responses larger than the limit are refused."""
    with pytest.raises(ResponseTooLarge) as info:
        local(max_response_size=1024).get(f"{server}/large")
    assert isinstance(info.value, RequestMaybeSent)


def test_deadline(server: str) -> None:
    """Requests end by the deadline."""
    with pytest.raises(DeadlineExceeded):
        local(deadline=time.monotonic() - 1).get(f"{server}/ok")
    with pytest.raises(RequestMaybeSent) as info:
        local(deadline=time.monotonic() + 0.1).get(f"{server}/slow")
    assert info.value.reason == "read_timeout"
    client = local(timeout=5)
    assert client.remaining() == 5
    client.deadline = time.monotonic() + 1
    assert client.remaining() <= 1


def test_connection_refused() -> None:
    """Refused connections are not sent."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(RequestNotSent) as info:
        local().get(f"http://127.0.0.1:{port}/")
    assert info.value.reason == "connect_error"


@pytest.mark.parametrize(
    ("error", "kind", "reason"),
    [
        (
            requests.ConnectionError(
                urllib3.exceptions.NameResolutionError("h", None, socket.gaierror())  # type: ignore[arg-type]
            ),
            HostNotResolved,
            "dns_error",
        ),
        (requests.exceptions.SSLError("bad"), RequestNotSent, "tls_error"),
        (
            requests.ConnectionError(
                urllib3.exceptions.MaxRetryError(
                    None,  # type: ignore[arg-type]
                    "/",
                    reason=urllib3.exceptions.SSLError(ssl.SSLCertVerificationError()),
                )
            ),
            RequestNotSent,
            "tls_error",
        ),
        (requests.exceptions.ConnectTimeout(), RequestNotSent, "connect_error"),
        (requests.exceptions.ReadTimeout(), RequestMaybeSent, "read_timeout"),
        (requests.ConnectionError("reset"), RequestMaybeSent, "connection_lost"),
    ],
)
def test_classify(error: Exception, kind: type[HttpError], reason: str) -> None:
    """Errors of requests are classified as sent or not."""
    result = http._classify(error, "https://example.org")  # type: ignore[arg-type]
    assert type(result) is kind
    assert result.reason == reason


def test_classify_cycles() -> None:
    """Cyclic exception chains end."""
    error = requests.ConnectionError("x")
    error.__context__ = error
    assert http._classify(error, "u").reason == "connection_lost"


def test_read_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Errors while reading are classified."""

    class Response:
        def iter_content(self, chunk_size: int) -> Iterator[bytes]:
            raise requests.exceptions.ChunkedEncodingError("lost")

    with pytest.raises(RequestMaybeSent):
        GuardedClient()._read(
            Response(),  # type: ignore[arg-type]
            "u",
            http._Guard(allow_private_addresses=False),
        )


def test_fetch_json(server: str) -> None:
    """JSON documents are fetched with status 200 only."""
    options: dict[str, Any] = {"allow_private_addresses": True, "allow_http": True}
    assert fetch_json(f"{server}/ok", 5, **options) == [1, 2]
    with pytest.raises(HttpError) as info:
        fetch_json(f"{server}/missing", 5, **options)
    assert info.value.reason == "http_404"
    with pytest.raises(HttpError) as info:
        fetch_json(f"{server}/redirect", 5, **options)
    assert info.value.reason == "http_302"
    with pytest.raises(HttpError) as info:
        fetch_json(f"{server}/text", 5, **options)
    assert info.value.reason == "invalid_json"


def test_json_too_deep() -> None:
    """Deeply nested JSON is invalid."""
    response = http.HttpResponse(status=200, content=b"[" * 100000)
    with pytest.raises(ValueError, match=r"deeply|Expecting"):
        response.json()


def test_public_peer_connects(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Connections to public addresses pass the check."""
    monkeypatch.setattr(http, "is_public_address", lambda address: True)
    assert GuardedClient(allow_http=True).get(f"{server}/ok").status == 200


@pytest.fixture
def trickle() -> Iterator[str]:
    """Run a server that sends one byte at a time and never ends.

    Yields:
        Base URL of the server; the path `/headers` trickles the headers,
        any other path the body.
    """
    listener = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve(conn: socket.socket) -> None:
        with conn:
            request = conn.recv(65536)
            data = b"HTTP/1.1 200 OK\r\n"
            if b"/headers" not in request:
                conn.sendall(data + b"Content-Length: 1000000\r\n\r\n")
                data = b""
            try:
                while not stop.is_set():
                    conn.sendall(data[:1] or b"x")
                    data = data[1:]
                    time.sleep(0.02)
            except OSError:
                pass

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    stop.set()
    listener.close()


@pytest.mark.parametrize("path", ["/headers", "/body"])
def test_deadline_bounds_reads(trickle: str, path: str) -> None:
    """Servers that send slowly cannot keep requests open."""
    started = time.monotonic()
    with pytest.raises(RequestMaybeSent) as info:
        local(deadline=time.monotonic() + 0.3).get(f"{trickle}{path}")
    assert info.value.reason == "read_timeout"
    assert time.monotonic() - started < 2


def slow_resolver(seconds: float) -> Any:
    """Create a resolver that takes some seconds.

    Args:
        seconds: Seconds per resolution.

    Returns:
        Replacement of `socket.getaddrinfo`.
    """

    def resolve(*args: Any, **kwargs: Any) -> list[Any]:
        time.sleep(seconds)
        return [(None, None, None, "", ("8.8.8.8", 443))]

    return resolve


def test_resolution_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name resolution ends by the deadline."""
    monkeypatch.setattr(socket, "getaddrinfo", slow_resolver(2))
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        check_public_host("https://slow.example.org/", timeout=0.1)
    with pytest.raises(DeadlineExceeded):
        GuardedClient(deadline=time.monotonic() + 0.1).get("https://slow.example.org/")
    assert time.monotonic() - started < 1.5


def test_resolution_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts that cannot be resolved are reported as such."""

    def resolve(*args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror("unknown")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(HostNotResolved) as info:
        GuardedClient(timeout=2).get("https://unknown.example.org/")
    assert info.value.reason == "dns_error"


def test_no_time_to_connect(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Connections are not opened once the deadline passed."""
    resolve = http._resolve

    def late(*args: Any) -> list[str]:
        time.sleep(0.2)
        return resolve(*args)

    monkeypatch.setattr(http, "_resolve", late)
    with pytest.raises(DeadlineExceeded, match="connect"):
        local(deadline=time.monotonic() + 0.1).get(f"{server}/ok")


def test_connect_timeout(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Connections that time out are not sent."""

    def timeout(*args: Any, **kwargs: Any) -> socket.socket:
        raise TimeoutError("timed out")

    monkeypatch.setattr(http.urllib3_connection, "create_connection", timeout)
    with pytest.raises(RequestNotSent) as info:
        local(timeout=2).get(f"{server}/ok")
    assert info.value.reason == "connect_error"


def test_rebinding(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Peers are checked again after connecting."""
    port = int(server.rsplit(":", 1)[1])
    connect = http.urllib3_connection.create_connection
    monkeypatch.setattr(http, "_resolve", lambda *args: ["8.8.8.8"])
    monkeypatch.setattr(
        http.urllib3_connection,
        "create_connection",
        lambda address, *args, **kwargs: connect(("127.0.0.1", port), *args, **kwargs),
    )
    with pytest.raises(AddressNotAllowed, match=r"127\.0\.0\.1"):
        GuardedClient(allow_http=True, timeout=2).get(
            f"http://public.example.org:{port}/"
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1/",
        "https://" + "a" * 64 + ".example.org/",
        "https://" + "a." * 127 + "org/",
        "https://example.org:99999/",
    ],
)
def test_malformed_urls(url: str) -> None:
    """Malformed URLs are invalid, also for host checks."""
    with pytest.raises(InvalidRequestURL):
        GuardedClient(timeout=1).get(url)
    with pytest.raises(InvalidRequestURL):
        check_public_host(url)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"json": {"x": float("nan")}}, "invalid_request"),
        ({"json": {1, 2}}, "invalid_request"),
        ({"json": [object()]}, "invalid_request"),
        ({"headers": {"X": "a\r\nB: c"}}, "invalid_header"),
        ({"headers": {"X": " leading"}}, "invalid_header"),
    ],
)
def test_invalid_requests(kwargs: dict[str, Any], reason: str) -> None:
    """Bodies that are no JSON and invalid headers are not sent."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(InvalidRequest) as info:
        local(timeout=2).post(f"http://127.0.0.1:{port}/", **kwargs)
    assert isinstance(info.value, RequestNotSent)
    assert info.value.reason == reason


@pytest.mark.parametrize("method", ["GET\r\nX-Injected: 1", "", "G ET", 1])
def test_invalid_methods(method: Any) -> None:
    """Methods must be tokens."""
    with pytest.raises(InvalidRequestURL, match="method"):
        GuardedClient(timeout=1).request(method, "https://example.org/")


class KeepAliveHandler(Handler):
    """Handler with persistent connections and slow paths.

    `/sleep/<seconds>` sends half of the body, waits, then sends the rest;
    `/drip` sends one byte every 50 ms and never ends.
    """

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Answer by path."""
        if self.path.startswith("/sleep/"):
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            self.wfile.write(b"x" * 5)
            self.wfile.flush()
            time.sleep(float(self.path.rsplit("/", 1)[1]))
            self.wfile.write(b"y" * 5)
            return
        if self.path == "/drip":
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
            return
        super().do_GET()


@pytest.fixture(scope="module")
def keep_alive() -> Iterator[str]:
    """Run a local HTTP/1.1 server with persistent connections.

    Yields:
        Base URL of the server.
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), KeepAliveHandler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_concurrent_deadlines(keep_alive: str) -> None:
    """Concurrent requests of one client keep their own deadlines."""
    client = local(timeout=1.0)
    results: dict[str, Any] = {}

    def get(name: str, seconds: float, delay: float) -> None:
        time.sleep(delay)
        try:
            results[name] = client.get(f"{keep_alive}/sleep/{seconds}").content
        except HttpError as exc:
            results[name] = exc

    threads = [
        threading.Thread(target=get, args=("a", 0.5, 0)),
        # ends 1.1 s after a starts, within its own second
        threading.Thread(target=get, args=("b", 0.85, 0.25)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == {"a": b"xxxxxyyyyy", "b": b"xxxxxyyyyy"}
    client.close()


def test_reused_connections_bounded(keep_alive: str) -> None:
    """Deadlines also stop requests on reused connections."""
    client = local(timeout=0.5)
    assert client.get(f"{keep_alive}/ok").status == 200
    started = time.monotonic()
    with pytest.raises(RequestMaybeSent) as info:
        client.get(f"{keep_alive}/drip")
    assert info.value.reason == "read_timeout"
    assert time.monotonic() - started < 2
    client.close()


def test_guard() -> None:
    """Sockets of expired requests are shut down, also when added late."""
    guard = http._Guard(allow_private_addresses=True)
    guard.start(0.01)
    time.sleep(0.1)
    assert guard.expired
    first, second = socket.socketpair()
    with first, second:
        guard.track(first)
        assert first.recv(1) == b""
    guard.track(first)  # closed: shutting it down fails silently
    guard.finish()
    guard.finish()
    assert guard.left() < 0
    finished = http._Guard(allow_private_addresses=True)
    finished.finish()  # never started
    finished._stop()  # e.g., a timer that fires late: stops nothing
    assert not finished.expired


def test_guards_per_thread() -> None:
    """Connections find the guard of their thread's request only."""
    guards = http._Guards(allow_private_addresses=False)
    with pytest.raises(RuntimeError, match="No guarded request"):
        guards.current()
    with guards.request(5) as guard:
        assert guards.current() is guard
        seen: list[Any] = []

        def other() -> None:
            try:
                guards.current()
            except RuntimeError as exc:
                seen.append(exc)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        assert len(seen) == 1
    with pytest.raises(RuntimeError):
        guards.current()


def test_expired_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Errors of requests stopped at the deadline are read timeouts."""
    client = GuardedClient(timeout=2)

    def fail(error: Exception) -> Any:
        def request(*args: Any, **kwargs: Any) -> None:
            client._guards.current().expired = True
            raise error

        return request

    for error in (ValueError("closed"), requests.ConnectionError("reset")):
        monkeypatch.setattr(client.session, "request", fail(error))
        with pytest.raises(RequestMaybeSent) as info:
            client.get("https://example.org/")
        assert info.value.reason == "read_timeout"

    class Expired:
        """Response whose reading the deadline stops."""

        def __init__(
            self, guard: Any, chunks: list[bytes], error: bool = False
        ) -> None:
            """Create the response.

            Args:
                guard: Guard of the request.
                chunks: Chunks of the body; the deadline passes before the
                    first.
                error: Whether reading fails at the end.
            """
            self.guard = guard
            self.chunks = chunks
            self.error = error

        def iter_content(self, chunk_size: int) -> Iterator[bytes]:
            """Read the body.

            Args:
                chunk_size: Ignored.

            Yields:
                Chunks.

            Raises:
                ChunkedEncodingError: If reading fails.
            """
            self.guard.expired = True
            yield from self.chunks
            if self.error:
                raise requests.exceptions.ChunkedEncodingError("lost")

    for chunks, fails in (([], False), ([], True), ([b"x", b"y"], False)):
        guard = http._Guard(allow_private_addresses=False)
        response = Expired(guard, chunks, error=fails)
        with pytest.raises(RequestMaybeSent, match="deadline"):
            client._read(response, "u", guard)  # type: ignore[arg-type]
