"""Tests of the credentials of backends (`foca.plugins.backends.credentials`)."""

import base64
import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import requests
from pydantic import ValidationError

from foca.plugins import Backend
from foca.plugins.backends import (
    NO_CREDENTIALS,
    BackendList,
    BackendListConfig,
    BackendSession,
    ClientCredentialsConfig,
    Credentials,
    CredentialsError,
    ServiceEntry,
    TokenProvider,
    check_backends,
    check_client_credentials,
    get_credentials,
    get_env,
    token_audience,
    uses_tokens,
)
from foca.plugins.backends import credentials as module

ISSUER = "https://login.example.org/"
DISCOVERY_URL = "https://login.example.org/.well-known/openid-configuration"
TOKEN_ENDPOINT = "https://login.example.org/token"  # noqa: S105 (a URL)
CLIENT_ID_ENV = "GATEWAY_CLIENT_ID"
CLIENT_SECRET_ENV = "GATEWAY_CLIENT_SECRET"  # noqa: S105 (a variable name)
CLIENT_ID = "gateway client"
CLIENT_SECRET = "s3cret:&"  # noqa: S105 (a test value)
BACKEND_URL = "https://tes.example.org"
REGISTRY_URL = "https://registry.example.org/ga4gh/registry/v1"


class FakeResponse:
    """Response of the fake identity provider.

    Attributes:
        body: JSON body; `None` for an invalid one.
        status_code: Status code.
        is_redirect: Whether the response is a redirect.
    """

    def __init__(self, body: Any, status_code: int = 200) -> None:
        """Create a response.

        Args:
            body: JSON body; `None` for an invalid one.
            status_code: Status code.
        """
        self.body = body
        self.status_code = status_code
        self.is_redirect = 300 <= status_code < 400

    def raise_for_status(self) -> None:
        """Raise an error for error status codes, as `requests` does.

        Raises:
            requests.HTTPError: The status code is 400 or higher.
        """
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)  # type: ignore[arg-type]

    def json(self) -> Any:
        """Get the JSON body.

        Returns:
            The body.

        Raises:
            ValueError: The body is invalid.
        """
        if self.body is None:
            raise ValueError("invalid JSON")
        return self.body


