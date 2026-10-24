"""Tests for URL checks and normalization."""

import unicodedata
from typing import Any
from unittest import mock

import pytest
from flask import Flask

from foca.errors.exceptions import build_problem
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, ExceptionConfig, ServerConfig
from foca.utils.urls import (
    HTTP_SCHEMES,
    SUPPORTED_SCHEMES,
    InvalidURL,
    check_url,
    check_url_fields,
    get_public_url,
    normalize_url,
)
from foca.utils.urls import _normalize_host as normalize_host


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        (
            "HTTPS://TES.Example.org:443/ga4gh/tes/v1/",
            "https://tes.example.org/ga4gh/tes/v1",
        ),
        ("http://tes.example.org:80/", "http://tes.example.org"),
        ("https://tes.example.org/v1?x=1#top", "https://tes.example.org/v1"),
        ("https://tes.example.org:8443/V1", "https://tes.example.org:8443/V1"),
        ("https://[::1]:443/v1", "https://[::1]/v1"),
        ("https://[::1]:8443/v1", "https://[::1]:8443/v1"),
        ("https://tes.example.org./v1", "https://tes.example.org/v1"),
        (
            "https://tes.example.org//ga4gh/./tes/x/../v1",
            "https://tes.example.org/ga4gh/tes/v1",
        ),
        ("https://tes.example.org/../v1", "https://tes.example.org/v1"),
        ("https://tes.example.org/a/%2e%2E/v1", "https://tes.example.org/v1"),
        ("https://tes.example.org.:8443/v1", "https://tes.example.org:8443/v1"),
        ("https://tes.example.org/%74es/a%2fb", "https://tes.example.org/tes/a%2Fb"),
        ("https://bücher.example.org", "https://xn--bcher-kva.example.org"),
        ("https://XN--BCHER-KVA.example.org", "https://xn--bcher-kva.example.org"),
        ("https://faß.de", "https://xn--fa-hia.de"),
        # zero width non-joiner and joiner (IDNA 2008, CONTEXTJ)
        ("https://\u0628\u06cc\u200c\u062a.ir", "https://xn--ngbe99b612f.ir"),
        ("https://\u0915\u094d\u200d\u0937.in", "https://xn--11b2ezcw70k.in"),
        ("https://a.com。/x", "https://a.com/x"),
        ("https://my_host.internal", "https://my_host.internal"),
        ("https://[0::FFFF:1.2.3.4]/v1", "https://[::ffff:1.2.3.4]/v1"),
        ("https://[fe80::1%25eth0]", "https://[fe80::1%25eth0]"),
        ("https://tes.example.org/ü", "https://tes.example.org/%C3%BC"),
        ("http://tes.example.org:443", "http://tes.example.org:443"),
        ("https://192.0.2.1:443/v1", "https://192.0.2.1/v1"),
        ("https://tes.example.org1/v1", "https://tes.example.org1/v1"),
    ],
)
def test_normalize_url(url: str, normalized: str) -> None:
    """URLs are normalized for comparisons.

    With lowercase schemes and hosts, encoded host names, and without
    trailing dots of hosts, default ports, dot segments, repeated and trailing
    slashes, needless percent-encoding, queries and fragments.
    """
    assert normalize_url(url) == normalized


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("https://a.org/v1?x=%7e&y=%2f#%7Etop", "https://a.org/v1?x=~&y=%2F#~top"),
        ("https://a.org/v1?q=ü#ü", "https://a.org/v1?q=%C3%BC#%C3%BC"),
        ("https://a.org/v1", "https://a.org/v1"),
    ],
)
def test_normalize_url_keep_query_and_fragment(url: str, normalized: str) -> None:
    """Queries and fragments can be kept, with normalized percent-encodings."""
    assert normalize_url(url, keep_query=True, keep_fragment=True) == normalized


def test_normalize_url_keep_query_only() -> None:
    """Queries can be kept without fragments."""
    assert normalize_url("https://a.org/?q=1#top", keep_query=True) == (
        "https://a.org?q=1"
    )


