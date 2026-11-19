"""Configuration models of backend lists (cf. :py:mod:`foca.plugins.backends`)."""

import os
import re
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    RootModel,
    field_validator,
    model_validator,
)

from foca.plugins.model import Backend, Location
from foca.utils.urls import normalize_url


@dataclass(frozen=True)
class ApiType:
    """API type of backends, as listed in service registries.

    Attributes:
        group: Group of the GA4GH service type, e.g., `org.ga4gh`.
        artifact: Artifact of the GA4GH service type, e.g., `tes`.
        path: API path, removed from the URLs of discovered services, as
            registries list the URLs that the paths of the API are relative
            to, whereas apps append the API path themselves.
        entry_type: Type of backend list entries of the API.
    """

    group: str
    artifact: str
    path: str
    entry_type: str


API_TYPES: dict[str, ApiType] = {
    "tes": ApiType("org.ga4gh", "tes", "/ga4gh/tes/v1", "ga4gh-tes"),
    "wes": ApiType("org.ga4gh", "wes", "/ga4gh/wes/v1", "ga4gh-wes"),
}
"""API types of the apps' APIs, by API (cf. `GatewayApp.api`)."""

# Absolute paths of unreserved characters (RFC 3986, section 2.3)
_API_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)+")


def _check_path(value: str) -> None:
    """Check an API path of a backend.

    Args:
        value: API path, with or without trailing slash.

    Raises:
        ValueError: The path is neither empty nor absolute, has characters
            other than unreserved ones (e.g., a query), or empty segments or
            segments `.` or `..`.
    """
    if not value:
        return
    if not value.startswith("/"):
        raise ValueError("API path must be absolute")
    path = value.removesuffix("/")
    if path and (not _API_PATH.fullmatch(path) or {".", ".."} & set(path.split("/"))):
        raise ValueError(
            "API path must have unreserved characters only, and no empty"
            " segments or segments `.` or `..`"
        )