class FakeIdp:
    """Fake identity provider: discovery and token endpoint.

    Attributes:
        discovery: Discovery document served.
        token: Token response served.
        token_status: Status of token responses.
        requests: Requests to the token endpoint (keyword arguments).
        discoveries: Requests of the discovery document (keyword arguments).
        hook: Called with the keyword arguments of each token request
            before it is answered, if set.
    """

    def __init__(self) -> None:
        """Create the identity provider, issuing tokens for 300 seconds."""
        self.discovery: Any = {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT}
        self.token: Any = {
            "access_token": "token-1",
            "token_type": "Bearer",
            "expires_in": 300,
        }
        self.token_status = 200
        self.requests: list[dict[str, Any]] = []
        self.discoveries: list[dict[str, Any]] = []
        self.hook: Any = None

    def send(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        """Serve the discovery document and the token endpoint.

        Valid tokens are numbered by request.

        Args:
            method: HTTP method.
            url: URL.
            **kwargs: Other arguments of the request.

        Returns:
            The response.

        Raises:
            requests.ConnectionError: The URL is neither.
        """
        if method == "GET" and url == DISCOVERY_URL:
            self.discoveries.append(kwargs)
            return FakeResponse(self.discovery)
        if method == "POST" and url == TOKEN_ENDPOINT:
            if self.hook is not None:
                self.hook(kwargs)
            self.requests.append(kwargs)
            body = self.token
            if isinstance(body, dict) and body.get("access_token") == "token-1":
                body = {**body, "access_token": f"token-{len(self.requests)}"}
            return FakeResponse(body, status_code=self.token_status)
        raise requests.ConnectionError(url)


class Clock:
    """Fake monotonic clock.

    Attributes:
        now: Current time.
    """

    def __init__(self) -> None:
        """Start at 1000."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Get the current time.

        Returns:
            The time.
        """
        return self.now


@pytest.fixture
def provider() -> TokenProvider:
    """Token provider of a gateway, with an empty cache."""
    return TokenProvider(CLIENT_ID_ENV, CLIENT_SECRET_ENV)


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> FakeIdp:
    """Fake identity provider, with the gateway's client in the environment."""
    fake = FakeIdp()
    monkeypatch.setattr(TokenProvider, "_send", staticmethod(fake.send))
    monkeypatch.setenv(CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(CLIENT_SECRET_ENV, CLIENT_SECRET)
    return fake


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Fake clock of the token cache."""
    fake = Clock()
    monkeypatch.setattr(module.time, "monotonic", fake)
    return fake


def client(**settings: Any) -> ClientCredentialsConfig:
    """Get a client of the gateway.

    Args:
        **settings: Settings; by default, the issuer.

    Returns:
        The client.
    """
    return ClientCredentialsConfig(**(settings or {"issuer": ISSUER}))


# Config


def test_client_defaults() -> None:
    """The client has defaults for all but the identity provider."""
    conf = ClientCredentialsConfig()
    assert conf.issuer is None
    assert conf.token_endpoint is None
    assert conf.url_schemes == ["https"]
    assert conf.auth_method == "client_secret_basic"
    assert conf.refresh_before == 60
    assert conf.timeout == 10


@pytest.mark.parametrize(
    "settings",
    [
        {"issuer": "http://login.example.org/"},
        {"token_endpoint": "http://login.example.org/token"},
        {"issuer": "https://user:hunter2@login.example.org/"},
        {"token_endpoint": "https:///token"},
        {"issuer": "ftp://login.example.org/", "url_schemes": ["https", "http"]},
    ],
)
def test_client_urls_invalid(settings: dict[str, Any]) -> None:
    """URLs of the identity provider are checked, and left out of errors."""
    with pytest.raises(ValidationError, match="invalid, with user info") as exc_info:
        ClientCredentialsConfig(**settings)
    assert "hunter2" not in str(exc_info.value)


def test_client_http_allowed() -> None:
    """`http` URLs of the identity provider can be allowed, e.g., for tests."""
    conf = ClientCredentialsConfig(
        issuer="http://login.example.org/", url_schemes=["https", "http"]
    )
    assert conf.issuer == "http://login.example.org/"


@pytest.mark.parametrize(
    "settings",
    [{"timeout": 0}, {"refresh_before": 0}, {"scope": ""}, {"unknown": 1}],
)
def test_client_settings_invalid(settings: dict[str, Any]) -> None:
    """Invalid and unknown settings are refused."""
    with pytest.raises(ValidationError):
        ClientCredentialsConfig(**settings)


def test_entry_audience() -> None:
    """Entries of backends may have an audience, with `client_credentials`."""
    entry = ServiceEntry(type="ga4gh-tes", url=BACKEND_URL, audience="tes-client")
    assert entry.audience == "tes-client"


@pytest.mark.parametrize(
    "settings, match",
    [
        ({"auth": "none", "audience": "tes"}, "needs `auth: client_credentials`"),
        ({"audience": " tes"}, "surrounding whitespace"),
    ],
)
def test_entry_audience_invalid(settings: dict[str, Any], match: str) -> None:
    """Audiences need token authentication and no surrounding whitespace."""
    with pytest.raises(ValidationError, match=match):
        ServiceEntry(type="ga4gh-tes", url=BACKEND_URL, **settings)


def entries(*items: dict[str, Any]) -> BackendListConfig:
    """Get a backend list.

    Args:
        *items: Entries.

    Returns:
        The list.
    """
    return BackendListConfig.model_validate(list(items))


def test_uses_tokens() -> None:
    """Lists use tokens if an entry has `auth: client_credentials`."""
    assert uses_tokens(entries({"type": "ga4gh-tes", "url": BACKEND_URL}))
    none = entries({"type": "ga4gh-tes", "url": BACKEND_URL, "auth": "none"})
    assert not uses_tokens(none)
    assert not uses_tokens(none.root)


def test_check_client_credentials() -> None:
    """Lists with tokens and audiences fit clients that request them."""
    listed = entries(
        {"type": "ga4gh-tes", "url": BACKEND_URL, "audience": "https://tes/"},
        {"type": "ga4gh-service-registry", "url": REGISTRY_URL},
    )
    check_client_credentials(
        listed, client(issuer=ISSUER, audience_parameter="resource")
    )
    check_client_credentials(
        listed.root,
        client(token_endpoint=TOKEN_ENDPOINT, audience_parameter="audience"),
    )
    # without tokens, no identity provider is needed
    check_client_credentials(
        entries({"type": "ga4gh-tes", "url": BACKEND_URL, "auth": "none"}),
        ClientCredentialsConfig(),
    )


@pytest.mark.parametrize(
    "entry, conf, match",
    [
        ({}, {}, "need the `issuer` or `token_endpoint`"),
        ({"audience": "tes"}, {"issuer": ISSUER}, "needs `audience_parameter`"),
        (
            {"audience": "tes"},
            {"issuer": ISSUER, "audience_parameter": "resource"},
            "absolute URI",
        ),
        (
            {"audience": "https://tes.example.org/?a=b"},
            {"issuer": ISSUER, "audience_parameter": "resource"},
            "absolute URI",
        ),
        (
            {"audience": "https://tes.example.org:99999/"},
            {"issuer": ISSUER, "audience_parameter": "resource"},
            "absolute URI",
        ),
        (
            {"audience": "urn:"},
            {"issuer": ISSUER, "audience_parameter": "resource"},
            "absolute URI",
        ),
    ],
)
def test_check_client_credentials_invalid(
    entry: dict[str, Any], conf: dict[str, Any], match: str
) -> None:
    """Lists that don't fit the client are refused."""
    listed = entries({"type": "ga4gh-tes", "url": BACKEND_URL, **entry})
    with pytest.raises(ValueError, match=match):
        check_client_credentials(listed, ClientCredentialsConfig(**conf))


def test_check_client_credentials_urn() -> None:
    """Resource indicators may be URNs, e.g., of the backend's client."""
    listed = entries({"type": "ga4gh-tes", "url": BACKEND_URL, "audience": "urn:x:tes"})
    check_client_credentials(
        listed, client(issuer=ISSUER, audience_parameter="resource")
    )


# Environment


def test_get_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials come from the environment, and must not be empty."""
    monkeypatch.setenv("SOME_SECRET", "value")
    assert get_env("SOME_SECRET") == "value"
    monkeypatch.setenv("SOME_SECRET", "")
    with pytest.raises(CredentialsError, match="SOME_SECRET is not set"):
        get_env("SOME_SECRET")
    monkeypatch.delenv("SOME_SECRET")
    with pytest.raises(CredentialsError, match="SOME_SECRET is not set"):
        get_env("SOME_SECRET")


# Credentials


def test_credentials_bearer() -> None:
    """Bearer credentials set the header, and their representation hides them."""
    creds = Credentials.bearer("abc.def")
    assert creds.scheme == "Bearer"
    assert creds.authorization == "Bearer abc.def"
    assert creds.headers == {"Authorization": "Bearer abc.def"}
    assert "abc" not in repr(creds)
    assert repr(creds) == "Credentials(Bearer)"
    assert creds == Credentials.bearer("abc.def")
    assert creds != Credentials.bearer("other")
    assert creds != "Bearer abc.def"
    assert hash(creds) == hash(Credentials.bearer("abc.def"))


@pytest.mark.parametrize("token", ["", "a b", "a\r\nX-Injected: 1", "töken"])
def test_credentials_bearer_invalid(token: str) -> None:
    """Tokens that don't fit in a header are refused."""
    with pytest.raises(CredentialsError, match="visible ASCII"):
        Credentials.bearer(token)


def test_credentials_basic() -> None:
    """HTTP Basic credentials are encoded as UTF-8 (RFC 7617)."""
    creds = Credentials.basic("usér", "p:ss")
    assert creds.scheme == "Basic"
    encoded = base64.b64encode("usér:p:ss".encode()).decode()
    assert creds.authorization == f"Basic {encoded}"
    assert repr(creds) == "Credentials(Basic)"


@pytest.mark.parametrize(
    "username, password, match",
    [
        ("a:b", "c", "colon"),
        ("a\n", "c", "control characters"),
        ("a", "c\x00", "control characters"),
    ],
)
def test_credentials_basic_invalid(username: str, password: str, match: str) -> None:
    """User names with colons, and control characters, are refused."""
    with pytest.raises(CredentialsError, match=match):
        Credentials.basic(username, password)


def test_credentials_value_needs_scheme() -> None:
    """Credentials have a value with, and only with, a scheme."""
    with pytest.raises(ValueError, match="value with, and only with"):
        Credentials("Bearer")
    with pytest.raises(ValueError, match="value with, and only with"):
        Credentials(value="x")


def test_no_credentials() -> None:
    """No credentials remove the `Authorization` header."""
    assert NO_CREDENTIALS.authorization is None
    assert NO_CREDENTIALS.headers == {}
    assert repr(NO_CREDENTIALS) == "Credentials(none)"
    headers = {"authorization": "Bearer caller", "Accept": "x"}
    NO_CREDENTIALS.apply(headers)
    assert headers == {"Accept": "x"}


def test_credentials_replace_caller_header() -> None:
    """Credentials replace the caller's `Authorization` header, in any case."""
    request = requests.Request(
        "GET", BACKEND_URL, headers={"AUTHORIZATION": "Bearer caller"}
    ).prepare()
    Credentials.bearer("own")(request)
    assert dict(request.headers) == {"Authorization": "Bearer own"}
    plain = {"Authorization": "Bearer caller", "authorization": "Bearer x"}
    Credentials.bearer("own").apply(plain)
    assert plain == {"Authorization": "Bearer own"}


# Token provider


def test_provider_env_names() -> None:
    """Providers need two different, non-empty variable names."""
    with pytest.raises(ValueError, match="Names"):
        TokenProvider("", CLIENT_SECRET_ENV)
    with pytest.raises(ValueError, match="of their own"):
        TokenProvider(CLIENT_ID_ENV, CLIENT_ID_ENV)
    provider = TokenProvider(CLIENT_ID_ENV, CLIENT_SECRET_ENV)
    assert repr(provider) == (
        f"TokenProvider(client_id_env={CLIENT_ID_ENV!r},"
        f" client_secret_env={CLIENT_SECRET_ENV!r})"
    )


def test_provider_missing_env(
    provider: TokenProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Providers tell which variables of their client are not set."""
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    monkeypatch.setenv(CLIENT_SECRET_ENV, "x")
    assert provider.missing_env() == [CLIENT_ID_ENV]


def test_token_discovered(idp: FakeIdp, clock: Clock, provider: TokenProvider) -> None:
    """Tokens are fetched from the issuer's token endpoint, as the client.

    The client authenticates with HTTP Basic (RFC 6749, section 2.3.1); the
    request is not redirected; the discovery document is read without
    credentials.
    """
    assert provider.get_token(client()) == "token-1"
    (request,) = idp.requests
    assert request["data"] == {"grant_type": "client_credentials"}
    assert request["auth"] == ("gateway+client", "s3cret%3A%26")
    assert request["allow_redirects"] is False
    assert request["timeout"] == 10
    (discovery,) = idp.discoveries
    assert discovery["auth"] is NO_CREDENTIALS


def test_token_cached(idp: FakeIdp, clock: Clock, provider: TokenProvider) -> None:
    """Tokens are cached, and fetched anew `refresh_before` seconds early."""
    assert provider.get_token(client()) == "token-1"
    clock.now += 239
    assert provider.get_token(client()) == "token-1"
    clock.now += 1
    assert provider.get_token(client()) == "token-2"
    assert len(idp.requests) == 2
    # the discovery document is fetched once
    assert len(idp.discoveries) == 1


def test_token_cache_per_provider(
    idp: FakeIdp, clock: Clock, provider: TokenProvider
) -> None:
    """Each provider has a cache of its own; `clear()` empties it."""
    assert provider.get_token(client()) == "token-1"
    other = TokenProvider(CLIENT_ID_ENV, CLIENT_SECRET_ENV)
    assert other.get_token(client()) == "token-2"
    provider.clear()
    assert provider.get_token(client()) == "token-3"
    assert len(idp.discoveries) == 3


def test_token_refresh_failed(
    idp: FakeIdp,
    clock: Clock,
    provider: TokenProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If a new token can't be fetched, the cached one is used until it expires.

    The warning names the error, not the token or the secret.
    """
    assert provider.get_token(client()) == "token-1"
    idp.token_status = 503
    clock.now += 240
    with caplog.at_level(logging.WARNING):
        assert provider.get_token(client()) == "token-1"
    assert "status 503" in caplog.text
    assert "token-1" not in caplog.text
    assert CLIENT_SECRET not in caplog.text
    clock.now += 59
    assert provider.get_token(client()) == "token-1"
    clock.now += 1
    with pytest.raises(CredentialsError, match="status 503"):
        provider.get_token(client())
    idp.token_status = 200
    assert provider.get_token(client()) == "token-5"


def test_token_short_lifetime(
    idp: FakeIdp, clock: Clock, provider: TokenProvider
) -> None:
    """Short-lived tokens are fetched anew after half their lifetime."""
    idp.token["expires_in"] = 20
    assert provider.get_token(client()) == "token-1"
    clock.now += 9
    assert provider.get_token(client()) == "token-1"
    clock.now += 1
    assert provider.get_token(client()) == "token-2"


@pytest.mark.parametrize("expires_in", [None, "300", True])
def test_token_lifetime_unknown(
    idp: FakeIdp, clock: Clock, provider: TokenProvider, expires_in: Any
) -> None:
    """Tokens without a valid lifetime are taken to expire after 60 seconds."""
    idp.token["expires_in"] = expires_in
    assert provider.get_token(client()) == "token-1"
    clock.now += 29
    assert provider.get_token(client()) == "token-1"
    clock.now += 1
    assert provider.get_token(client()) == "token-2"


def test_token_endpoint_configured(
    idp: FakeIdp, clock: Clock, provider: TokenProvider
) -> None:
    """A configured token endpoint is used without discovery; with a scope.

    The client authenticates in the request body (`client_secret_post`).
    """
    conf = client(
        token_endpoint=TOKEN_ENDPOINT, scope="tes", auth_method="client_secret_post"
    )
    assert provider.get_token(conf) == "token-1"
    assert not idp.discoveries
    (request,) = idp.requests
    assert request["data"] == {
        "grant_type": "client_credentials",
        "scope": "tes",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    # no HTTP Basic credentials from `.netrc` besides the client's
    assert request["auth"] is NO_CREDENTIALS


@pytest.mark.parametrize("parameter", ["resource", "audience"])
def test_token_audience(
    idp: FakeIdp, clock: Clock, provider: TokenProvider, parameter: str
) -> None:
    """Tokens are requested and cached per audience, in the configured parameter.

    Requests without an audience name none, and get a token of their own.
    """
    conf = client(issuer=ISSUER, scope="tes", audience_parameter=parameter)
    get = provider.get_token
    assert get(conf, audience="https://tes1.example.org") == "token-1"
    assert get(conf, audience="https://tes2.example.org") == "token-2"
    assert get(conf, audience="https://tes1.example.org") == "token-1"
    assert get(conf) == "token-3"
    assert [request["data"] for request in idp.requests] == [
        {
            "grant_type": "client_credentials",
            "scope": "tes",
            parameter: "https://tes1.example.org",
        },
        {
            "grant_type": "client_credentials",
            "scope": "tes",
            parameter: "https://tes2.example.org",
        },
        {"grant_type": "client_credentials", "scope": "tes"},
    ]


def test_token_audience_parameters_apart(
    idp: FakeIdp, clock: Clock, provider: TokenProvider
) -> None:
    """Tokens for the same audience in other parameters are cached apart."""
    for parameter in ("resource", "audience"):
        conf = client(issuer=ISSUER, audience_parameter=parameter)
        provider.get_token(conf, audience="https://tes1.example.org")
    assert [list(request["data"]) for request in idp.requests] == [
        ["grant_type", "resource"],
        ["grant_type", "audience"],
    ]


def test_token_audience_refused(idp: FakeIdp, provider: TokenProvider) -> None:
    """Errors of token requests name the requested audience."""
    idp.token_status = 400
    conf = client(issuer=ISSUER, audience_parameter="resource")
    with pytest.raises(
        CredentialsError,
        match=r"No access token for resource 'https://tes1\.example\.org' from",
    ):
        provider.get_token(conf, audience="https://tes1.example.org")


def test_token_audience_not_requested(
    idp: FakeIdp, clock: Clock, provider: TokenProvider
) -> None:
    """Without `audience_parameter`, audiences are not requested: one token."""
    assert (
        provider.get_token(client(), audience="https://tes1.example.org") == "token-1"
    )
    assert (
        provider.get_token(client(), audience="https://tes2.example.org") == "token-1"
    )
    (request,) = idp.requests
    assert request["data"] == {"grant_type": "client_credentials"}


def test_token_audiences_concurrent(idp: FakeIdp, provider: TokenProvider) -> None:
    """A slow token request for one audience doesn't hold up the others.

    Threads that need the same token wait for the same request.
    """
    conf = client(issuer=ISSUER, audience_parameter="resource")
    assert provider.get_token(conf, audience="https://fast.example.org") == "token-1"
    started = threading.Event()
    release = threading.Event()

    def hold(kwargs: dict[str, Any]) -> None:
        """Hold the request for the slow audience until released."""
        if kwargs["data"].get("resource") == "https://slow.example.org":
            started.set()
            assert release.wait(timeout=10)

    idp.hook = hold
    results: list[str] = []

    def fetch_slow() -> None:
        """Get the token of the slow audience."""
        results.append(provider.get_token(conf, audience="https://slow.example.org"))

    threads = [threading.Thread(target=fetch_slow) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert started.wait(timeout=10)
    # cached, while the other request is pending
    assert provider.get_token(conf, audience="https://fast.example.org") == "token-1"
    release.set()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert results == ["token-2", "token-2"]
    assert len(idp.requests) == 2


def test_token_refresh_pending(
    idp: FakeIdp, clock: Clock, provider: TokenProvider
) -> None:
    """While one thread refreshes a token, the others use the cached one."""
    assert provider.get_token(client()) == "token-1"
    clock.now += 240
    started = threading.Event()
    release = threading.Event()

    def hold(kwargs: dict[str, Any]) -> None:
        """Hold the request until released."""
        started.set()
        assert release.wait(timeout=10)

    idp.hook = hold
    results: list[str] = []
    thread = threading.Thread(
        target=lambda: results.append(provider.get_token(client()))
    )
    thread.start()
    assert started.wait(timeout=10)
    assert provider.get_token(client()) == "token-1"
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert results == ["token-2"]
    assert provider.get_token(client()) == "token-2"


def test_token_unexpected_error_released(
    idp: FakeIdp, clock: Clock, provider: TokenProvider
) -> None:
    """Unexpected errors of a fetch end it, so that other threads don't wait."""

    def fail(kwargs: dict[str, Any]) -> None:
        """Fail unexpectedly."""
        raise RuntimeError("bug")

    idp.hook = fail
    with pytest.raises(RuntimeError, match="bug"):
        provider.get_token(client())
    idp.hook = None
    assert provider.get_token(client()) == "token-1"


def test_token_expired_removed(
    idp: FakeIdp, clock: Clock, provider: TokenProvider
) -> None:
    """Expired tokens are removed from the cache when a token is fetched."""
    conf = client(issuer=ISSUER, audience_parameter="resource")
    provider.get_token(conf, audience="https://tes1.example.org")
    clock.now += 300
    provider.get_token(conf, audience="https://tes2.example.org")
    assert [key.audience for key in provider._tokens] == ["https://tes2.example.org"]


@pytest.mark.parametrize("name", [CLIENT_ID_ENV, CLIENT_SECRET_ENV])
def test_token_client_missing(
    idp: FakeIdp, provider: TokenProvider, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Without the client ID or secret, there are no tokens."""
    monkeypatch.setenv(name, "")
    with pytest.raises(CredentialsError, match=name):
        provider.get_token(client())
    assert not idp.requests


@pytest.mark.parametrize(
    "token, status, match",
    [
        ({"error": "invalid_client"}, 401, "HTTPError \\(status 401\\)"),
        ({"access_token": "x"}, 302, "redirected \\(status 302\\)"),
        (None, 200, "invalid response"),
        (["token"], 200, "invalid response"),
        ({"token_type": "Bearer"}, 200, "No valid bearer access token"),
        ({"access_token": ""}, 200, "No valid bearer access token"),
        ({"access_token": "a\nb"}, 200, "No valid bearer access token"),
        ({"access_token": "x", "token_type": "DPoP"}, 200, "No valid bearer"),
        ({"access_token": "x", "token_type": 1}, 200, "No valid bearer access"),
    ],
)
def test_token_invalid(
    idp: FakeIdp, provider: TokenProvider, token: Any, status: int, match: str
) -> None:
    """Errors and invalid responses of the token endpoint give no token.

    Messages leave out the response, which may echo the client secret.
    """
    idp.token = token
    idp.token_status = status
    with pytest.raises(CredentialsError, match=match) as exc_info:
        provider.get_token(client())
    assert CLIENT_SECRET not in str(exc_info.value)


def test_token_endpoint_unreachable(
    idp: FakeIdp, provider: TokenProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreachable token endpoints give no token."""
    conf = client(token_endpoint="https://down.example.org/token")  # noqa: S106
    with pytest.raises(CredentialsError, match="ConnectionError \\(status None\\)"):
        provider.get_token(conf)


@pytest.mark.parametrize(
    "discovery, match",
    [
        (None, "unavailable"),
        (["document"], "invalid"),
        ({"issuer": "https://other.example.org/"}, "invalid"),
        ({"issuer": ISSUER}, "no valid token endpoint"),
        ({"issuer": ISSUER, "token_endpoint": 1}, "no valid token endpoint"),
        (
            {"issuer": ISSUER, "token_endpoint": "http://login.example.org/token"},
            "no valid token endpoint with an allowed URL scheme",
        ),
        (
            {"issuer": ISSUER, "token_endpoint": "https:///token"},
            "no valid token endpoint",
        ),
        (
            {"issuer": ISSUER, "token_endpoint": "https://u:hunter2@idp.example.org/t"},
            "no valid token endpoint",
        ),
        (
            {"issuer": ISSUER, "token_endpoint": "https://[idp.example.org/token"},
            "no valid token endpoint",
        ),
    ],
)
def test_discovery_invalid(
    idp: FakeIdp, provider: TokenProvider, discovery: Any, match: str
) -> None:
    """Invalid discovery documents give no token, and are not cached."""
    idp.discovery = discovery
    with pytest.raises(CredentialsError, match=match):
        provider.get_token(client())
    with pytest.raises(CredentialsError, match=match) as exc_info:
        provider.get_token(client())
    assert "hunter2" not in str(exc_info.value)
    assert len(idp.discoveries) == 2
    assert not idp.requests


def test_discovery_unreachable(idp: FakeIdp, provider: TokenProvider) -> None:
    """Unreachable issuers give no token."""
    with pytest.raises(CredentialsError, match="unavailable: ConnectionError"):
        provider.get_token(client(issuer="https://down.example.org/"))


def test_no_issuer(idp: FakeIdp, provider: TokenProvider) -> None:
    """Without issuer and token endpoint, there are no tokens.

    The config checks prevent this for backends with tokens.
    """
    with pytest.raises(CredentialsError, match="No issuer or token endpoint"):
        provider.get_token(ClientCredentialsConfig())


# Requests over HTTP


class IdpHandler(BaseHTTPRequestHandler):
    """Local identity provider: discovery, tokens, and a redirect."""

    seen: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, format: str, *args: Any) -> None:
        """Log nothing.

        Args:
            format: Format.
            *args: Arguments.
        """

    def _json(
        self, status: int, body: Any, headers: dict[str, str] | None = None
    ) -> None:
        """Send a JSON response.

        Args:
            status: Status code.
            body: Body.
            headers: Other headers.
        """
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        """Serve the discovery document."""
        host = f"http://{self.headers['Host']}"
        self.seen.append(
            {"path": self.path, "authorization": self.headers.get("Authorization")}
        )
        self._json(200, {"issuer": f"{host}/", "token_endpoint": f"{host}/token"})

    def do_POST(self) -> None:
        """Serve the token endpoint, or redirect from `/moved`."""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        self.seen.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        if self.path == "/moved":
            self._json(307, {}, {"Location": "/token"})
            return
        self._json(200, {"access_token": "local-token", "expires_in": 300})


@pytest.fixture
def local_idp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[str]:
    """Local identity provider over `http`, with a `.netrc` for its host.

    Yields:
        Its base URL.
    """
    IdpHandler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), IdpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login netrc-user password netrc-pass\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv(CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(CLIENT_SECRET_ENV, CLIENT_SECRET)
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_token_over_http(local_idp: str, provider: TokenProvider) -> None:
    """Tokens are fetched over HTTP, discovery without `.netrc` credentials."""
    conf = client(issuer=f"{local_idp}/", url_schemes=["http"])
    assert provider.get_token(conf) == "local-token"
    discovery, token = IdpHandler.seen
    assert discovery["path"] == "/.well-known/openid-configuration"
    assert discovery["authorization"] is None
    expected = base64.b64encode(b"gateway+client:s3cret%3A%26").decode()
    assert token["authorization"] == f"Basic {expected}"
    assert token["body"] == "grant_type=client_credentials"


def test_token_not_redirected(local_idp: str, provider: TokenProvider) -> None:
    """Token requests are not redirected, so the secret goes nowhere else."""
    conf = client(token_endpoint=f"{local_idp}/moved", url_schemes=["http"])
    with pytest.raises(CredentialsError, match="redirected"):
        provider.get_token(conf)
    assert [request["path"] for request in IdpHandler.seen] == ["/moved"]


def test_session_no_netrc(local_idp: str) -> None:
    """Sessions take no credentials from `.netrc`, also after redirects."""
    with BackendSession() as session:
        response = session.post(f"{local_idp}/moved", data="x", timeout=5)
    assert response.status_code == 200
    assert [request["authorization"] for request in IdpHandler.seen] == [None, None]
    # plain `requests` would send the `.netrc` credentials
    IdpHandler.seen = []
    requests.post(f"{local_idp}/token", data="x", timeout=5)
    assert IdpHandler.seen[0]["authorization"] is not None


def test_session_redirect_credentials(local_idp: str) -> None:
    """Credentials stay on redirects to the same host."""
    with BackendSession() as session:
        session.post(
            f"{local_idp}/moved", data="x", auth=Credentials.bearer("own"), timeout=5
        )
    assert [request["authorization"] for request in IdpHandler.seen] == [
        "Bearer own",
        "Bearer own",
    ]


def test_session_redirect_other_host() -> None:
    """Credentials are dropped on redirects to other hosts."""
    session = BackendSession()
    prepared = requests.Request(
        "GET", "https://other.example.org/", headers={"Authorization": "Bearer x"}
    ).prepare()
    response = requests.Response()
    response.request = requests.Request("GET", BACKEND_URL).prepare()
    session.rebuild_auth(prepared, response)
    assert "Authorization" not in prepared.headers
    session.close()


# Resolver


@pytest.fixture
def backend_list() -> BackendList:
    """Backend list with an entry of each credentials mode, and a registry."""
    return BackendList(
        [
            {"type": "ga4gh-tes", "url": f"{BACKEND_URL}/token"},
            {
                "type": "ga4gh-tes",
                "url": f"{BACKEND_URL}/audience",
                "audience": "urn:tes:audience",
            },
            {
                "type": "ga4gh-tes",
                "url": f"{BACKEND_URL}/basic",
                "auth": "basic",
                "basic": {"username_env": "TES_USER", "password_env": "TES_PASSWORD"},
            },
            {"type": "ga4gh-tes", "url": f"{BACKEND_URL}/none", "auth": "none"},
            {"type": "ga4gh-service-registry", "id": "reg", "url": REGISTRY_URL},
        ],
        api="tes",
    )


def resolve(
    backend_list: BackendList,
    backend: Backend,
    provider: TokenProvider,
    conf: ClientCredentialsConfig | None = None,
    auth: str | None = None,
) -> Credentials:
    """Get the credentials of a backend.

    Args:
        backend_list: Backend list.
        backend: Backend.
        provider: Token provider.
        conf: Client; by default, with the issuer.
        auth: Name of the credentials, if not the backend's.

    Returns:
        Credentials.
    """
    return get_credentials(
        backend,
        backend_list.entry_for(backend),
        conf or client(),
        provider,
        auth=auth,
    )


def configured(backend_list: BackendList, path: str) -> Backend:
    """Get a configured backend of the list.

    Args:
        backend_list: Backend list.
        path: Path of its URL.

    Returns:
        The backend.
    """
    return next(
        entry.backend()
        for entry in backend_list.entries
        if isinstance(entry, ServiceEntry) and entry.url == f"{BACKEND_URL}{path}"
    )


DISCOVERED = Backend(
    id="https://found.example.org",
    url="https://found.example.org",
    source="discovered",
    group="reg",
    auth="client_credentials",
)


def test_credentials_token(
    idp: FakeIdp, provider: TokenProvider, backend_list: BackendList
) -> None:
    """Backends with `auth: client_credentials` get the gateway's token."""
    creds = resolve(backend_list, configured(backend_list, "/token"), provider)
    assert creds.authorization == "Bearer token-1"
    assert resolve(backend_list, DISCOVERED, provider) == creds


def test_credentials_token_audience(
    idp: FakeIdp, provider: TokenProvider, backend_list: BackendList
) -> None:
    """With audiences, each backend gets a token for its own."""
    conf = client(issuer=ISSUER, audience_parameter="resource")
    resolve(backend_list, configured(backend_list, "/token"), provider, conf)
    resolve(backend_list, configured(backend_list, "/audience"), provider, conf)
    resolve(backend_list, DISCOVERED, provider, conf)
    assert [request["data"]["resource"] for request in idp.requests] == [
        f"{BACKEND_URL}/token",
        "urn:tes:audience",
        "https://found.example.org",
    ]


def test_token_audience_function(backend_list: BackendList) -> None:
    """Audiences are those of entries, or else the backends' URLs."""
    backend = configured(backend_list, "/audience")
    entry = backend_list.entry_for(backend)
    assert token_audience(backend, entry, client()) is None
    conf = client(issuer=ISSUER, audience_parameter="audience")
    assert token_audience(backend, entry, conf) == "urn:tes:audience"
    assert token_audience(DISCOVERED, backend_list.entry_for(DISCOVERED), conf) == (
        "https://found.example.org"
    )


def test_credentials_basic_env(
    provider: TokenProvider, backend_list: BackendList, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backends with `auth: basic` get the credentials of the environment."""
    backend = configured(backend_list, "/basic")
    with pytest.raises(CredentialsError, match="TES_USER is not set"):
        resolve(backend_list, backend, provider)
    monkeypatch.setenv("TES_USER", "user")
    monkeypatch.setenv("TES_PASSWORD", "pass")
    creds = resolve(backend_list, backend, provider)
    assert creds == Credentials.basic("user", "pass")


def test_credentials_none(provider: TokenProvider, backend_list: BackendList) -> None:
    """Backends with `auth: none` get no credentials."""
    creds = resolve(backend_list, configured(backend_list, "/none"), provider)
    assert creds is NO_CREDENTIALS


def test_credentials_chosen(
    idp: FakeIdp, provider: TokenProvider, backend_list: BackendList
) -> None:
    """Plugins may choose other credentials of a backend by name."""
    backend = configured(backend_list, "/token")
    assert resolve(backend_list, backend, provider, auth="none") is NO_CREDENTIALS
    with pytest.raises(CredentialsError, match="No HTTP Basic credentials"):
        resolve(backend_list, backend, provider, auth="basic")
    with pytest.raises(CredentialsError, match="No HTTP Basic credentials"):
        resolve(backend_list, DISCOVERED, provider, auth="basic")
    with pytest.raises(CredentialsError, match="Unknown credentials 'netrc'"):
        resolve(backend_list, backend, provider, auth="netrc")


# Startup checks


def check(
    listed: list[dict[str, Any]],
    provider: TokenProvider,
    conf: ClientCredentialsConfig | None = None,
    public_url: str | None = "https://gateway.example.org",
) -> None:
    """Check backends of a TES gateway.

    Args:
        listed: Entries.
        provider: Token provider.
        conf: Client; by default, with the issuer.
        public_url: Public URL of the gateway.
    """
    check_backends(
        listed,
        conf or client(),
        provider,
        api="tes",
        public_url=public_url,
        key="custom.tes.backends",
    )


def test_check_backends(
    idp: FakeIdp,
    provider: TokenProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Backends are checked and logged, without credentials."""
    monkeypatch.setenv("TES_USER", "user")
    monkeypatch.setenv("TES_PASSWORD", "pass")
    listed: list[dict[str, Any]] = [
        {"type": "ga4gh-tes", "url": f"{BACKEND_URL}/token"},
        {
            "type": "ga4gh-tes",
            "url": f"{BACKEND_URL}/basic",
            "auth": "basic",
            "basic": {"username_env": "TES_USER", "password_env": "TES_PASSWORD"},
        },
        {"type": "ga4gh-tes", "url": f"{BACKEND_URL}/none", "auth": "none"},
        {
            "type": "ga4gh-service-registry",
            "url": REGISTRY_URL,
            "auth": "none",
            "allow_private_addresses": True,
            "registry_allow_private_addresses": True,
        },
    ]
    with caplog.at_level(logging.INFO, logger=module.__name__):
        check(listed, provider, client(issuer=ISSUER, audience_parameter="resource"))
    text = caplog.text
    assert "TES backend https://tes.example.org/basic: HTTP Basic" in text
    assert "TES backend https://tes.example.org/none: no authentication" in text
    assert f"TES backends discovered at registry {REGISTRY_URL} get no" in text
    assert "may have private addresses" in text
    assert f"Registry {REGISTRY_URL} may have a private address" in text
    assert (
        "TES backends with access tokens of the gateway (each for its own"
        " audience, requested with `resource`): https://tes.example.org/token"
    ) in text
    assert "pass" not in text
    assert CLIENT_SECRET not in text


def test_check_backends_registry_tokens(
    idp: FakeIdp, provider: TokenProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Registries whose backends get tokens are logged as token recipients."""
    with caplog.at_level(logging.INFO, logger=module.__name__):
        check([{"type": "ga4gh-service-registry", "url": REGISTRY_URL}], provider)
    assert (
        f"access tokens of the gateway: those verified by registry {REGISTRY_URL}"
        in (caplog.text)
    )


def test_check_backends_empty(
    provider: TokenProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Without backends, a warning is logged; no client is needed."""
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        check_backends(
            BackendListConfig([]), ClientCredentialsConfig(), provider, api="wes"
        )
    assert "No WES backends are configured (`backends`)" in caplog.text


def test_check_backends_http(
    idp: FakeIdp, provider: TokenProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """URLs with scheme `http` are warned about."""
    listed = [
        {
            "type": "ga4gh-tes",
            "url": "http://tes.local",
            "url_schemes": ["https", "http"],
        }
    ]
    conf = client(issuer="http://login.local/", url_schemes=["https", "http"])
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        check(listed, provider, conf)
    assert (
        "scheme 'http' are allowed (`url_schemes`) for http://tes.local" in caplog.text
    )
    assert "allowed for the identity provider" in caplog.text


@pytest.mark.parametrize("name", [CLIENT_ID_ENV, CLIENT_SECRET_ENV])
def test_check_backends_client_missing(
    idp: FakeIdp, provider: TokenProvider, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Backends with tokens need the client's environment variables."""
    monkeypatch.delenv(name)
    with pytest.raises(RuntimeError, match=f"set the environment variables {name}$"):
        check([{"type": "ga4gh-tes", "url": BACKEND_URL}], provider)


def test_check_backends_basic_missing(
    provider: TokenProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backends with `auth: basic` need their environment variables."""
    monkeypatch.delenv("TES_USER", raising=False)
    monkeypatch.delenv("TES_PASSWORD", raising=False)
    listed = [
        {
            "type": "ga4gh-tes",
            "url": BACKEND_URL,
            "auth": "basic",
            "basic": {"username_env": "TES_USER", "password_env": "TES_PASSWORD"},
        }
    ]
    with pytest.raises(
        RuntimeError,
        match="Credentials of TES backends missing: set the environment variables"
        " TES_PASSWORD, TES_USER",
    ):
        check(listed, provider)


def test_check_backends_basic_removed(provider: TokenProvider) -> None:
    """Entries changed after validation to `basic` without `basic` fail."""
    entry = ServiceEntry(type="ga4gh-tes", url=BACKEND_URL, auth="none")
    entry.auth = "basic"
    with pytest.raises(RuntimeError, match="`auth: basic` without `basic`"):
        module._check_service(entry, "TES backend")


@pytest.mark.parametrize(
    "listed, conf, public_url, match",
    [
        (
            [{"type": "ga4gh-wes", "url": BACKEND_URL}],
            None,
            None,
            "Invalid backends \\(`custom.tes.backends`\\).*type 'ga4gh-tes'",
        ),
        (
            [{"type": "ga4gh-tes", "url": BACKEND_URL}],
            ClientCredentialsConfig(),
            None,
            "need the `issuer` or `token_endpoint`",
        ),
        (
            [{"type": "ga4gh-service-registry", "url": REGISTRY_URL, "auth": "none"}],
            None,
            None,
            "`server.public_url` is not set",
        ),
        (
            [
                {
                    "type": "ga4gh-tes",
                    "url": "https://gateway.example.org/tes",
                    "auth": "none",
                }
            ],
            None,
            "https://gateway.example.org",
            "would never be used.*https://gateway.example.org/tes",
        ),
    ],
)
def test_check_backends_invalid(
    provider: TokenProvider,
    listed: list[dict[str, Any]],
    conf: ClientCredentialsConfig | None,
    public_url: str | None,
    match: str,
) -> None:
    """Lists that can't work refuse to start."""
    with pytest.raises(RuntimeError, match=match):
        check(listed, provider, conf, public_url=public_url)
