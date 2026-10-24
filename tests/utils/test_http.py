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
            "connect_error",
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
        (requests.exceptions.InvalidHeader(), RequestNotSent, "connect_error"),
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
        GuardedClient()._read(Response(), "u")  # type: ignore[arg-type]


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