class _Model(BaseModel):
    """Base of the models: unknown keys are errors, without their values."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class BasicCredentialsConfig(_Model):
    """Environment variables with HTTP Basic credentials of a backend.

    Args:
        username_env: Name of the variable with the user name.
        password_env: Name of the variable with the password.

    Attributes:
        username_env: Name of the variable with the user name.
        password_env: Name of the variable with the password.
    """

    username_env: str = Field(min_length=1)
    password_env: str = Field(min_length=1)


class LocationConfig(_Model):
    """Location of a backend, set by the admin.

    Args:
        latitude: Latitude in degrees.
        longitude: Longitude in degrees.

    Attributes:
        latitude: Latitude in degrees.
        longitude: Longitude in degrees.
    """

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class ServiceEntry(_Model):
    """Backend of the app's API (`type: ga4gh-tes` or `type: ga4gh-wes`).

    The type must be that of the app's API (`entry_type` of its API type in
    :py:data:`API_TYPES`), which :py:class:`foca.plugins.backends.BackendList`
    checks.

    Args:
        type: Type of the entry.
        url: Base URL of the backend, without the API path and user info;
            normalized (cf. :py:func:`foca.utils.urls.normalize_url`).
        url_schemes: Allowed schemes of `url`; `https` only by default
            (dev and test setups may allow `http`).
        id: Identifier of the backend; defaults to the normalized URL.
        auth: Credentials for the backend: the app's access token
            (`client_credentials`), HTTP Basic credentials from the
            environment (`basic`, cf. `basic`), or none (`none`).
        basic: Environment variables with the HTTP Basic credentials; set
            if, and only if, `auth` is `basic`.
        audience: Audience of the gateway's access tokens for the backend
            (e.g., its client ID at the identity provider), requested with
            `audience_parameter` of the gateway's client
            (:py:class:`ClientCredentialsConfig`); by default, the URL of the
            backend. Only with `auth: client_credentials`.
        labels: Labels, e.g., `{"region": "eu-north"}`.
        location: Location, e.g., for distance routing.
        api_path: Path of the API, relative to `url`, for backends that
            serve it elsewhere than its specification says; `/` (or empty)
            if it is at `url` itself. By default, the path of the entry
            type's API (e.g., `/ga4gh/tes/v1`; cf. :py:data:`API_TYPES`).
            Apps send their requests to it; FOCA itself doesn't use it.

    Attributes:
        type: Type of the entry.
        url: Normalized base URL.
        url_schemes: Allowed schemes of `url`.
        id: Identifier of the backend.
        auth: Credentials for the backend.
        basic: Environment variables with HTTP Basic credentials, if any.
        audience: Audience of the gateway's access tokens, if not the URL.
        labels: Labels.
        location: Location, if any.
        api_path: Path of the API, relative to `url`, without trailing
            slash (empty if it is at `url` itself); set when validated.
    """

    type: Literal["ga4gh-tes", "ga4gh-wes"]
    url: str
    url_schemes: list[Literal["https", "http"]] = Field(default=["https"], min_length=1)
    # the normalized URL by default (set when validated)
    id: str = Field(default="", min_length=1)
    auth: Literal["client_credentials", "basic", "none"] = "client_credentials"
    basic: BasicCredentialsConfig | None = None
    audience: str | None = Field(default=None, min_length=1)
    labels: dict[str, str] = {}
    location: LocationConfig | None = None
    # the API path of the entry type by default (set when validated)
    api_path: str | None = None

    @field_validator("api_path")
    @classmethod
    def _check_api_path(cls, value: str | None) -> str | None:
        """Check the API path.

        Leaves the path as given, so that validators of subclasses that
        redefine the field (e.g., proTES's) get it as given too; trailing
        slashes are removed when the entry is validated (cf. `_check`).

        Args:
            value: API path, as given.

        Returns:
            The API path, unchanged.

        Raises:
            ValueError: The path is neither empty nor absolute, has
                characters other than unreserved ones (e.g., a query), or
                empty segments or segments `.` or `..`.
        """
        if value is not None:
            _check_path(value)
        return value

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Normalize the URL, default the identifier and API path, check auth.

        The API path is checked again, as defaults of subclasses (e.g.,
        apps' own) are not validated by field validators.

        Returns:
            The entry.

        Raises:
            ValueError: The URL is invalid, has user info or another scheme,
                the type has no API type and the entry no API path, the API
                path is invalid, or `basic` is missing for `auth: basic` or
                set otherwise.
        """
        self.url = normalize_url(
            self.url, schemes=self.url_schemes, name="Backend URLs"
        )
        if not self.id:
            self.id = self.url
        if self.api_path is None:
            paths = {t.entry_type: t.path for t in API_TYPES.values()}
            if self.type not in paths:
                raise ValueError(f"No API type for backend type {self.type!r}")
            self.api_path = paths[self.type]
        _check_path(self.api_path)
        self.api_path = self.api_path.removesuffix("/")
        if (self.auth == "basic") != (self.basic is not None):
            raise ValueError(
                f"Backend {self.id!r}: `basic` is needed with, and only with,"
                " `auth: basic`"
            )
        audience = self.audience
        if audience is not None:
            if self.auth != "client_credentials":
                raise ValueError(
                    f"Backend {self.id!r}: `audience` needs `auth: client_credentials`"
                )
            if audience != audience.strip():
                raise ValueError(
                    f"Backend {self.id!r}: `audience` must not have surrounding"
                    " whitespace"
                )
        return self

    def backend(self) -> Backend:
        """Get the candidate of the entry.

        Returns:
            Configured backend.
        """
        location = self.location
        return Backend(
            id=self.id,
            url=self.url,
            source="configured",
            labels=dict(self.labels),
            location=Location(location.latitude, location.longitude)
            if location
            else None,
            auth=self.auth,
        )


class RegistryEntry(_Model):
    """Service registry whose verified backends are used.

    Args:
        type: Type of the entry (`ga4gh-service-registry`).
        url: Base URL of the registry's API (`https`), e.g.,
            `https://registry.example.org/ga4gh/registry/v1`.
        url_schemes: Allowed schemes of `url`; `https` only by default
            (tests may allow `http`).
        id: Identifier of the entry (`Backend.group` of its backends);
            defaults to the normalized URL.
        refresh_interval: Seconds between reads of the registry.
        max_stale: Seconds that the last good list of backends is used
            for while the registry cannot be read; then, the entry yields
            no candidates until the next good read. At least
            `refresh_interval`.
        timeout: Seconds per request to the registry.
        versions: Accepted versions of the app's API, as prefixes (e.g.,
            `1.1` accepts `1.1` and `1.1.0`, but not `1.10`).
        environments: Accepted environments of services (`environment`);
            any, also none, if empty.
        auth: Credentials for the discovered backends: the app's access
            token (`client_credentials`) or none (`none`).
        allow_private_addresses: Whether discovered backends may have hosts
            with addresses that are not public.
        registry_allow_private_addresses: Whether the registry's own host
            may have addresses that are not public (e.g., a registry in the
            same cluster).
        registry_ca_bundle: Path of a file (PEM) or directory (hashed
            names, as by `openssl rehash`) of the certificate authorities
            that the registry's TLS certificate is verified with, instead of
            the public ones (e.g., for a registry with a certificate of a
            private certificate authority); relative paths are resolved
            against the working directory when validated. Verification
            can't be turned off.
        labels: Labels of the discovered backends.

    Attributes:
        type: Type of the entry.
        url: Normalized base URL of the registry's API.
        url_schemes: Allowed schemes of `url`.
        id: Identifier of the entry.
        refresh_interval: Seconds between reads of the registry.
        max_stale: Seconds that the last good list is used for.
        timeout: Seconds per request to the registry.
        versions: Accepted versions of the app's API, as prefixes.
        environments: Accepted environments of services.
        auth: Credentials for the discovered backends.
        allow_private_addresses: Whether private addresses of discovered
            backends are allowed.
        registry_allow_private_addresses: Whether private addresses of the
            registry are allowed.
        registry_ca_bundle: Absolute path of the registry's CA bundle, if
            any.
        labels: Labels of the discovered backends.
    """

    type: Literal["ga4gh-service-registry"]
    url: str
    url_schemes: list[Literal["https", "http"]] = Field(default=["https"], min_length=1)
    # the normalized URL by default (set when validated)
    id: str = Field(default="", min_length=1)
    refresh_interval: PositiveInt = 300
    max_stale: PositiveInt = 3600
    timeout: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 10
    versions: list[Annotated[str, Field(min_length=1)]] = Field(
        default=["1.0", "1.1"], min_length=1
    )
    environments: list[Annotated[str, Field(min_length=1)]] = []
    auth: Literal["client_credentials", "none"] = "client_credentials"
    allow_private_addresses: bool = False
    registry_allow_private_addresses: bool = False
    # strict: paths only, never booleans or bytes (no way to turn verification
    # off), as `GuardedClient` checks again (`foca.utils.http._ca_bundle_path`);
    # absolute here already, so that the cache key (`get_cache()`) is too
    registry_ca_bundle: Annotated[str, Field(min_length=1, strict=True)] | None = None
    labels: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Normalize URL and CA bundle, default the identifier, check `max_stale`.

        Returns:
            The entry.

        Raises:
            ValueError: The URL is invalid, has user info or another scheme,
                or `max_stale` is less than `refresh_interval`.
        """
        self.url = normalize_url(
            self.url, schemes=self.url_schemes, name="Registry URLs"
        )
        if not self.id:
            self.id = self.url
        if self.registry_ca_bundle is not None:
            # so that later changes of the working directory don't change it
            self.registry_ca_bundle = os.path.abspath(self.registry_ca_bundle)
        if self.max_stale < self.refresh_interval:
            raise ValueError(
                f"Registry {self.id!r}: `max_stale` must be at least `refresh_interval`"
            )
        return self


BackendEntry = Annotated[ServiceEntry | RegistryEntry, Field(discriminator="type")]
"""Entry of a backend list."""


class BackendListConfig(RootModel[list[BackendEntry]]):
    """Ordered list of backend entries, with unique identifiers.

    Examples:
        >>> entries = BackendListConfig.model_validate(
        ...     [{"type": "ga4gh-tes", "url": "https://TES.example.org/"}]
        ... )
        >>> entries.root[0].id
        'https://tes.example.org'
    """

    # Of the outermost model, so that errors leave out what entries hold
    model_config = ConfigDict(hide_input_in_errors=True)

    root: list[BackendEntry] = []

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        """Check that the identifiers of the entries are unique.

        Returns:
            The list.

        Raises:
            ValueError: Two entries have the same identifier.
        """
        seen: set[str] = set()
        for entry in self.root:
            if entry.id in seen:
                raise ValueError(f"Backend entries with the same id: {entry.id}")
            seen.add(entry.id)
        return self