@pytest.mark.parametrize(
    "url",
    [
        "tes.example.org/v1",
        "mailto:support@example.org",
        "javascript://tes.example.org/%0Aalert(1)",
        "file://tes.example.org/etc/passwd",
        "ftp://tes.example.org/v1",
        "https://user:secret@tes.example.org",
        "https://user@tes.example.org",
        "https://tes.example.org:99999",
        "https://tes.example.org:port",
        "https://tes.example.org:0",
        "https://tes.exa\nmple.org",
        "ht\ttps://tes.example.org",
        " https://tes.example.org",
        "\u00a0https://tes.example.org",
        "https://tes.example.org/v1\n",
        "https://tes.ex\u00adample.org",
        "https://tes\u202e.example.org",
        "https://tes.example.org/\u200dv1",
        "http://[::1/v1",
        "https://.",
        "https://[::g]",
        f"https://{'ü' * 64}.example.org",
        # longer than 253 characters once encoded
        f"https://{'.'.join(['ü' * 40] * 6)}.org",
        "https://bü-.example.org",
        "https://tes example.org",
        "https://tes<x>.example.org",
        "https://tes%zz.example.org",
        "https://tes.example.org/a%ZZ",
        "https://tes.example.org/a%2",
        "https://tes.example.org/v1?a=%zz",
        "https://tes.example.org/v1#%2",
        "https://tes.example.org/\ud800",
        "https://[fe80::1%25eth\u200d0]/v1",
        "https://[fe80::1%25ethü]/v1",
        "https://[fe80::1%25]/v1",
        "https://[fe80::1%eth0]/v1",
        "https://[fe80::1%]/v1",
        # empty labels would be removed, making different hosts equal
        "https://a..example.org/v1",
        "https://.example.org/v1",
        "https://example.org../v1",
        "http://127.0.0.1../v1",
        # IPvFuture literals would lose their brackets
        "https://[v1.example.org]/v1",
        # percent-encoded hosts would not be normalized or checked
        "https://ex%61mple.org/v1",
        "https://ex%00ample.org/v1",
        "https://ex%C3%A4mple.org/v1",
        # IPv4 addresses that are not in dotted-decimal form
        "https://127.1/v1",
        "https://0x7f.0.0.1/v1",
        "https://2130706433/v1",
        "https://1.2.3.4.5/v1",
        "https://192.0.2.01/v1",
        "https://example.123/v1",
        # strings only
        None,
        b"https://tes.example.org",
    ],
)
def test_normalize_url_invalid(url: object) -> None:
    """Invalid URLs are rejected.

    Also URLs without scheme or host, with schemes other than `http` and
    `https`, user info, invalid ports or hosts, whitespace or control
    characters.
    """
    with pytest.raises(InvalidURL):
        normalize_url(url)


def test_normalize_host_invalid_ipv6() -> None:
    """Invalid IPv6 addresses are rejected (`urlsplit()` rejects most)."""
    with pytest.raises(InvalidURL, match="need a valid host"):
        normalize_host("::g", bracketed=True, name="URLs")


@pytest.mark.parametrize("zone", ["%25%0a", "%25eth%30"])
def test_normalize_host_invalid_zone(zone: str) -> None:
    """Zone identifiers must not be percent-encoded.

    Python 3.11.4 and later reject them already in `urlsplit()`, older versions do not.
    """
    with pytest.raises(InvalidURL, match="need a valid host"):
        normalize_host(f"fe80::1{zone}", bracketed=True, name="URLs")


# Host names of the maximum length: 253 characters, labels of 63
LONGEST_HOST = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])


@pytest.mark.parametrize(
    "host",
    [
        LONGEST_HOST,
        f"{LONGEST_HOST}.",
        f"{'ü' * 30}。{'a' * 63}。org",
        f"{unicodedata.normalize('NFD', 'é' * 40)}.org",
    ],
    ids=["253", "253-trailing-dot", "international", "decomposed"],
)
def test_normalize_url_longest_host(host: str) -> None:
    """Hosts of up to 253 characters, with labels of up to 63, are valid."""
    assert normalize_url(f"https://{host}/v1").startswith("https://")


@pytest.mark.parametrize(
    "host",
    [
        f"{LONGEST_HOST}a",
        f"{LONGEST_HOST}a.",
        f"{'a' * 64}.example.org",
        f"{'ü' * 64}.example.org",
        f"{'ü' * 20000}.example.org",
        f"{'ü' * 63}。{'ü' * 63}。{'ü' * 63}。{'ü' * 63}。org",
        f"a{unicodedata.lookup('COMBINING ACUTE ACCENT') * 20000}.org",
    ],
    ids=[
        "254",
        "254-trailing-dot",
        "label",
        "international-label",
        "huge",
        "dots",
        "combining",
    ],
)
def test_normalize_url_long_host(host: str) -> None:
    """Long hosts are rejected before IDNA encoding, which is slow for them.

    Hosts must not be longer than 253 characters, labels not longer than 63.
    UTS 46 full stops (e.g., `。`) separate labels, as `.` does.
    """
    with (
        mock.patch("idna.encode", side_effect=AssertionError("encoded")),
        mock.patch("idna.decode", side_effect=AssertionError("decoded")),
        pytest.raises(InvalidURL, match="need a valid host: too long") as exc_info,
    ):
        normalize_url(f"https://{host}/v1")
    assert host not in str(exc_info.value)


