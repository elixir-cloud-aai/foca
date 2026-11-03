"""Tests of the guarded HTTP client over real TLS, on a local server.

They check whether errors say that a request was sent or not, as seen from
real handshakes and connections rather than from errors raised by fakes.
"""

import contextlib
import datetime
import ipaddress
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import requests.adapters
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from foca.utils.http import (
    DeadlineExceeded,
    GuardedClient,
    RequestMaybeSent,
    RequestNotSent,
)

Behaviour = Callable[[socket.socket, threading.Event], None]


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Create a self-signed certificate for the loopback address.

    Args:
        tmp_path_factory: Factory of temporary directories.

    Returns:
        Paths of the certificate and its key.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "foca-test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp("tls")
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def trusted(certificate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Make clients trust the local certificate.

    Args:
        certificate: Paths of the certificate and its key.
        monkeypatch: Fixture to change the verification context of `requests`.
    """
    monkeypatch.setattr(
        requests.adapters, "DEFAULT_CA_BUNDLE_PATH", str(certificate[0])
    )


@pytest.fixture
def serve() -> Iterator[Callable[[Behaviour], str]]:
    """Run local servers that treat each connection in a given way.

    Yields:
        Function that starts a server and returns its base URL.
    """
    stop = threading.Event()
    threads: list[threading.Thread] = []
    listeners: list[socket.socket] = []

    def _serve(behaviour: Behaviour) -> str:
        listener = socket.create_server(("127.0.0.1", 0))
        listeners.append(listener)

        def run() -> None:
            with contextlib.suppress(OSError):
                connection, _ = listener.accept()
                with connection:
                    behaviour(connection, stop)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        threads.append(thread)
        return f"https://127.0.0.1:{listener.getsockname()[1]}/"

    yield _serve
    stop.set()
    for listener in listeners:
        listener.close()
    for thread in threads:
        thread.join(timeout=5)


def _tls(certificate: tuple[Path, Path], connection: socket.socket) -> ssl.SSLSocket:
    """Do the server's side of a TLS handshake.

    Args:
        certificate: Paths of the certificate and its key.
        connection: Connection.

    Returns:
        TLS connection.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(*certificate)
    return context.wrap_socket(connection, server_side=True)


def _read_request(connection: ssl.SSLSocket) -> None:
    """Read a request's head.

    Args:
        connection: TLS connection.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            return
        data += chunk


def client(timeout: float = 5) -> GuardedClient:
    """Create a client for the local server.

    Args:
        timeout: Seconds per request.

    Returns:
        Client allowing private addresses.
    """
    return GuardedClient(allow_private_addresses=True, timeout=timeout)


def test_close_in_handshake(serve: Callable[[Behaviour], str]) -> None:
    """A server closing during the handshake got no request."""
    url = serve(lambda connection, stop: None)
    with pytest.raises(RequestNotSent) as info:
        client().post(url, json={})
    # A reset instead of a clean close, if the server was slower than the client
    assert info.value.reason in {"tls_error", "connect_error"}


def test_hang_in_handshake(serve: Callable[[Behaviour], str]) -> None:
    """A deadline passing during the handshake is before sending."""

    def behaviour(connection: socket.socket, stop: threading.Event) -> None:
        stop.wait(10)

    url = serve(behaviour)
    with pytest.raises(DeadlineExceeded):
        client(timeout=0.3).post(url, json={})


def test_untrusted_certificate(
    serve: Callable[[Behaviour], str], certificate: tuple[Path, Path]
) -> None:
    """A certificate that isn't trusted stops the request before sending."""

    def behaviour(connection: socket.socket, stop: threading.Event) -> None:
        with contextlib.suppress(OSError):
            _tls(certificate, connection)

    with pytest.raises(RequestNotSent) as info:
        client().post(serve(behaviour), json={})
    assert info.value.reason == "tls_error"


@pytest.mark.usefixtures("trusted")
def test_response(
    serve: Callable[[Behaviour], str], certificate: tuple[Path, Path]
) -> None:
    """Requests over TLS get their responses."""

    def behaviour(connection: socket.socket, stop: threading.Event) -> None:
        with _tls(certificate, connection) as tls:
            _read_request(tls)
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    response = client().get(serve(behaviour))
    assert response.status == 200
    assert response.content == b"ok"


@pytest.mark.usefixtures("trusted")
def test_close_after_sending(
    serve: Callable[[Behaviour], str], certificate: tuple[Path, Path]
) -> None:
    """A server closing after the handshake may have got the request."""

    def behaviour(connection: socket.socket, stop: threading.Event) -> None:
        with _tls(certificate, connection) as tls:
            _read_request(tls)

    with pytest.raises(RequestMaybeSent) as info:
        client().post(serve(behaviour), json={})
    assert info.value.reason == "connection_lost"


@pytest.mark.usefixtures("trusted")
def test_hang_after_sending(
    serve: Callable[[Behaviour], str], certificate: tuple[Path, Path]
) -> None:
    """A deadline passing after sending may mean that the request arrived."""

    def behaviour(connection: socket.socket, stop: threading.Event) -> None:
        with _tls(certificate, connection) as tls:
            _read_request(tls)
            stop.wait(10)

    with pytest.raises(RequestMaybeSent):
        client(timeout=0.5).post(serve(behaviour), json={})
