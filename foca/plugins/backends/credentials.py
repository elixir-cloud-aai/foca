"""Credentials of the backends of gateway apps (cf. :py:mod:`foca.plugins.backends`).

Gateways authenticate at their backends with credentials of their own, never
with those of their callers, as named by `auth` of each entry of the backend
list (discovered backends get that of their registry entry; plugins may name
other ones, `OutboundRequest.auth`):

- `client_credentials`: an access token of the gateway, obtained from its
  identity provider with the OAuth 2.0 client credentials grant (RFC 6749,
  section 4.4; :py:class:`TokenProvider`), with the client ID and secret of
  environment variables that the gateway names (e.g., `PROTES_CLIENT_ID` and
  `PROTES_CLIENT_SECRET`) and the settings of
  :py:class:`foca.plugins.backends.ClientCredentialsConfig`; with its
  `audience_parameter`, each backend gets a token for its own audience
  (cf. :py:func:`token_audience`), otherwise all get the same;
- `basic`: HTTP Basic credentials of the environment variables named by the
  entry (`basic`; a transitional mode, for backends that don't accept tokens
  yet);
- `none`: no credentials.

:py:func:`get_credentials` resolves them for each request, so that
long-running jobs (e.g., Celery tasks) get fresh tokens, as a `requests`
auth object (:py:class:`Credentials`) that also gives the `Authorization`
header for other clients. Requests get no other credentials: the auth object
replaces any `Authorization` header that the gateway copied from its caller,
and keeps `requests` from taking credentials from a `.netrc` file of the
environment; :py:class:`BackendSession` takes none after redirects either.
Secrets are read from the environment only (e.g., set from mounted
Kubernetes Secrets), never from the config, and never logged: errors name
variables, endpoints, audiences and error types only.

:py:func:`check_backends` checks the credentials on startup and logs how the
backends are called.

Plain `requests`; no web framework.
"""

import base64
import logging
import math
import os
import re
import threading
import time
import weakref
from collections.abc import Collection, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NamedTuple, Self
from urllib.parse import quote_plus, urlsplit

import requests
from pydantic import Field, PositiveInt, model_validator

from foca.config.environment import get_env
from foca.plugins.backends.config import (
    BackendListConfig,
    RegistryEntry,
    ServiceEntry,
    _Model,
)
from foca.plugins.backends.lists import BackendList
from foca.plugins.model import Backend
from foca.utils.urls import InvalidURL, normalize_url

logger = logging.getLogger(__name__)

OIDC_CONFIG_PATH = ".well-known/openid-configuration"
"""Path of OpenID Connect discovery documents, relative to the issuer (cf.
https://openid.net/specs/openid-connect-discovery-1_0.html)."""

DEFAULT_TOKEN_LIFETIME = 60
"""Lifetime (in seconds) assumed for tokens of responses without a valid
`expires_in`."""

MAX_TOKEN_LIFETIME = 86400
"""Maximum lifetime (in seconds) of cached tokens, whatever `expires_in` says."""

FAILURE_RETRY = 5.0

WAIT_FACTOR = 3
"""Threads wait for the fetch of another thread for at most this many times
the timeout of the client (`ClientCredentialsConfig.timeout`), as that
timeout bounds each operation of a request rather than all of it."""
"""Seconds that a failed token fetch or discovery is not tried again: calls
meanwhile get the same error (or a cached token that is still valid)."""

_DIGITS = re.compile(r"[0-9]{1,9}")


def _lifetime(expires_in: object) -> float:
    """Get the lifetime of a token from `expires_in` of a token response.

    Args:
        expires_in: Value of `expires_in`: seconds, as a number or, as some
            identity providers send it, as a string of digits.

    Returns:
        The lifetime in seconds, from 0 to `MAX_TOKEN_LIFETIME`;
        `DEFAULT_TOKEN_LIFETIME` for other values (e.g., booleans, `NaN` or
        infinite numbers).
    """
    if isinstance(expires_in, str) and _DIGITS.fullmatch(expires_in):
        expires_in = int(expires_in)
    if isinstance(expires_in, bool) or not isinstance(expires_in, int | float):
        return DEFAULT_TOKEN_LIFETIME
    if isinstance(expires_in, float) and not math.isfinite(expires_in):
        return DEFAULT_TOKEN_LIFETIME
    # bounded before converting: integers of JSON may exceed floats
    return float(min(max(expires_in, 0), MAX_TOKEN_LIFETIME))