@pytest.mark.parametrize(
    "domain",
    [f"{'ü' * 64}.org", f"{'ü.' * 127}org", f"{'ü' * 20000}.org"],
    ids=["label", "254", "huge"],
)
def test_check_url_mailto_long_domain(domain: str) -> None:
    """Long email domains are rejected before IDNA encoding."""
    with (
        mock.patch("idna.encode", side_effect=AssertionError("encoded")),
        pytest.raises(InvalidURL, match="with a domain name"),
    ):
        check_url(f"mailto:a@{domain}", schemes=["mailto"])


def test_normalize_url_schemes() -> None:
    """Only allowed schemes are accepted, e.g., `https` only."""
    assert normalize_url("HTTPS://a.org", schemes=["https"]) == "https://a.org"
    with pytest.raises(InvalidURL, match="one of these schemes: 'https'"):
        normalize_url("http://a.org", schemes=["https"], name="Service URLs")


def test_normalize_url_name() -> None:
    """Messages name the URLs, as given."""
    with pytest.raises(InvalidURL, match=r"^Service URLs need a scheme and a host\."):
        normalize_url("a.org/v1", name="Service URLs")


@pytest.mark.parametrize("schemes", [["mailto"], ["ftp"], [], ["HTTPS"]])
def test_normalize_url_unsupported_schemes(schemes: list[str]) -> None:
    """Only `http` and `https` (lowercase) URLs can be normalized."""
    with pytest.raises(ValueError, match="schemes") as exc_info:
        normalize_url("https://a.org", schemes=schemes)
    assert not isinstance(exc_info.value, InvalidURL)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.example.org",
        "HTTPS://WWW.Example.org:8443/docs?page=1#top",
        "https://[::1]/docs",
        "https://bücher.example.org",
        "https://\u0628\u06cc\u200c\u062a.ir",
        "https://\u0915\u094d\u200d\u0937.in",
    ],
)
def test_check_url(url: str) -> None:
    """HTTP(S) URLs with valid hosts are valid."""
    check_url(url, name="URLs in 'documentationUrl'")


@pytest.mark.parametrize(
    ("url", "detail"),
    [
        (1, "must be strings"),
        ("https://www.example.org/a b", "must not contain whitespace"),
        (" https://www.example.org", "must not contain whitespace"),
        ("\u00a0https://www.example.org", "must not contain whitespace"),
        ("ht\ttps://www.example.org", "must not contain whitespace"),
        ("https://www.exa\nmple.org", "must not contain whitespace"),
        ("https://www.example.org/\x00", "control or format characters"),
        ("https://www.example.org/\u202ev1", "control or format characters"),
        ("https://www.ex\u00adample.org", "control or format characters"),
        ("https://www.example.org/?q=\u200c", "control or format characters"),
        ("https://www.example.org/#\u2028", "control or format characters"),
        ("https://www\u202e.example.org", "control or format characters"),
        ("https://www.example.org/\ud800", "control or format characters"),
        ("https://www.example.org/a<b>", "do not allow unencoded"),
        ("https://www.example.org/a|b", "do not allow unencoded"),
        ("https://www.example.org/a\\b", "do not allow unencoded"),
        ("https://www.example.org/?q={x}", "do not allow unencoded"),
        ("https://www.example.org/#a#b", "do not allow unencoded"),
        ('https://www.example.org/a"b', "do not allow unencoded"),
        ("https://www.example.org/a^b", "do not allow unencoded"),
        ("https://www.example.org/a`b", "do not allow unencoded"),
        ("https://www.example.org/[a]", "do not allow unencoded"),
        ("https://xn--a.example.org", "need a valid host"),
        ("https://XN--A.example.org", "need a valid host"),
        ("https://www.example.org/a%zz", "malformed percent-encodings"),
        ("https://www.example.org/?q=%", "malformed percent-encodings"),
        ("https://www.example.org/#%4", "malformed percent-encodings"),
        ("https://[fe80::1%25eth\u200c0]/docs", "need a valid host"),
        ("https://www.example.org:port", "must be valid URLs"),
        ("http://[::1/docs", "must be valid URLs"),
        ("https://www.example.org:0", "need a valid port"),
        ("javascript:alert(1)", "one of these schemes: 'http', 'https'"),
        ("file:///etc/passwd", "one of these schemes"),
        ("ftp://ftp.example.org/pub", "one of these schemes"),
        ("mailto:support@example.org", "one of these schemes"),
        ("www.example.org/docs", "need a scheme and a host"),
        ("not a URL", "must not contain whitespace"),
        ("https:///docs", "need a scheme and a host"),
        ("https://user@www.example.org", "must not contain user info"),
        ("https://www<x>.example.org", "need a valid host"),
    ],
)
def test_check_url_invalid(url: object, detail: str) -> None:
    """Invalid URLs are rejected, saying why.

    URLs must be HTTP(S) URLs with a valid host and port, and without user
    info, whitespace, control or format characters.
    """
    with pytest.raises(InvalidURL) as exc_info:
        check_url(url, name="URLs in 'documentationUrl'")
    assert str(exc_info.value).startswith("URLs in 'documentationUrl' ")
    assert detail in str(exc_info.value)


