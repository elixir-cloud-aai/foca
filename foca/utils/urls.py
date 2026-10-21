"""Check and normalize URLs, e.g., of resources stored by an app.

Apps decide which of their fields hold URLs, and which schemes each field
allows (cf. :py:func:`check_url_fields`): FOCA does not know their data.
Supported schemes are `http` and `https`, with a host, and `mailto`, with
email addresses (e.g., for contact URLs, as in the GA4GH service info
specification).
"""

import ipaddress
import re
import string
import unicodedata
from collections.abc import Collection, Mapping
from typing import Any, Literal, get_args
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit

import idna

URLScheme = Literal["http", "https", "mailto"]
"""Supported URL schemes, e.g., for app configuration models."""

SUPPORTED_SCHEMES: frozenset[str] = frozenset(get_args(URLScheme))
"""Schemes that URLs can be checked for: `http`, `https` and `mailto`."""
HTTP_SCHEMES: frozenset[str] = frozenset({"http", "https"})
"""Schemes of HTTP(S) URLs: `http` and `https`."""

# Default ports of HTTP(S) URLs, removed by normalization
_DEFAULT_PORTS = {"http": 80, "https": 443}
# Categories of characters rejected in URLs (cf. `_check_characters()`):
# control and format characters, surrogates (unpaired; they cannot be
# encoded) and separators
_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Zs"})
# Format characters allowed in international host names, where IDNA 2008
# allows them in some contexts (CONTEXTJ): zero width non-joiner and joiner
_JOINERS = frozenset("\u200c\u200d")
_PERCENT_ENCODED = re.compile(r"%([0-9A-Fa-f]{2})")
# Percent signs not starting a percent-encoding
_MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
# IPv6 zone identifiers (RFC 6874), after `%25`
_ZONE_ID = re.compile(r"(?:[A-Za-z0-9\-._~]|%[0-9A-Fa-f]{2})+")
# Labels of domain names (letters, digits, hyphens; RFC 1123, section 2.1)
_LDH_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
# Registered names (RFC 3986, section 3.2.2), without percent-encodings,
# which would let different spellings of a host (e.g., `ex%61mple.org`)
# and encoded control characters pass
_REG_NAME = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=]*")
# Last labels of hosts that WHATWG URL parsers (e.g., browsers) take as
# IPv4 addresses, e.g., `127.1` or `0x7f.0.0.1`
_NUMERIC_LABEL = re.compile(r"[0-9]+|0[xX][0-9A-Fa-f]*")
# Unreserved characters (RFC 3986, section 2.3)
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
# Printable ASCII characters, kept when percent-encoding non-ASCII ones
_ASCII_PRINTABLE = "".join(chr(code) for code in range(33, 127))


class InvalidURL(ValueError):
    """A URL is invalid or uses a scheme that is not allowed.

    The message says why, naming the URLs as passed to the check (e.g.,
    `URLs in 'contactUrl' need valid email addresses.`), so that it can be
    returned to clients. Unless an app maps the exception to other problem
    details (cf. `exceptions.mapping`), FOCA's problem handler responds with
    `400 Bad Request`.

    Attributes:
        code: HTTP status code of responses to requests with invalid URLs.
    """

    code = 400


def check_url(
    url: object,
    schemes: Collection[str] = HTTP_SCHEMES,
    *,
    name: str = "URLs",
) -> None:
    """Check that a URL is valid and uses one of the allowed schemes.

    URLs are checked as written, without normalizing them (cf.
    :py:func:`normalize_url`), so that they can be stored as written:

    * `http` and `https` URLs need a valid host: a domain or registered name
      (international host names must be valid as per IDNA 2008, UTS 46;
      without percent-encodings), an IPv4 address in dotted-decimal form (not,
      e.g., `127.1`), or an IPv6 address in brackets (with an optional zone
      identifier, as per RFC 6874). They must not have user info or an
      invalid port (or `0`).
    * `mailto` URLs (RFC 6068) need comma-separated email addresses, each
      with a local part and a domain name (at least two labels of letters,
      digits and hyphens after IDNA encoding, the last one not only digits,
      at most 253 characters), and must not have an authority (`//`). Addresses
      are not checked as per RFC 5322.
    * URLs must not contain whitespace (also leading or trailing; strip it
      beforehand to tolerate it), control or format characters (e.g.,
      bidirectional controls, soft hyphens), except zero width (non-)joiners
      in international host names, or malformed percent-encodings:
      `urllib.parse.urlsplit()` would strip some of them, so that a stored
      URL would differ from the one checked.

    Args:
        url: URL.
        schemes: Allowed schemes, lowercase; a subset of `SUPPORTED_SCHEMES`
            (`http`, `https`, `mailto`). Schemes of URLs are compared
            case-insensitively.
        name: Name of the URLs in messages, e.g., `Service URLs` or
            `URLs in 'contactUrl'`.

    Raises:
        InvalidURL: The URL is not a string, is invalid, or uses a scheme
            that is not allowed.
        ValueError: `schemes` is empty or has unsupported or uppercase
            schemes (not an `InvalidURL`, as this is an error of the app).
    """
    allowed = _check_schemes(schemes)
    url = _check_string(url, name=name)
    scheme, colon, rest = url.partition(":")
    if colon and scheme.lower() == "mailto" and "mailto" in allowed:
        _check_mailto(url, rest=rest, name=name)
        return
    _split_http_url(url, schemes=allowed, name=name)