# Visible ASCII characters but space: access tokens (RFC 6749, appendix A.12)
# that fit in an `Authorization` header (RFC 6750, section 2.1)
_TOKEN = re.compile(r"[\x21-\x7e]+")
# Characters that HTTP Basic credentials must not have (RFC 7617, section 2):
# control characters
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# Absolute URIs without query and fragment (RFC 3986, sections 3 and 4.3):
# a scheme, then unreserved, reserved (but `?`, `#`) and percent-encoded
# characters only
_RESOURCE = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*:(?:[A-Za-z0-9._~!$&'()*+,;=:@/\[\]-]|%[0-9A-Fa-f]{2})+"
)


def _is_resource(value: str) -> bool:
    """Check whether a value is a resource indicator (RFC 8707, section 2).

    Args:
        value: The value.

    Returns:
        Whether it is an absolute URI without query and fragment, with a
        host or path, and a valid port, if any.
    """
    if not _RESOURCE.fullmatch(value):
        return False
    parts = urlsplit(value)
    try:
        parts.port  # noqa: B018 (raises on invalid ports)
    except ValueError:
        return False
    return bool(parts.netloc or parts.path)


def check_idp_url(url: object, schemes: Collection[str], name: str) -> None:
    """Check a URL of the identity provider of a gateway's client.

    Error messages leave out the URL, which may hold credentials.

    Args:
        url: URL, e.g., of the issuer or token endpoint.
        schemes: Allowed schemes.
        name: What the URL is of, for error messages.

    Raises:
        ValueError: The URL is invalid, has user info, or a scheme that is
            not allowed.
    """
    try:
        normalize_url(url, schemes=schemes, name=name, keep_query=True)
    except InvalidURL as exc:
        raise ValueError(
            f"{name}: invalid, with user info, or of a scheme that is not"
            f" allowed (`url_schemes`: {sorted(schemes)})"
        ) from exc


class ClientCredentialsConfig(_Model):
    """Client of a gateway at its identity provider (OAuth 2.0).

    The gateway gets access tokens of its own with the client credentials
    grant (RFC 6749, section 4.4) for the backends with `auth:
    client_credentials` (cf.
    :py:class:`foca.plugins.backends.credentials.TokenProvider`). The client
    ID and secret are read from environment variables that the gateway names
    (e.g., `PROTES_CLIENT_ID` and `PROTES_CLIENT_SECRET`), never from the
    config. Gateways embed it next to their backend list (e.g.,
    `custom.tes.client_credentials`) and check both with
    :py:func:`check_client_credentials`.

    Args:
        issuer: Issuer of the identity provider; its token endpoint is taken
            from its OpenID Connect discovery document unless
            `token_endpoint` is set.
        token_endpoint: Token endpoint of the identity provider.
        url_schemes: Allowed schemes of the URLs of the identity provider
            (issuer, token endpoint, also a discovered one); `https` only by
            default (dev and test setups may allow `http`).
        scope: Scope of the requested tokens (space-separated), if any.
        audience_parameter: Parameter of token requests that names the
            backend that a token is for, so that each backend gets a token
            of its own, which the others don't accept: `resource` (RFC 8707,
            resource indicators) or `audience` (as some identity providers
            name it); the value is the `audience` of the backend's entry, or
            else its URL. By default, no backend is named, and all backends
            get the same token.
        auth_method: How the client authenticates at the token endpoint:
            with HTTP Basic (`client_secret_basic`) or in the request body
            (`client_secret_post`).
        refresh_before: Seconds before the expiry of a token at which a new
            one is fetched (at most half its lifetime).
        timeout: Seconds per request to the identity provider.

    Attributes:
        issuer: Issuer of the identity provider, if any.
        token_endpoint: Token endpoint of the identity provider, if any.
        url_schemes: Allowed schemes of the URLs of the identity provider.
        scope: Scope of the requested tokens, if any.
        audience_parameter: Parameter of token requests that names the
            backend (`resource` or `audience`), if any.
        auth_method: How the client authenticates at the token endpoint.
        refresh_before: Seconds before the expiry of a token at which a new
            one is fetched.
        timeout: Seconds per request to the identity provider.
    """

    issuer: str | None = None
    token_endpoint: str | None = None
    url_schemes: list[Literal["https", "http"]] = Field(default=["https"], min_length=1)
    scope: str | None = Field(default=None, min_length=1)
    audience_parameter: Literal["resource", "audience"] | None = None
    auth_method: Literal["client_secret_basic", "client_secret_post"] = (
        "client_secret_basic"
    )
    refresh_before: PositiveInt = 60
    timeout: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 10

    @model_validator(mode="after")
    def _check_urls(self) -> Self:
        """Check the URLs of the identity provider.

        Returns:
            The config.

        Raises:
            ValueError: The issuer or token endpoint is invalid, has user
                info, or a scheme that is not allowed.
        """
        for name, value in (
            ("issuer", self.issuer),
            ("token endpoint", self.token_endpoint),
        ):
            if value is not None:
                check_idp_url(value, self.url_schemes, f"URL of the {name}")
        return self


def _entries(
    entries: BackendListConfig | Iterable[ServiceEntry | RegistryEntry],
) -> list[ServiceEntry | RegistryEntry]:
    """Get the entries of a backend list.

    Args:
        entries: Backend list, or its entries.

    Returns:
        The entries.
    """
    if isinstance(entries, BackendListConfig):
        return list(entries.root)
    return list(entries)


def uses_tokens(
    entries: BackendListConfig | Iterable[ServiceEntry | RegistryEntry],
) -> bool:
    """Check whether a backend has, or discovered ones get, the gateway's token.

    Args:
        entries: Backend list, or its entries.

    Returns:
        Whether an entry has `auth: client_credentials`.
    """
    return any(entry.auth == "client_credentials" for entry in _entries(entries))


def check_client_credentials(
    entries: BackendListConfig | Iterable[ServiceEntry | RegistryEntry],
    client: ClientCredentialsConfig,
) -> None:
    """Check a backend list against the gateway's client, e.g., in a validator.

    Args:
        entries: Backend list, or its entries.
        client: Client of the gateway at its identity provider.

    Raises:
        ValueError: A backend has (or discovered backends get) `auth:
            client_credentials`, but the client has neither issuer nor token
            endpoint; or an entry has an `audience`, but audiences are not
            requested (`audience_parameter`), or one requested as
            `resource` is not an absolute URI without query and fragment
            (RFC 8707, section 2).
    """
    entries = _entries(entries)
    if uses_tokens(entries) and client.issuer is None and client.token_endpoint is None:
        raise ValueError(
            "Backends with `auth: client_credentials` need the `issuer` or"
            " `token_endpoint` of `client_credentials`"
        )
    parameter = client.audience_parameter
    for entry in entries:
        audience = getattr(entry, "audience", None)
        if audience is None:
            continue
        if parameter is None:
            raise ValueError(
                f"Backend {entry.id!r}: `audience` needs `audience_parameter` of"
                " `client_credentials`"
            )
        if parameter == "resource" and not _is_resource(audience):
            raise ValueError(
                f"Backend {entry.id!r}: `audience` must be an absolute URI"
                " without query and fragment, for `audience_parameter:"
                " resource` (RFC 8707)"
            )


class CredentialsError(Exception):
    """Credentials of a backend are not available.

    Messages name environment variables, endpoints, audiences and error
    types, never secrets or tokens, so that they can be logged.
    """


def _get_env(name: str) -> str:
    """Get credentials from an environment variable, or the file it names.

    As :py:func:`foca.config.environment.get_env`: with `<name>_FILE`, the
    value is read from the named file (e.g., a mounted Kubernetes Secret).

    Args:
        name: Name of the environment variable.

    Returns:
        The value.

    Raises:
        CredentialsError: Neither the variable nor `<name>_FILE` is set (or
            the value is empty), both are set, or the file can't be read.
    """
    try:
        value = get_env(name)
    except ValueError as exc:
        # names the variables only, never the content of the file
        raise CredentialsError(str(exc)) from None
    if not value:
        raise CredentialsError(
            f"Environment variable {name} (or {name}_FILE) is not set"
        )
    return value


def _missing_env(*names: str) -> list[str]:
    """Get the environment variables with credentials that are not available.

    Args:
        *names: Names of environment variables.

    Returns:
        The names of the variables that are neither set (nor `<name>_FILE`),
        empty, set both ways, or whose file can't be read.
    """
    missing = []
    for name in names:
        try:
            _get_env(name)
        except CredentialsError:
            missing.append(name)
    return missing


def _valid_url(url: str, client: ClientCredentialsConfig) -> bool:
    """Check whether a URL of the identity provider is valid and allowed.

    Args:
        url: URL, e.g., of a discovered token endpoint.
        client: Client of the gateway, with the allowed URL schemes.

    Returns:
        Whether the URL is valid, without user info, of an allowed scheme.
    """
    try:
        check_idp_url(url, client.url_schemes, "Token endpoint")
    except ValueError:
        return False
    return True


class Credentials(requests.auth.AuthBase):
    """Credentials of a request to a backend, as a `requests` auth object.

    Sets the `Authorization` header of each request, or removes it for no
    credentials, so that requests carry these credentials only (none of the
    gateway's caller, none from `.netrc`). Its representation leaves the
    credentials out.

    Attributes:
        scheme: Authentication scheme (`Bearer` or `Basic`), or `None` for
            no credentials.
    """

    def __init__(
        self,
        scheme: Literal["Bearer", "Basic"] | None = None,
        value: str | None = None,
    ) -> None:
        """Create credentials.

        Args:
            scheme: Authentication scheme; `None` for no credentials.
            value: Credentials of the scheme (a token, or encoded user and
                password), set if, and only if, `scheme` is.

        Raises:
            ValueError: `value` is missing for a scheme, or set without one.
        """
        if (scheme is None) != (value is None):
            raise ValueError("Credentials need a value with, and only with, a scheme")
        self.scheme = scheme
        self._authorization = None if scheme is None else f"{scheme} {value}"

    @classmethod
    def bearer(cls, token: str) -> "Credentials":
        """Get the credentials of an access token.

        Args:
            token: Access token.

        Returns:
            Credentials.

        Raises:
            CredentialsError: The token is empty, or has characters other
                than visible ASCII ones (e.g., spaces or line breaks).
        """
        if not _TOKEN.fullmatch(token):
            raise CredentialsError(
                "Access token is empty or has characters other than visible ASCII"
            )
        return cls("Bearer", token)

    @classmethod
    def basic(cls, username: str, password: str) -> "Credentials":
        """Get HTTP Basic credentials (RFC 7617), encoded as UTF-8.

        Args:
            username: User name.
            password: Password.

        Returns:
            Credentials.

        Raises:
            CredentialsError: The user name has a colon, or either has
                control characters.
        """
        if ":" in username:
            raise CredentialsError("HTTP Basic user name must not have a colon")
        if _CONTROL.search(username) or _CONTROL.search(password):
            raise CredentialsError(
                "HTTP Basic credentials must not have control characters"
            )
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return cls("Basic", encoded)

    @property
    def authorization(self) -> str | None:
        """Value of the `Authorization` header, or `None` for none."""
        return self._authorization

    @property
    def headers(self) -> dict[str, str]:
        """Headers with the credentials (empty for none), for other clients."""
        if self._authorization is None:
            return {}
        return {"Authorization": self._authorization}

    def apply(self, headers: MutableMapping[str, str]) -> None:
        """Set the credentials in a mutable mapping of headers.

        Removes any `Authorization` header first (in any case of the name),
        also for no credentials.

        Args:
            headers: Headers, e.g., `requests.structures.CaseInsensitiveDict`
                or a plain `dict`.
        """
        for name in [name for name in headers if name.lower() == "authorization"]:
            del headers[name]
        if self._authorization is not None:
            headers["Authorization"] = self._authorization

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        """Set the credentials of a request.

        Args:
            request: The request.

        Returns:
            The request.
        """
        self.apply(request.headers)
        return request

    def __eq__(self, other: object) -> bool:
        """Compare credentials.

        Args:
            other: Other object.

        Returns:
            Whether both are credentials with the same header.
        """
        if not isinstance(other, Credentials):
            return NotImplemented
        return self._authorization == other._authorization

    def __hash__(self) -> int:
        """Hash the credentials.

        Returns:
            Hash of the header.
        """
        return hash(self._authorization)

    def __repr__(self) -> str:
        """Represent the credentials, without them.

        Returns:
            The scheme only, e.g., `Credentials(Bearer)`.
        """
        return f"Credentials({self.scheme or 'none'})"


NO_CREDENTIALS = Credentials()
"""No credentials, also none from `.netrc`."""


class BackendSession(requests.Session):
    """Session of requests to backends and identity providers, without `.netrc`.

    `requests` takes credentials from a `.netrc` file of the environment
    (`~/.netrc`, or the file of `NETRC`) for requests without credentials of
    their own, and again after each redirect. This session never does:
    requests get only the credentials they are sent with. Other settings of
    the environment, such as proxies and CA certificates, still apply.
    Credentials are dropped on redirects to other hosts or ports, and from
    `https` to `http`, as `requests` does.
    """

    def __init__(self) -> None:
        """Create a session."""
        super().__init__()
        self.auth = NO_CREDENTIALS

    def rebuild_auth(
        self, prepared_request: requests.PreparedRequest, response: requests.Response
    ) -> None:
        """Drop the credentials of a redirect to another host; add none.

        Args:
            prepared_request: The request of the redirect.
            response: The response with the redirect.
        """
        headers = prepared_request.headers
        if "Authorization" in headers and self.should_strip_auth(
            str(response.request.url), str(prepared_request.url)
        ):
            del headers["Authorization"]


class TokenKey(NamedTuple):
    """Key of a cached access token of a gateway.

    Attributes:
        endpoint: URL of the token endpoint.
        client_id: Client ID of the gateway.
        scope: Requested scope, if any.
        audience_parameter: Parameter of the requested audience, if any.
        audience: Requested audience, if any.
    """

    endpoint: str
    client_id: str
    scope: str | None
    audience_parameter: str | None
    audience: str | None


@dataclass(frozen=True, repr=False)
class _Token:
    """Cached access token.

    Attributes:
        value: The access token.
        refresh_at: When to fetch a new token (`time.monotonic()`).
        expires_at: When the token expires (`time.monotonic()`).
    """

    value: str
    refresh_at: float
    expires_at: float


class TokenProvider:
    """Access tokens of a gateway (client credentials grant), cached.

    Each gateway creates one provider per process (e.g., at module level),
    with the names of the environment variables of its client ID and secret.
    Thread-safe; each process (e.g., each Gunicorn or Celery worker) has its
    own cache. Each token (cf. :py:class:`TokenKey`) is fetched by one
    thread at a time, so that threads that need the same new token at the
    same time wait for the same fetch instead of each fetching one; threads
    that need other tokens (e.g., for other audiences) don't wait for it, nor
    do threads that can still use the cached token while it is refreshed.

    Requests to the identity provider follow its settings
    (:py:class:`foca.plugins.backends.ClientCredentialsConfig`), go through
    a :py:class:`BackendSession` (with the proxies and CA certificates of
    the environment, e.g., `REQUESTS_CA_BUNDLE`, but no `.netrc`), and don't
    follow redirects with the client credentials.

    Attributes:
        client_id_env: Name of the environment variable with the client ID.
        client_secret_env: Name of the environment variable with the client
            secret.
    """

    def __init__(self, client_id_env: str, client_secret_env: str) -> None:
        """Create a provider with empty caches.

        Args:
            client_id_env: Name of the environment variable with the client
                ID, e.g., `PROTES_CLIENT_ID`.
            client_secret_env: Name of the environment variable with the
                client secret, e.g., `PROTES_CLIENT_SECRET`.

        Raises:
            ValueError: A name is empty, or both are the same.
        """
        if not (client_id_env and client_secret_env):
            raise ValueError("Names of the client's environment variables are needed")
        if client_id_env == client_secret_env:
            raise ValueError(
                "Client ID and secret need environment variables of their own"
            )
        self.client_id_env = client_id_env
        self.client_secret_env = client_secret_env
        self._tokens: dict[TokenKey, _Token] = {}
        # failed fetches: until when, and the message
        self._failures: dict[TokenKey, tuple[float, str]] = {}
        self._token_endpoints: dict[str, str] = {}
        self._discovery_failures: dict[str, tuple[float, str]] = {}
        # changed by `clear()`, so that fetches in progress then don't store
        self._generation = 0
        self._reset_lock()
        _providers.add(self)

    def _reset_lock(self) -> None:
        """Create the lock, and forget the fetches in progress.

        Also in forked processes (cf. `_reset_after_fork`), whose copies of
        the lock may be held, and of fetches that no thread of theirs ends.
        """
        # guards the caches and the fetches in progress; never held during
        # requests to the identity provider
        self._lock = threading.Lock()
        # notified whenever a fetch ends
        self._fetched = threading.Condition(self._lock)
        self._fetching: set[TokenKey] = set()
        self._discovering: set[str] = set()

    def __repr__(self) -> str:
        """Represent the provider by its environment variables.

        Returns:
            Representation, without tokens.
        """
        return (
            f"TokenProvider(client_id_env={self.client_id_env!r},"
            f" client_secret_env={self.client_secret_env!r})"
        )

    def missing_env(self) -> list[str]:
        """Get the environment variables of the client that are not set.

        Returns:
            Names of the variables of the client ID and secret that are not
            set, or empty.
        """
        return _missing_env(self.client_id_env, self.client_secret_env)

    def clear(self) -> None:
        """Remove all cached tokens, token endpoints and failures."""
        with self._lock:
            self._generation += 1
            self._tokens.clear()
            self._failures.clear()
            self._token_endpoints.clear()
            self._discovery_failures.clear()

    def _wait(self, deadline: float, what: str) -> None:
        """Wait for the fetch of another thread, until a deadline.

        Must be called with the lock held.

        Args:
            deadline: `time.monotonic()` value until which to wait.
            what: What is fetched, for the error message.

        Raises:
            CredentialsError: The deadline passed.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CredentialsError(f"{what} still being fetched by another thread")
        self._fetched.wait(remaining)

    def get_token(
        self, client: ClientCredentialsConfig, audience: str | None = None
    ) -> str:
        """Get an access token of the gateway.

        A cached token is used until `refresh_before` seconds before it
        expires (at most half its lifetime before), then a new one is
        fetched (meanwhile, other threads use the cached one); if that
        fails, the cached token is used until it expires. Tokens are cached
        per token endpoint, client, scope and audience; expired ones are
        removed whenever a token is fetched. After a failed fetch, further
        calls for the same token get the same error (or the cached token)
        for `FAILURE_RETRY` seconds instead of fetching it again, so that
        an unreachable identity provider doesn't hold up each request in
        turn.

        Args:
            client: Client of the gateway at its identity provider.
            audience: Audience of the token, requested with
                `client.audience_parameter` (ignored if that is not set); by
                default, none.

        Returns:
            The access token.

        Raises:
            CredentialsError: The client ID or secret is not set, or no
                token was obtained.
        """
        client_id = _get_env(self.client_id_env)
        client_secret = _get_env(self.client_secret_env)
        if client.audience_parameter is None:
            audience = None
        endpoint = self._get_token_endpoint(client)
        key = TokenKey(
            endpoint, client_id, client.scope, client.audience_parameter, audience
        )
        deadline = time.monotonic() + WAIT_FACTOR * client.timeout
        with self._fetched:
            while True:
                token = self._tokens.get(key)
                now = time.monotonic()
                usable = token is not None and now < token.expires_at
                if token is not None and now < token.refresh_at:
                    return token.value
                if key not in self._fetching:
                    failure = self._failures.get(key)
                    if failure is None or now >= failure[0]:
                        self._fetching.add(key)
                        break
                    if token is not None and usable:
                        return token.value
                    raise CredentialsError(failure[1])
                if token is not None and usable:
                    # being refreshed by another thread: use it meanwhile
                    return token.value
                self._wait(deadline, f"Access token from {endpoint}")
            generation = self._generation
        new: _Token | None = None
        error: CredentialsError | None = None
        try:
            new = self._fetch_token(
                endpoint=endpoint,
                client_id=client_id,
                client_secret=client_secret,
                client=client,
                audience=audience,
            )
        except CredentialsError as exc:
            error = exc
        finally:
            # also after unexpected errors, so that waiting threads go on
            with self._fetched:
                now = time.monotonic()
                self._fetching.discard(key)
                if generation != self._generation:
                    pass  # cleared meanwhile: store nothing
                elif new is not None:
                    for stale in [
                        k for k, v in self._tokens.items() if now >= v.expires_at
                    ]:
                        del self._tokens[stale]
                    self._tokens[key] = new
                    self._failures.pop(key, None)
                elif error is not None:
                    self._failures[key] = (now + FAILURE_RETRY, str(error))
                for gone in [k for k, v in self._failures.items() if now >= v[0]]:
                    del self._failures[gone]
                self._fetched.notify_all()
        if new is not None:
            return new.value
        if token is None or time.monotonic() >= token.expires_at:
            raise error  # type: ignore[misc]  # set if no token was fetched
        # the messages of CredentialsError name endpoints, audiences and
        # error types only, never tokens or client secrets
        # nosemgrep  # logger-credential-disclosure: see above
        logger.warning("Using the cached access token until it expires: %s", error)
        return token.value

    @staticmethod
    def _send(method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send a request to the identity provider, without redirects.

        Args:
            method: HTTP method.
            url: URL.
            **kwargs: Other arguments of `requests.Session.request`.

        Returns:
            The response.

        Raises:
            requests.RequestException: The request failed, also for errors
                that `requests` raises as plain `OSError` (e.g., a CA bundle
                of the environment that doesn't exist).
        """
        try:
            with BackendSession() as session:
                return session.request(method, url, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise
        except OSError as exc:
            raise requests.ConnectionError(type(exc).__name__) from exc

    def _get_token_endpoint(self, client: ClientCredentialsConfig) -> str:
        """Get the token endpoint, from the config or the issuer's discovery.

        Discovery documents are fetched without holding the lock, by one
        thread at a time per issuer, and cached per issuer; failures are
        returned to further calls for `FAILURE_RETRY` seconds.

        Args:
            client: Client of the gateway at its identity provider.

        Returns:
            URL of the token endpoint.

        Raises:
            CredentialsError: Neither token endpoint nor issuer is set, or
                the discovery document can't be fetched, is not of the
                issuer, or has no valid token endpoint with an allowed URL
                scheme.
        """
        if client.token_endpoint is not None:
            return client.token_endpoint
        issuer = client.issuer
        if issuer is None:
            raise CredentialsError(
                "No issuer or token endpoint of the gateway's client"
            )
        deadline = time.monotonic() + WAIT_FACTOR * client.timeout
        with self._fetched:
            while True:
                cached = self._token_endpoints.get(issuer)
                if cached is not None:
                    return cached
                if issuer not in self._discovering:
                    failure = self._discovery_failures.get(issuer)
                    if failure is not None and time.monotonic() < failure[0]:
                        raise CredentialsError(failure[1])
                    self._discovering.add(issuer)
                    break
                self._wait(deadline, f"Discovery document of {issuer!r}")
            generation = self._generation
        endpoint: str | None = None
        error: CredentialsError | None = None
        try:
            endpoint = self._discover(issuer, client)
        except CredentialsError as exc:
            error = exc
        finally:
            with self._fetched:
                self._discovering.discard(issuer)
                if generation != self._generation:
                    pass  # cleared meanwhile: store nothing
                elif endpoint is not None:
                    self._token_endpoints[issuer] = endpoint
                    self._discovery_failures.pop(issuer, None)
                elif error is not None:
                    self._discovery_failures[issuer] = (
                        time.monotonic() + FAILURE_RETRY,
                        str(error),
                    )
                now = time.monotonic()
                for gone in [
                    k for k, v in self._discovery_failures.items() if now >= v[0]
                ]:
                    del self._discovery_failures[gone]
                self._fetched.notify_all()
        if endpoint is None:
            raise error  # type: ignore[misc]  # set if no endpoint was found
        return endpoint

    def _discover(self, issuer: str, client: ClientCredentialsConfig) -> str:
        """Read the token endpoint from the issuer's discovery document.

        The document is read without credentials and without following
        redirects, so that it comes from the issuer's URL itself.

        Args:
            issuer: Issuer.
            client: Client of the gateway at its identity provider.

        Returns:
            URL of the token endpoint.

        Raises:
            CredentialsError: The discovery document can't be fetched (also
                if redirected), is not of the issuer, or has no valid token
                endpoint with an allowed URL scheme.
        """
        url = f"{issuer.rstrip('/')}/{OIDC_CONFIG_PATH}"
        try:
            response = self._send(
                "GET",
                url,
                auth=NO_CREDENTIALS,
                headers={"Accept": "application/json"},
                timeout=client.timeout,
            )
            response.raise_for_status()
            if response.is_redirect:
                raise requests.TooManyRedirects("redirected", response=response)
            document = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise CredentialsError(
                f"OpenID Connect discovery document of {issuer!r} unavailable:"
                f" {type(exc).__name__}"
            ) from exc
        if not isinstance(document, dict) or document.get("issuer") != issuer:
            raise CredentialsError(
                f"OpenID Connect discovery document of {issuer!r} is invalid"
            )
        endpoint = document.get("token_endpoint")
        if not isinstance(endpoint, str) or not _valid_url(endpoint, client):
            raise CredentialsError(
                f"OpenID Connect discovery document of {issuer!r} has no valid token"
                f" endpoint with an allowed URL scheme ({client.url_schemes}),"
                " without user info"
            )
        return endpoint

    def _fetch_token(
        self,
        endpoint: str,
        client_id: str,
        client_secret: str,
        client: ClientCredentialsConfig,
        audience: str | None = None,
    ) -> _Token:
        """Fetch an access token with the client credentials grant.

        Args:
            endpoint: URL of the token endpoint.
            client_id: Client ID of the gateway.
            client_secret: Client secret of the gateway.
            client: Client of the gateway at its identity provider.
            audience: Audience of the token, requested in the parameter
                `client.audience_parameter`; by default, none.

        Returns:
            The access token, with the time to fetch a new one.

        Raises:
            CredentialsError: No access token was obtained.
        """
        data = {"grant_type": "client_credentials"}
        if client.scope is not None:
            data["scope"] = client.scope
        # not secret: names the backend in errors
        target = ""
        if audience is not None and client.audience_parameter is not None:
            data[client.audience_parameter] = audience
            target = f" for {client.audience_parameter} {audience!r}"
        auth: tuple[str, str] | requests.auth.AuthBase = NO_CREDENTIALS
        if client.auth_method == "client_secret_basic":
            # encoded as per RFC 6749, section 2.3.1
            auth = (quote_plus(client_id), quote_plus(client_secret))
        else:
            data.update(client_id=client_id, client_secret=client_secret)
        start = time.monotonic()
        try:
            response = self._send(
                "POST",
                endpoint,
                data=data,
                auth=auth,
                headers={"Accept": "application/json"},
                # not redirected (cf. `_send`): the client credentials must
                # not be sent elsewhere
                timeout=client.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            # without the response body, which may echo the request
            status = exc.response.status_code if exc.response is not None else None
            raise CredentialsError(
                f"No access token{target} from {endpoint}: {type(exc).__name__}"
                f" (status {status})"
            ) from exc
        if response.is_redirect:
            raise CredentialsError(
                f"No access token{target} from {endpoint}: redirected (status"
                f" {response.status_code})"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise CredentialsError(
                f"No access token{target} from {endpoint}: invalid response"
            ) from exc
        if not isinstance(body, dict):
            raise CredentialsError(
                f"No access token{target} from {endpoint}: invalid response"
            )
        value = body.get("access_token")
        token_type = body.get("token_type", "Bearer")
        if not (isinstance(value, str) and _TOKEN.fullmatch(value)) or (
            not isinstance(token_type, str) or token_type.lower() != "bearer"
        ):
            raise CredentialsError(
                f"No valid bearer access token{target} in the response of {endpoint}"
            )
        lifetime = _lifetime(body.get("expires_in"))
        margin = min(float(client.refresh_before), lifetime / 2)
        return _Token(
            value=value,
            refresh_at=start + lifetime - margin,
            expires_at=start + lifetime,
        )


# Providers of this process, for `_reset_after_fork`
_providers: "weakref.WeakSet[TokenProvider]" = weakref.WeakSet()


def _reset_after_fork() -> None:
    """Give the providers of a forked process new locks.

    The parent's locks may have been held by its other threads when it
    forked (e.g., Gunicorn or Celery workers of an app that created a
    provider before), and their fetches are never ended in the child.
    """
    for provider in list(_providers):
        provider._reset_lock()


os.register_at_fork(after_in_child=_reset_after_fork)


def token_audience(
    backend: Backend,
    entry: ServiceEntry | RegistryEntry,
    client: ClientCredentialsConfig,
) -> str | None:
    """Get the audience of the gateway's access tokens for a backend.

    Args:
        backend: The backend.
        entry: Entry of the backend in the backend list (the registry entry,
            for discovered backends).
        client: Client of the gateway at its identity provider.

    Returns:
        The `audience` of the backend's entry, or else its URL (for
        discovered backends, always their URL); `None` if audiences are not
        requested (`audience_parameter`).
    """
    if client.audience_parameter is None:
        return None
    if isinstance(entry, ServiceEntry) and entry.audience is not None:
        return entry.audience
    return backend.url


def get_credentials(
    backend: Backend,
    entry: ServiceEntry | RegistryEntry,
    client: ClientCredentialsConfig,
    provider: TokenProvider,
    auth: str | None = None,
) -> Credentials:
    """Get the credentials of a request to a backend.

    Look them up for each request (tokens are cached), so that long-running
    jobs get fresh tokens.

    Args:
        backend: The backend (cf. :py:meth:`BackendList.expand`).
        entry: Entry of the backend (cf. :py:meth:`BackendList.entry_for`).
        client: Client of the gateway at its identity provider.
        provider: Token provider of the gateway.
        auth: Name of the credentials (`client_credentials`, `basic` or
            `none`), e.g., `OutboundRequest.auth`; `None` for those of the
            backend (`backend.auth`).

    Returns:
        The credentials, a `requests` auth object; for other clients,
        :py:attr:`Credentials.headers`.

    Raises:
        CredentialsError: The credentials are unknown, or not available
            (e.g., environment variables not set, no token obtained, or
            `basic` for a backend without HTTP Basic credentials).
    """
    if auth is None:
        auth = backend.auth
    if auth == "client_credentials":
        token = provider.get_token(
            client, audience=token_audience(backend, entry, client)
        )
        return Credentials.bearer(token)
    if auth == "basic":
        basic = entry.basic if isinstance(entry, ServiceEntry) else None
        if basic is None:
            raise CredentialsError(f"No HTTP Basic credentials of {backend.url}")
        return Credentials.basic(
            _get_env(basic.username_env), _get_env(basic.password_env)
        )
    if auth == "none":
        return NO_CREDENTIALS
    raise CredentialsError(f"Unknown credentials {auth!r} of {backend.url}")


def _check_registry(entry: RegistryEntry, label: str) -> None:
    """Log how the backends of a registry entry are discovered and called.

    Args:
        entry: Registry entry.
        label: Name of the backends in messages, e.g., `TES backends`.
    """
    logger.info(
        "Discovering %s at registry %s every %d seconds, with `auth: %s`",
        label,
        entry.url,
        entry.refresh_interval,
        entry.auth,
    )
    if entry.auth == "none":
        logger.warning(
            "%s discovered at registry %s get no credentials (`auth: none`)",
            label,
            entry.url,
        )
    if entry.allow_private_addresses:
        logger.warning(
            "%s discovered at registry %s may have private addresses"
            " (`allow_private_addresses`); for local setups only",
            label,
            entry.url,
        )
    if entry.registry_allow_private_addresses:
        logger.warning(
            "Registry %s may have a private address"
            " (`registry_allow_private_addresses`)",
            entry.url,
        )


def _check_service(entry: ServiceEntry, label: str) -> list[str]:
    """Log how a configured backend is called.

    Args:
        entry: Configured backend.
        label: Name of a backend in messages, e.g., `TES backend`.

    Returns:
        The environment variables with its HTTP Basic credentials that are
        not set, if any.

    Raises:
        RuntimeError: The backend has `auth: basic`, but no `basic`.
    """
    if entry.auth == "basic":
        logger.warning(
            "%s %s: HTTP Basic credentials (`auth: basic`), a transitional mode"
            " for backends that don't accept tokens",
            label,
            entry.url,
        )
        if entry.basic is None:
            # prevented by validation, unless the config was changed later
            raise RuntimeError(f"{label} {entry.url}: `auth: basic` without `basic`")
        return _missing_env(entry.basic.username_env, entry.basic.password_env)
    if entry.auth == "none":
        logger.warning("%s %s: no authentication (`auth: none`)", label, entry.url)
    return []


def check_backends(
    entries: BackendListConfig
    | Iterable[ServiceEntry | RegistryEntry | Mapping[str, Any]],
    client: ClientCredentialsConfig,
    provider: TokenProvider,
    api: str,
    public_url: str | None = None,
    key: str = "backends",
) -> None:
    """Check the backends and their credentials on startup, and log their use.

    A warning is logged if no backend is configured, for each backend
    without token authentication (`auth: basic` or `none`), for registries
    whose backends get no tokens or may have private addresses, and for
    entries and identity providers that allow URLs with scheme `http`; the
    backends that get the gateway's tokens are logged (URLs only).

    Args:
        entries: Backend list, or its entries.
        client: Client of the gateway at its identity provider.
        provider: Token provider of the gateway, for the names of the
            environment variables of its client.
        api: API of the backends, e.g., `tes` (cf.
            :py:data:`foca.plugins.backends.API_TYPES`).
        public_url: Public URL of the gateway (`server.public_url`).
        key: Config key of the backend list, for messages, e.g.,
            `custom.tes.backends`.

    Raises:
        RuntimeError: The list is invalid for the API (cf.
            :py:class:`BackendList`), or doesn't fit the client (cf.
            :py:func:`foca.plugins.backends.check_client_credentials`); it
            has registries, but the gateway has no public URL (so that it
            might discover itself), or configured backends at or below the
            public URL (which are never used); or environment variables
            with credentials are not set: the client ID and secret if a
            backend has (or discovered backends get) `auth:
            client_credentials`, or the HTTP Basic credentials of a backend
            with `auth: basic`.
    """
    if isinstance(entries, BackendListConfig):
        entries = entries.root
    try:
        backend_list = BackendList(list(entries), api=api, public_url=public_url)
        check_client_credentials(backend_list.entries, client)
    except ValueError as exc:
        raise RuntimeError(f"Invalid backends (`{key}`): {exc}") from exc
    label = f"{api.upper()} backend"
    listed = backend_list.entries
    if not listed:
        logger.warning(
            "No %ss are configured (`%s`), so requests can't be forwarded",
            label,
            key,
        )
    if backend_list.has_registries() and public_url is None:
        raise RuntimeError(
            f"{label}s are discovered in registries (`{key}`), but"
            " `server.public_url` is not set: set it to the public URL of the"
            " gateway, so that it never forwards requests to itself"
        )
    http = [entry.id for entry in listed if "http" in entry.url_schemes]
    if http:
        logger.warning(
            "URLs with scheme 'http' are allowed (`url_schemes`) for %s:"
            " credentials are sent unencrypted; for local tests only",
            ", ".join(http),
        )
    if uses_tokens(listed) and "http" in client.url_schemes:
        logger.warning(
            "URLs with scheme 'http' are allowed for the identity provider"
            " (`client_credentials.url_schemes`): credentials are sent"
            " unencrypted; for local tests only"
        )
    missing: list[str] = []
    for entry in listed:
        if isinstance(entry, RegistryEntry):
            _check_registry(entry, f"{label}s")
        else:
            missing.extend(_check_service(entry, label))
    own = [
        entry.url
        for entry in listed
        if isinstance(entry, ServiceEntry) and backend_list.is_own(entry.url)
    ]
    if own:
        raise RuntimeError(
            f"{label}s at or below the public URL of the gateway ({public_url},"
            " `server.public_url`) would never be used, as gateways never"
            f" forward requests to themselves: {', '.join(own)}; serve the"
            " gateway under a host or path prefix of its own (and set"
            " `server.public_url` to it), or move the backends to other hosts"
        )
    # registries too: the backends that their admins verify get the token
    token_recipients = [
        entry.url
        if isinstance(entry, ServiceEntry)
        else f"those verified by registry {entry.url}"
        for entry in listed
        if entry.auth == "client_credentials"
    ]
    if token_recipients:
        parameter = client.audience_parameter
        audiences = (
            ""
            if parameter is None
            else f" (each for its own audience, requested with `{parameter}`)"
        )
        # logs the URLs of the backends, not tokens
        # nosemgrep  # logger-credential-disclosure: see above
        logger.info(
            "%ss with access tokens of the gateway%s: %s",
            label,
            audiences,
            ", ".join(token_recipients),
        )
        missing.extend(provider.missing_env())
    if missing:
        raise RuntimeError(
            f"Credentials of {label}s missing: set the environment variables"
            f" {', '.join(sorted(set(missing)))} (or `<name>_FILE`, but not"
            " both)"
        )
