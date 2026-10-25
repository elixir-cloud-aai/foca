"""Check and normalize URLs, e.g., of resources stored by an app.

Also builds public URLs of the app (cf. :py:func:`get_public_url`), e.g., for
`Location` headers.

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
# IPv6 zone identifiers (RFC 6874), after `%25`; unreserved characters only,
# without percent-encodings, as checked by `urlsplit()` of newer Python
# versions (3.11.4 and later)
_ZONE_ID = re.compile(r"[A-Za-z0-9\-._~]+")
# Labels of domain names (letters, digits, hyphens; RFC 1123, section 2.1)
_LDH_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
# Registered names (RFC 3986, section 3.2.2), without percent-encodings,
# which would let different spellings of a host (e.g., `ex%61mple.org`)
# and encoded control characters pass
_REG_NAME = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=]*")
# Label separators of host names: `.` and the full stops that IDNA (UTS 46)
# maps to it
_LABEL_SEPARATORS = re.compile(r"[.\u3002\uff0e\uff61]")
# Maximum lengths of host names, without a trailing dot, and of their labels
# (RFC 1035, section 2.3.4)
_MAX_HOST_LENGTH = 253
_MAX_LABEL_LENGTH = 63
# Maximum length of hosts before normalization: decomposed characters (NFD)
# take up to 4 code points (e.g., Hangul syllables 3)
_MAX_DECOMPOSED_LENGTH = 4 * (_MAX_HOST_LENGTH + 1)
# Last labels of hosts that WHATWG URL parsers (e.g., browsers) take as
# IPv4 addresses, e.g., `127.1` or `0x7f.0.0.1`
_NUMERIC_LABEL = re.compile(r"[0-9]+|0[xX][0-9A-Fa-f]*")
# Unreserved characters (RFC 3986, section 2.3)
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
# ASCII characters allowed in paths, queries and fragments (RFC 3986,
# section 3.3 to 3.5): unreserved characters, sub-delimiters, `:`, `@`, `/`
# and `?`, and `%` of percent-encodings; others (e.g., `<`, `|`, `\`)
# must be percent-encoded
_URI_CHARACTERS = _UNRESERVED | frozenset("!$&'()*+,;=:@/?%")
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
      (international host names, also `xn--` labels, must be valid as per
      IDNA 2008, UTS 46; without percent-encodings), an IPv4 address in
      dotted-decimal form (not, e.g., `127.1`), or an IPv6 address in
      brackets (with an optional zone identifier, as per RFC 6874). They
      must not have user info or an invalid port (or `0`).
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
      URL would differ from the one checked. Paths, queries and fragments
      must not contain ASCII characters that URLs only allow
      percent-encoded (RFC 3986; e.g., `<`, `|`, `[` or backslashes); non-ASCII
      characters are allowed (RFC 3987).

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
    canonical form, removes a trailing dot of hosts and default ports (`80`
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


def get_public_url(path: str = "") -> str:
    """Get the public URL of a path of the current app.

    Must be called in a request context. The URL is built from the public URL
    of the app (`server.public_url`, as configured, including any path prefix
    of a reverse proxy), followed by the root path of the app
    (`request.script_root`, e.g., set via `SCRIPT_NAME`) and `path`, both
    percent-encoded (UTF-8; all characters but unreserved ones and `/`, also
    `%`, so that percent-encodings in `path` are kept literally, e.g.,
    `%2e%2e` as `%252e%252e`). Slashes separate segments, also repeated ones
    (e.g., `a//b`), which are kept, except leading and trailing ones; a slash
    within a segment (e.g., of an identifier) cannot be expressed, so do not
    allow slashes in identifiers that are part of public URLs.

    The URL is not built from the request (e.g., its `Host` or
    `X-Forwarded-*` headers), as clients control these, unless a proxy sets
    them.

    Args:
        path: Path below the root path of the app (e.g., `request.path`),
            not percent-encoded; empty for the root path of the app. A
            leading slash is optional.

    Returns:
        Public URL, without trailing slash, unless it is the URL of the
        host's root (e.g., for an empty path, without path prefix and root
        path).

    Raises:
        RuntimeError: The app has no public URL (`server.public_url`), or
            there is no request context.
        InvalidURL: The root path of the app or `path` has dot segments
            (`.` or `..`), which clients would resolve, e.g., to URLs outside
            of the app. As `path` may come from clients (e.g., identifiers in
            the request path), FOCA's problem handler responds with `400 Bad
            Request` unless the app handles the exception. Also raised for
            paths with characters that cannot be encoded (unpaired
            surrogates).
    """
    # Flask only here, so that the other functions work without it (e.g.,
    # in `foca.plugins`)
    from flask import request  # noqa: PLC0415

    from foca.config.access import get_config  # noqa: PLC0415 (import cycle)

    public_url = get_config().server.public_url
    if public_url is None:
        raise RuntimeError(
            "No public URL of the app configured: set `server.public_url`"
        )
    root = request.script_root
    if any(segment in (".", "..") for segment in f"{root}/{path}".split("/")):
        raise InvalidURL("Paths of public URLs must not have dot segments.")
    parts = urlsplit(public_url)
    # The path prefix of `public_url` is normalized, i.e., encoded as it
    # appears in URLs, and is therefore not encoded again
    try:
        segments = (parts.path, quote(root), quote(path))
    except UnicodeEncodeError as exc:
        raise InvalidURL(
            "Paths of public URLs must not contain unpaired surrogates."
        ) from exc
    url_path = "/".join(filter(None, (segment.strip("/") for segment in segments)))
    return urlunsplit((parts.scheme, parts.netloc, f"/{url_path}", "", ""))


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
    # without an authority, `urlsplit()` does not raise
    if rest.startswith("//"):
        raise InvalidURL(f"{name} need valid email addresses.")
    parts = urlsplit(url)
    for part in (parts.path, parts.query, parts.fragment):
        _check_uri_characters(part, name=name)
        _check_percent_encodings(part, name=name)
    if not parts.path:
        raise InvalidURL(f"{name} need valid email addresses.")
    _check_addresses(parts.path, name=name)


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


def _check_uri_characters(text: str, name: str) -> None:
    """Check that a part of a URL has only characters that URLs allow.

    Args:
        text: Path, query or fragment of a URL; non-ASCII characters are
            allowed, as in internationalized URLs (RFC 3987).
        name: Name of the URLs in messages.

    Raises:
        InvalidURL: The text has ASCII characters that must be
            percent-encoded (cf. `_URI_CHARACTERS`).
    """
    if any(char.isascii() and char not in _URI_CHARACTERS for char in text):
        raise InvalidURL(
            f"{name} must not contain characters that URLs do not allow "
            "unencoded (e.g., '<', '|', '\\')."
        )


def _check_a_labels(host: str, message: str) -> None:
    """Check the IDNA A-labels (`xn--` labels) of a host.

    Args:
        host: Host, in ASCII.
        message: Message of the exception.

    Raises:
        InvalidURL: An A-label is not valid as per IDNA 2008 (e.g., `xn--a`).
    """
    for label in host.lower().split("."):
        if label.startswith("xn--"):
            try:
                idna.decode(label)
            except idna.IDNAError as exc:
                raise InvalidURL(message) from exc


def _check_host_length(host: str, message: str) -> None:
    """Check the length of a host name and of its labels, before encoding.

    IDNA encoding takes time quadratic in the length of the host in some
    versions of `idna` (before 3.15, CVE-2026-45409), so long hosts are
    rejected before they are encoded. Labels are separated by `.` and the
    full stops that IDNA maps to it (e.g., U+3002, ideographic full stop);
    as international labels get longer when encoded (`xn--` labels), hosts
    are only rejected if they would be too long anyway, except for labels
    with characters that IDNA ignores (e.g., variation selectors).

    Args:
        host: Host, possibly with international labels, and a trailing dot.
        message: Message of the exception.

    Raises:
        InvalidURL: The host, without a trailing dot, is longer than 253
            characters, or one of its labels is longer than 63.
    """
    # Composed as by IDNA, so that combining marks (e.g., of decomposed
    # accented letters) do not count separately
    # Hosts that are too long even decomposed are rejected before
    # normalization, which takes time quadratic in the length of sequences
    # of combining marks
    if len(host) > _MAX_DECOMPOSED_LENGTH:
        raise InvalidURL(message)
    labels = _LABEL_SEPARATORS.split(unicodedata.normalize("NFC", host))
    if len(labels) > 1 and not labels[-1]:
        labels.pop()
    if sum(map(len, labels)) + len(labels) - 1 > _MAX_HOST_LENGTH or any(
        len(label) > _MAX_LABEL_LENGTH for label in labels
    ):
        raise InvalidURL(message)


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
            only digits, and at most 253 characters (also before encoding,
            cf. `_check_host_length()`).
    """
    message = f"{name} need email addresses with a domain name."
    _check_host_length(domain, message=message)
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
    _check_a_labels(domain, message=message)


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
            `_check_characters()` (joiners only in the host) or
            `_check_uri_characters()`, malformed percent-encodings, no
            scheme or an invalid or no host, a scheme that is not allowed,
            user info, or an invalid port (or `0`).
    """
    _check_characters(url, name=name, allowed=_JOINERS)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise InvalidURL(f"{name} must be valid URLs: {exc}.") from exc
    for part in (parts.path, parts.query, parts.fragment):
        _check_characters(part, name=name)
        _check_uri_characters(part, name=name)
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
        Host without a trailing dot; IPv6 addresses in canonical form, in
        brackets; international host names encoded (IDNA 2008, UTS 46).

    Raises:
        InvalidURL: The host is invalid or empty, e.g., an IP literal in
            brackets other than an IPv6 address (e.g., IPvFuture), an IPv6
            address with a `%` not followed by `25` and a zone identifier
            (`_ZONE_ID`, non-empty), a host with percent-encodings or empty
            labels (other than a trailing dot), a host longer than 253
            characters or with labels longer than 63 (cf.
            `_check_host_length()`), or a host whose last label is numeric
            (cf. `_NUMERIC_LABEL`) but that is not an IPv4 address in
            dotted-decimal form.
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
    _check_host_length(host, message=f"{name} need a valid host: too long.")
    if not host.isascii():
        try:
            host = idna.encode(host, uts46=True, transitional=False).decode()
        except idna.IDNAError as exc:
            raise InvalidURL(f"{name} need a valid host: {exc}.") from exc
    if not _REG_NAME.fullmatch(host):
        raise InvalidURL(f"{name} need a valid host: '{host}'.")
    _check_a_labels(host, message=f"{name} need a valid host: '{host}'.")
    # One trailing dot (of a fully qualified domain name) is removed; empty
    # labels otherwise (e.g., `a..org`, `.a.org`, `a.org..`) are rejected, as
    # removing them would make different hosts equal
    host = host.removesuffix(".")
    if not host:
        raise InvalidURL(f"{name} need a scheme and a host.")
    if "" in host.split("."):
        raise InvalidURL(f"{name} need a valid host: '{host}'.")
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