def test_check_url_https_only() -> None:
    """URLs can be restricted to `https`."""
    check_url("https://a.org", schemes={"https"})
    with pytest.raises(InvalidURL, match="one of these schemes: 'https'"):
        check_url("http://a.org", schemes={"https"})


@pytest.mark.parametrize(
    "url",
    [
        "mailto:support@example.org",
        "MAILTO:support@example.org",
        "mailto:a@example.org,b@bücher.example.org?subject=TES%20access",
        "mailto:support%40tes@example.org",
        f"mailto:a@{'x' * 63}.example.org",
        "mailto:a@x-1.EXAMPLE.org",
        "https://example.org/contact",
    ],
)
def test_check_url_mailto(url: str) -> None:
    """`mailto:` URLs with email addresses are valid if allowed."""
    check_url(url, schemes=SUPPORTED_SCHEMES, name="URLs in 'contactUrl'")


def test_check_url_mailto_only() -> None:
    """With `mailto` only, HTTP(S) URLs are rejected."""
    check_url("mailto:a@example.org", schemes=["mailto"])
    with pytest.raises(InvalidURL, match="one of these schemes: 'mailto'"):
        check_url("https://example.org", schemes=["mailto"])


@pytest.mark.parametrize(
    ("url", "detail"),
    [
        ("mailto:", "need valid email addresses"),
        ("mailto:support", "need valid email addresses"),
        ("mailto:@example.org", "need valid email addresses"),
        ("mailto:support@", "need valid email addresses"),
        ("mailto:a@example.org,", "need valid email addresses"),
        ("mailto:support@exa<mple.org", "do not allow unencoded"),
        ("mailto:a@b@example.org", "need valid email addresses"),
        ("mailto:a@x'onclick=alert(1)", "with a domain name"),
        ("mailto:a@xn--a.org", "with a domain name"),
        ("mailto:a@XN--A.org", "with a domain name"),
        ("mailto:a|b@example.org", "do not allow unencoded"),
        ("mailto://[x", "need valid email addresses"),
        ("mailto:a@-x.org", "with a domain name"),
        ("mailto:a@x-.org", "with a domain name"),
        ("mailto:a@%FF", "with a domain name"),
        ("mailto:a@::1%25eth0", "with a domain name"),
        ("mailto:a@.", "with a domain name"),
        ("mailto:a@example.org.", "with a domain name"),
        ("mailto:a@localhost", "with a domain name"),
        ("mailto:a@192.0.2.1", "with a domain name"),
        ("mailto:a@127.1", "with a domain name"),
        ("mailto:a@b.123", "with a domain name"),
        ("mailto:a@[192.0.2.1]", "do not allow unencoded"),
        (f"mailto:a@{'x' * 64}.org", "with a domain name"),
        (f"mailto:a@{'x.' * 127}org", "with a domain name"),
        ("mailto:a@bü\u0301\u0300--.org", "with a domain name"),
        ("mailto:support@example.org?subject=%zz", "malformed percent-encodings"),
        ("mailto:support%@example.org", "malformed percent-encodings"),
        ("mailto:support@example.org\ud800", "control or format characters"),
        ("mailto:#support", "need valid email addresses"),
        ("mailto:?to=support@example.org", "need valid email addresses"),
        ("mailto:support\u00ad@example.org", "control or format characters"),
        ("mailto:support\u200d@example.org", "control or format characters"),
        ("mailto://example.org/support", "need valid email addresses"),
        ("mailto:support@example.org\n", "must not contain whitespace"),
        ("mailto:a b@example.org", "must not contain whitespace"),
    ],
)
def test_check_url_mailto_invalid(url: str, detail: str) -> None:
    """`mailto:` URLs need email addresses, but no authority.

    They must not contain whitespace or control characters either.
    """
    with pytest.raises(InvalidURL) as exc_info:
        check_url(url, schemes=SUPPORTED_SCHEMES, name="URLs in 'contactUrl'")
    assert str(exc_info.value).startswith("URLs in 'contactUrl' ")
    assert detail in str(exc_info.value)