def normalize_url(
    url: object,
    schemes: Collection[str] = HTTP_SCHEMES,
    *,
    name: str = "URLs",
    keep_query: bool = False,
    keep_fragment: bool = False,
) -> str:
    """Check an HTTP(S) URL and normalize it, e.g., to compare base URLs.

    The URL is checked like by :py:func:`check_url`. Normalization (cf.
    RFC 3986, section 6.2.2) lowercases the scheme and host, encodes
    international host names (IDNA 2008, UTS 46), writes IPv6 addresses in
    canonical form, removes trailing dots of hosts and default ports (`80`
    for `http`, `443` for `https`), percent-encodes non-ASCII characters
    (UTF-8), decodes percent-encoded unreserved characters, uppercases other
    percent-encodings, and resolves dot segments. Repeated and trailing
    slashes of paths are removed, and so are queries and fragments, unless
    kept, so that spellings of the same base URL (e.g., of a service) are
    equal; `http` and `https` URLs differ. Empty queries and fragments
    (e.g., `?`) are removed in any case.

    Args:
        url: HTTP(S) URL.
        schemes: Allowed schemes, lowercase; a subset of `HTTP_SCHEMES`
            (e.g., `["https"]`).
        name: Name of the URLs in messages, e.g., `Service URLs`.
        keep_query: Whether to keep the query, with normalized
            percent-encodings.
        keep_fragment: Whether to keep the fragment, with normalized
            percent-encodings.

    Returns:
        Normalized URL.

    Raises:
        InvalidURL: The URL is not a string, is invalid, or uses a scheme
            that is not allowed (cf. :py:func:`check_url`).
        ValueError: `schemes` is empty or has schemes other than `http` and
            `https` (not an `InvalidURL`, as this is an error of the app).
    """
    allowed = _check_schemes(schemes, supported=HTTP_SCHEMES)
    url = _check_string(url, name=name)
    parts, host = _split_http_url(url, schemes=allowed, name=name)
    scheme = parts.scheme.lower()
    if parts.port is not None and parts.port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{parts.port}"
    return urlunsplit(
        (
            scheme,
            host,
            _normalize_path(parts.path),
            _normalize_percent_encodings(parts.query) if keep_query else "",
            _normalize_percent_encodings(parts.fragment) if keep_fragment else "",
        )
    )


def check_url_fields(
    data: Mapping[str, Any],
    fields: Mapping[str, Collection[str]],
) -> None:
    """Check the URLs in fields of an object, e.g., of a request body.

    Args:
        data: Object, e.g., a resource.
        fields: Allowed schemes (cf. :py:func:`check_url`) by field, as
            paths of keys separated by dots (e.g., `organization.url`).
            Missing fields (also if a parent is missing or not an object)
            are not checked; fields set to `None` are checked, i.e.,
            rejected.

    Raises:
        InvalidURL: The URL in a field is invalid or uses a scheme that is
            not allowed for the field; the message names the field (e.g.,
            `URLs in 'organization.url' must be strings.`).
        ValueError: The schemes of a field are empty or unsupported (cf.
            :py:func:`check_url`), also if the field is missing.
    """
    for field, schemes in fields.items():
        allowed = _check_schemes(schemes)
        value: object = data
        for key in field.split("."):
            if not isinstance(value, Mapping) or key not in value:
                break
            value = value[key]
        else:
            check_url(value, schemes=allowed, name=f"URLs in '{field}'")


