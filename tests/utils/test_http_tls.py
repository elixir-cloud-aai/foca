"""Tests of the guarded HTTP client over real TLS, on a local server.

They check whether errors say that a request was sent or not, as seen from
real handshakes and connections rather than from errors raised by fakes,
and which certificate authorities the client trusts.
"""

import contextlib
import datetime
import ipaddress
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

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
    return _self_signed(tmp_path_factory.mktemp("tls"), "foca-test")


def _self_signed(directory: Path, common_name: str) -> tuple[Path, Path]:
    """Create a self-signed CA certificate for the loopback address.

    Args:
        directory: Directory of the files.
        common_name: Common name of the certificate.

    Returns:
        Paths of the certificate and its key.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
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
    cert_path = directory / f"{common_name}.pem"
    key_path = directory / f"{common_name}.key"
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
        client(timeout=2).post(serve(behaviour), json={})


def _ok(certificate: tuple[Path, Path]) -> Behaviour:
    """Answer one request over TLS with status `200` and a JSON body.

    Args:
        certificate: Paths of the certificate and its key.

    Returns:
        Behaviour of the server.
    """

    def behaviour(connection: socket.socket, stop: threading.Event) -> None:
        with contextlib.suppress(OSError), _tls(certificate, connection) as tls:
            _read_request(tls)
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n[1, 2]")

    return behaviour


def _handshake_only(certificate: tuple[Path, Path]) -> Behaviour:
    """Do the server's side of the handshake, then close.

    Args:
        certificate: Paths of the certificate and its key.

    Returns:
        Behaviour of the server.
    """

    def behaviour(connection: socket.socket, stop: threading.Event) -> None:
        with contextlib.suppress(OSError):
            _tls(certificate, connection)

    return behaviour


def test_ca_bundle_file(
    serve: Callable[[Behaviour], str], certificate: tuple[Path, Path]
) -> None:
    """A CA bundle file makes the client trust its certificate authorities."""
    url = serve(_ok(certificate))
    http = GuardedClient(
        allow_private_addresses=True, timeout=5, ca_bundle=certificate[0]
    )
    assert http.ca_bundle == str(certificate[0])
    response = http.get(url)
    assert response.status == 200
    assert response.content == b"[1, 2]"


def test_ca_bundle_directory(
    serve: Callable[[Behaviour], str],
    certificate: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """A CA directory (with hashed names, as by `c_rehash`) is trusted."""
    openssl = shutil.which("openssl")
    if openssl is None:  # pragma: no cover - OpenSSL CLI in CI and locally
        pytest.skip("openssl not installed")
    subject_hash = subprocess.run(  # noqa: S603 - fixed arguments
        [openssl, "x509", "-hash", "-noout", "-in", str(certificate[0])],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    shutil.copy(certificate[0], tmp_path / f"{subject_hash}.0")
    url = serve(_ok(certificate))
    http = GuardedClient(allow_private_addresses=True, timeout=5, ca_bundle=tmp_path)
    assert http.get(url).status == 200


@pytest.mark.usefixtures("trusted")
def test_ca_bundle_only(
    serve: Callable[[Behaviour], str],
    certificate: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """With a CA bundle, the default certificate authorities aren't trusted."""
    other, _ = _self_signed(tmp_path, "other-ca")
    http = GuardedClient(allow_private_addresses=True, timeout=5, ca_bundle=other)
    with pytest.raises(RequestNotSent) as info:
        http.post(serve(_handshake_only(certificate)), json={})
    assert info.value.reason == "tls_error"


def test_ca_bundle_missing(
    serve: Callable[[Behaviour], str],
    certificate: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """A CA bundle that doesn't exist fails the request before sending."""
    url = serve(_ok(certificate))
    http = GuardedClient(
        allow_private_addresses=True, timeout=5, ca_bundle=tmp_path / "missing.pem"
    )
    with pytest.raises(RequestNotSent) as info:
        http.post(url, json={})
    assert info.value.reason == "tls_error"
    assert "missing.pem" in str(info.value)


def test_ca_bundle_invalid(
    serve: Callable[[Behaviour], str],
    certificate: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """A CA bundle without certificates fails the request before sending."""
    url = serve(_handshake_only(certificate))
    bundle = tmp_path / "invalid.pem"
    bundle.write_text("not a certificate")
    http = GuardedClient(allow_private_addresses=True, timeout=5, ca_bundle=bundle)
    with pytest.raises(RequestNotSent) as info:
        http.post(url, json={})
    assert info.value.reason == "tls_error"


def test_ca_bundle_removed_before_sending(
    serve: Callable[[Behaviour], str],
    certificate: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CA bundle that disappears after the check fails before sending."""

    def cert_verify(*args: object, **kwargs: object) -> None:
        raise OSError("Could not find a suitable TLS CA certificate bundle")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "cert_verify", cert_verify)
    url = serve(_ok(certificate))
    http = GuardedClient(
        allow_private_addresses=True, timeout=5, ca_bundle=certificate[0]
    )
    with pytest.raises(RequestNotSent) as info:
        http.post(url, json={})
    assert info.value.reason == "tls_error"


class _BytesPath:
    """Path-like object with a path as bytes."""

    def __fspath__(self) -> bytes:
        """Get the path.

        Returns:
            Path, as bytes.
        """
        return b"/etc/ssl/certs"


@pytest.mark.parametrize(
    ("ca_bundle", "error"),
    [
        (False, TypeError),
        (True, TypeError),
        (0, TypeError),
        (b"/etc/ssl/certs", TypeError),
        (_BytesPath(), TypeError),
        ("", ValueError),
    ],
)
def test_ca_bundle_never_disables_verification(
    ca_bundle: Any, error: type[Exception]
) -> None:
    """Values that would turn verification off (or are no paths) are refused."""
    with pytest.raises(error):
        GuardedClient(ca_bundle=ca_bundle)


def test_ca_bundle_read_only(certificate: tuple[Path, Path]) -> None:
    """The CA bundle of a client can't be changed (e.g., to `False`)."""
    http = GuardedClient(ca_bundle=certificate[0])
    with pytest.raises(AttributeError):
        http.ca_bundle = False  # type: ignore[misc,assignment]
    assert GuardedClient().ca_bundle is None