@pytest.mark.parametrize("schemes", [["ftp"], [], ["HTTP"], "https"])
def test_check_url_unsupported_schemes(schemes: Any) -> None:
    """Allowed schemes must be supported, lowercase and given as a collection."""
    with pytest.raises(ValueError, match="schemes") as exc_info:
        check_url("https://a.org", schemes=schemes)
    assert not isinstance(exc_info.value, InvalidURL)


def test_invalid_url_problem() -> None:
    """FOCA's problem handler responds to invalid URLs with `400`.

    Without the message, which may contain client input.
    """
    with pytest.raises(InvalidURL) as exc_info:
        check_url("ftp://a.org", name="Service URLs")
    assert isinstance(exc_info.value, ValueError)
    assert build_problem(exc_info.value, ExceptionConfig()) == (
        {"title": "Bad Request", "status": 400},
        400,
    )


@pytest.mark.parametrize(
    "check",
    [
        lambda schemes: check_url("https://a.org", schemes=schemes),
        lambda schemes: normalize_url("https://a.org", schemes=schemes),
        lambda schemes: check_url_fields({"a": "https://a.org"}, {"a": schemes}),
    ],
)
def test_schemes_iterator(check: Any) -> None:
    """Allowed schemes can be given as iterators, e.g., generators."""
    check(iter(["https"]))


FIELDS = {
    "url": ["https"],
    "contactUrl": SUPPORTED_SCHEMES,
    "documentationUrl": HTTP_SCHEMES,
    "organization.url": HTTP_SCHEMES,
}


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"organization": "organization"},
        {"organization": {"name": "My organization"}},
        {
            "url": "https://tes.example.org",
            "contactUrl": "mailto:support@example.org",
            "documentationUrl": "http://docs.example.org",
            "organization": {"url": "https://example.org"},
        },
    ],
)
def test_check_url_fields(data: dict[str, Any]) -> None:
    """Missing fields are not checked, and valid URLs are accepted."""
    check_url_fields(data, fields=FIELDS)


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"url": "http://tes.example.org"}, "url"),
        ({"contactUrl": "javascript:alert(1)"}, "contactUrl"),
        ({"documentationUrl": "mailto:support@example.org"}, "documentationUrl"),
        ({"organization": {"url": "file:///etc/passwd"}}, "organization.url"),
        ({"organization": {"url": None}}, "organization.url"),
    ],
)
def test_check_url_fields_invalid(data: dict[str, Any], field: str) -> None:
    """Invalid URLs are rejected, naming their field."""
    with pytest.raises(InvalidURL) as exc_info:
        check_url_fields(data, fields=FIELDS)
    assert str(exc_info.value).startswith(f"URLs in '{field}' ")


def test_check_url_fields_unsupported_schemes() -> None:
    """Unsupported schemes are rejected, also if the field is missing."""
    with pytest.raises(ValueError, match="schemes"):
        check_url_fields({}, fields={"url": ["ftp"]})


def _app(public_url: str | None) -> Flask:
    """Create an app with a public URL.

    Args:
        public_url: Public URL of the app, as configured.

    Returns:
        Flask app.
    """
    config = Config(server=ServerConfig(public_url=public_url))
    return create_connexion_app(config).app