def _check_schemes(
    schemes: Collection[str], supported: frozenset[str] = SUPPORTED_SCHEMES
) -> frozenset[str]:
    """Check that allowed schemes are supported.

    Args:
        schemes: Allowed schemes.
        supported: Supported schemes.

    Returns:
        Allowed schemes, as a set (e.g., of the items of an iterator).

    Raises:
        ValueError: `schemes` is a string or empty, or has schemes that are
            not in `supported` (e.g., uppercase ones).
    """
    if isinstance(schemes, str):
        raise ValueError(f"Allowed URL schemes must be a collection: {schemes!r}")
    allowed = frozenset(schemes)
    if not allowed:
        raise ValueError("Allowed URL schemes must not be empty")
    unsupported = allowed - supported
    if unsupported:
        raise ValueError(
            f"Unsupported URL schemes: {sorted(unsupported)}; supported schemes: "
            f"{sorted(supported)}"
        )
    return allowed


def _check_string(url: object, name: str) -> str:
    """Check that a URL is a string.

    Args:
        url: URL.
        name: Name of the URLs in messages.

    Returns:
        URL.

    Raises:
        InvalidURL: The URL is not a string.
    """
    if not isinstance(url, str):
        raise InvalidURL(f"{name} must be strings.")
    return url


def _check_mailto(url: str, rest: str, name: str) -> None:
    """Check a `mailto` URL.

    Args:
        url: URL.
        rest: URL after the scheme and colon.
        name: Name of the URLs in messages.

    Raises:
        InvalidURL: The URL is invalid (cf. :py:func:`check_url`).
    """
    _check_characters(url, name=name)
    _check_percent_encodings(url, name=name)
    path = urlsplit(url).path
    if rest.startswith("//") or not path:
        raise InvalidURL(f"{name} need valid email addresses.")
    _check_addresses(path, name=name)


def _check_characters(
    url: str, name: str, allowed: frozenset[str] = frozenset()
) -> None:
    """Check that a URL has no whitespace, control or format characters.

    Args:
        url: URL, or part of it.
        name: Name of the URLs in messages.
        allowed: Characters allowed although in `_REJECTED_CATEGORIES` (e.g.,
            `_JOINERS` in hosts, which the IDNA check validates).

    Raises:
        InvalidURL: The URL has whitespace, control or format characters,
            other than `allowed` ones.
    """
    if any(
        char not in allowed
        and (char.isspace() or unicodedata.category(char) in _REJECTED_CATEGORIES)
        for char in url
    ):
        raise InvalidURL(
            f"{name} must not contain whitespace, control or format characters."
        )


def _check_percent_encodings(text: str, name: str) -> None:
    """Check that the percent signs in a URL, or part of it, start encodings.

    Args:
        text: URL, or part of it.
        name: Name of the URLs in messages.

    Raises:
        InvalidURL: The text has malformed percent-encodings.
    """
    if _MALFORMED_PERCENT.search(text):
        raise InvalidURL(f"{name} must not contain malformed percent-encodings.")


def _check_addresses(path: str, name: str) -> None:
    """Check the email addresses of a `mailto` URL, minimally.

    Args:
        path: Path of the URL, with comma-separated addresses.
        name: Name of the URLs in messages.

    Raises:
        InvalidURL: An address has no local part, one with `@` (unless
            percent-encoded), or no domain name (cf. `_check_domain()`).
    """
    for address in path.split(","):
        local, _, domain = address.rpartition("@")
        if not local or "@" in local or not domain:
            raise InvalidURL(f"{name} need valid email addresses.")
        _check_domain(domain, name=name)


def _check_domain(domain: str, name: str) -> None:
    """Check the domain of an email address.

    Args:
        domain: Domain.
        name: Name of the URLs in messages.

    Raises:
        InvalidURL: The domain is not a domain name (e.g., an IP address,
            or not valid as per IDNA 2008): after IDNA encoding, it needs at
            least two labels of letters, digits and hyphens (1 to 63
            characters, no leading or trailing hyphen), the last one not
            only digits, and at most 253 characters.
    """
    message = f"{name} need email addresses with a domain name."
    if not domain.isascii():
        try:
            domain = idna.encode(domain, uts46=True, transitional=False).decode()
        except idna.IDNAError as exc:
            raise InvalidURL(message) from exc
    labels = domain.split(".")
    if (
        len(domain) > 253
        or len(labels) < 2
        or labels[-1].isdigit()
        or not all(_LDH_LABEL.fullmatch(label) for label in labels)
    ):
        raise InvalidURL(message)