@pytest.mark.parametrize(
    ("public_url", "root", "path", "url"),
    [
        ("http://1.2.3.4", "", "/services", "http://1.2.3.4/services"),
        (
            "http://1.2.3.4/registry",
            "",
            "/services",
            "http://1.2.3.4/registry/services",
        ),
        (
            "http://1.2.3.4/registry/",
            "/app",
            "/services/",
            "http://1.2.3.4/registry/app/services",
        ),
        ("http://1.2.3.4/registry", "", "services", "http://1.2.3.4/registry/services"),
        (
            "http://1.2.3.4/registry",
            "",
            "/services/a b",
            "http://1.2.3.4/registry/services/a%20b",
        ),
        (
            "http://1.2.3.4/registry",
            "",
            "/services/a%2Fb?c#d",
            "http://1.2.3.4/registry/services/a%252Fb%3Fc%23d",
        ),
        (
            "http://1.2.3.4/registry",
            "",
            "/services/caf\u00e9",
            "http://1.2.3.4/registry/services/caf%C3%A9",
        ),
        (
            "http://1.2.3.4/a%20b",
            "",
            "/services",
            "http://1.2.3.4/a%20b/services",
        ),
        ("http://1.2.3.4", "", "", "http://1.2.3.4/"),
        ("http://1.2.3.4", "", "/", "http://1.2.3.4/"),
        ("http://1.2.3.4/registry", "", "", "http://1.2.3.4/registry"),
        ("http://1.2.3.4", "/app", "", "http://1.2.3.4/app"),
        ("https://1.2.3.4:8443/x", "", "/services", "https://1.2.3.4:8443/x/services"),
    ],
)
def test_get_public_url(public_url: str, root: str, path: str, url: str) -> None:
    """Public URLs have the path prefix of the public URL in front of the path.

    The prefix is followed by the root path of the app and the path,
    percent-encoded, with no trailing slash, except for the root of the host.
    """
    app = _app(public_url)
    with app.test_request_context(base_url=f"http://localhost{root}"):
        assert get_public_url(path) == url


@pytest.mark.parametrize(
    ("public_url", "url"),
    [
        ("http://[::1]/registry", "http://[::1]/registry/services"),
        ("http://[0::1]:8080", "http://[::1]:8080/services"),
        (
            "http://[fe80::1%25eth0]/registry",
            "http://[fe80::1%25eth0]/registry/services",
        ),
        ("http://Example.org./registry", "http://example.org/registry/services"),
        ("http://b\u00fccher.example", "http://xn--bcher-kva.example/services"),
    ],
)
def test_get_public_url_host(public_url: str, url: str) -> None:
    """Hosts are normalized, IPv6 addresses bracketed, with encoded zone IDs."""
    with _app(public_url).test_request_context():
        assert get_public_url("/services") == url


def test_get_public_url_default_path() -> None:
    """Without a path, the public URL of the root path of the app is returned."""
    with _app("https://api.example.org/my-app/").test_request_context():
        assert get_public_url() == "https://api.example.org/my-app"


def test_get_public_url_not_from_request() -> None:
    """Public URLs are not built from the request or its headers."""
    app = _app("https://api.example.org")
    with app.test_request_context(
        base_url="http://evil.example.org:8080",
        headers={"X-Forwarded-Host": "evil.example.org", "X-Forwarded-Proto": "http"},
    ):
        assert get_public_url("/services") == "https://api.example.org/services"


def test_get_public_url_unset() -> None:
    """Without a public URL, an error is raised."""
    with (
        _app(None).test_request_context(),
        pytest.raises(RuntimeError, match=r"server\.public_url"),
    ):
        get_public_url("/services")


def test_get_public_url_no_request_context() -> None:
    """Outside of a request context, an error is raised."""
    with _app("https://api.example.org").app_context(), pytest.raises(RuntimeError):
        get_public_url("/services")


@pytest.mark.parametrize(
    ("root", "path"),
    [
        ("", "/services/.."),
        ("", "/services/../admin"),
        ("", "./services"),
        ("", ".."),
        ("/app/..", "/services"),
    ],
)
def test_get_public_url_dot_segments(root: str, path: str) -> None:
    """Paths with dot segments are rejected."""
    app = _app("https://api.example.org/my-app")
    with (
        app.test_request_context(base_url=f"http://localhost{root}"),
        pytest.raises(ValueError, match="dot segments"),
    ):
        get_public_url(path)


@pytest.mark.parametrize("path", ["/services/a.b", "/services/...", "/.well-known"])
def test_get_public_url_dots(path: str) -> None:
    """Segments with dots that are not dot segments are kept."""
    with _app("https://api.example.org").test_request_context():
        assert get_public_url(path) == f"https://api.example.org{path}"