def _split_http_url(
    url: str, schemes: frozenset[str], name: str
) -> tuple[SplitResult, str]:
    """Split an HTTP(S) URL, checking it.

    Args:
        url: URL.
        schemes: Allowed schemes; only `http` and `https` are accepted.
        name: Name of the URLs in messages.

    Returns:
        Parts of the URL, and its normalized host (cf. `_normalize_host()`).

    Raises:
        InvalidURL: The URL is invalid, has characters rejected by
            `_check_characters()` (joiners only in the host), malformed
            percent-encodings, no scheme or an invalid or no host, a scheme
            that is not allowed, user info, or an invalid port (or `0`).
    """
    _check_characters(url, name=name, allowed=_JOINERS)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise InvalidURL(f"{name} must be valid URLs: {exc}.") from exc
    for part in (parts.path, parts.query, parts.fragment):
        _check_characters(part, name=name)
        _check_percent_encodings(part, name=name)
    if parts.scheme and parts.scheme.lower() not in schemes & HTTP_SCHEMES:
        allowed = ", ".join(f"'{scheme}'" for scheme in sorted(schemes))
        raise InvalidURL(f"{name} must use one of these schemes: {allowed}.")
    if not parts.scheme or not parts.hostname:
        raise InvalidURL(f"{name} need a scheme and a host.")
    if parts.username is not None or parts.password is not None:
        raise InvalidURL(f"{name} must not contain user info.")
    if port == 0:
        raise InvalidURL(f"{name} need a valid port.")
    # Without user info, the host is in brackets if the authority starts
    # with one (e.g., an IPv6 address)
    bracketed = parts.netloc.startswith("[")
    return parts, _normalize_host(parts.hostname, bracketed=bracketed, name=name)


def _normalize_host(host: str, bracketed: bool, name: str) -> str:
    """Normalize the host of a URL.

    Args:
        host: Lowercase host, without brackets (cf.
            `urllib.parse.SplitResult.hostname`).
        bracketed: Whether the host is in brackets in the URL.
        name: Name of the URLs in messages.

    Returns:
        Host without trailing dots; IPv6 addresses in canonical form, in
        brackets; international host names encoded (IDNA 2008, UTS 46).

    Raises:
        InvalidURL: The host is invalid or empty, e.g., an IP literal in
            brackets other than an IPv6 address (e.g., IPvFuture), an IPv6
            address with a `%` not followed by `25` and a zone identifier
            (`_ZONE_ID`, non-empty), a host with percent-encodings, or a host
            whose last label is numeric (cf. `_NUMERIC_LABEL`) but that is not
            an IPv4 address in dotted-decimal form.
    """
    if bracketed:
        address, percent, zone = host.partition("%")
        try:
            address = ipaddress.IPv6Address(address).compressed
        except ValueError as exc:
            raise InvalidURL(f"{name} need a valid host: {exc}.") from exc
        if not percent:
            return f"[{address}]"
        if not zone.startswith("25") or not _ZONE_ID.fullmatch(zone[2:]):
            raise InvalidURL(f"{name} need a valid host: '{host}'.")
        return f"[{address}%{zone}]"
    if not host.isascii():
        try:
            host = idna.encode(host, uts46=True, transitional=False).decode()
        except idna.IDNAError as exc:
            raise InvalidURL(f"{name} need a valid host: {exc}.") from exc
    if not _REG_NAME.fullmatch(host):
        raise InvalidURL(f"{name} need a valid host: '{host}'.")
    host = host.rstrip(".")
    if not host:
        raise InvalidURL(f"{name} need a scheme and a host.")
    if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError as exc:
            raise InvalidURL(f"{name} need a valid host: {exc}.") from exc
    return host


def _normalize_path(path: str) -> str:
    """Normalize the path of a URL (cf. RFC 3986, section 6.2.2).

    Args:
        path: URL path, without malformed percent-encodings (cf.
            `_split_http_url()`).

    Returns:
        Path with normalized percent-encodings (cf.
        `_normalize_percent_encodings()`), dot segments resolved, and without
        repeated and trailing slashes.
    """
    segments: list[str] = []
    for segment in _normalize_percent_encodings(path).split("/"):
        if segment == "..":
            if segments:
                segments.pop()
        elif segment not in ("", "."):
            segments.append(segment)
    return "".join(f"/{segment}" for segment in segments)


def _normalize_percent_encodings(text: str) -> str:
    """Normalize the percent-encodings of a part of a URL.

    Args:
        text: Part of a URL, without malformed percent-encodings.

    Returns:
        Text with non-ASCII characters percent-encoded (UTF-8), unreserved
        characters decoded, and other percent-encodings in uppercase.
    """
    return _PERCENT_ENCODED.sub(_decode_unreserved, quote(text, safe=_ASCII_PRINTABLE))


def _decode_unreserved(match: re.Match[str]) -> str:
    """Decode a percent-encoded character if it is unreserved.

    Args:
        match: Match of a percent-encoded character (`_PERCENT_ENCODED`).

    Returns:
        The character if it is unreserved, otherwise its percent-encoding in
        uppercase.
    """
    char = chr(int(match.group(1), 16))
    return char if char in _UNRESERVED else match.group(0).upper()
