"""Configuration models of backend lists (cf. :py:mod:`foca.plugins.backends`)."""

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    RootModel,
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
}
"""API types of the apps' APIs, by API (cf. `GatewayApp.api`)."""


class _Model(BaseModel):
    """Base of the models: unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


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
    """Backend of the app's API (`type: ga4gh-tes`).

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
        labels: Labels, e.g., `{"region": "eu-north"}`.
        location: Location, e.g., for distance routing.

    Attributes:
        type: Type of the entry.
        url: Normalized base URL.
        url_schemes: Allowed schemes of `url`.
        id: Identifier of the backend.
        auth: Credentials for the backend.
        basic: Environment variables with HTTP Basic credentials, if any.
        labels: Labels.
        location: Location, if any.
    """

    type: Literal["ga4gh-tes"]
    url: str
    url_schemes: list[Literal["https", "http"]] = Field(default=["https"], min_length=1)
    id: str | None = Field(default=None, min_length=1)
    auth: Literal["client_credentials", "basic", "none"] = "client_credentials"
    basic: BasicCredentialsConfig | None = None
    labels: dict[str, str] = {}
    location: LocationConfig | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Normalize the URL, default the identifier, check the credentials.

        Returns:
            The entry.

        Raises:
            ValueError: The URL is invalid, has user info or another scheme,
                or `basic` is missing for `auth: basic` or set otherwise.
        """
        self.url = normalize_url(
            self.url, schemes=self.url_schemes, name="Backend URLs"
        )
        if self.id is None:
            self.id = self.url
        if (self.auth == "basic") != (self.basic is not None):
            raise ValueError(
                f"Backend {self.id!r}: `basic` is needed with, and only with,"
                " `auth: basic`"
            )
        return self

    def backend(self) -> Backend:
        """Get the candidate of the entry.

        Returns:
            Configured backend.
        """
        location = self.location
        return Backend(
            id=self.id or self.url,
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
        id: Identifier of the entry (`Backend.group` of its backends);
            defaults to the normalized URL.
        refresh_interval: Seconds between reads of the registry.
        timeout: Seconds per request to the registry.
        versions: Accepted versions of the app's API, as prefixes (e.g.,
            `1.1` accepts `1.1` and `1.1.0`, but not `1.10`).
        environments: Accepted environments of services (`environment`);
            any, also none, if empty.
        auth: Credentials for the discovered backends: the app's access
            token (`client_credentials`) or none (`none`).
        allow_private_addresses: Whether discovered backends may have hosts
            with addresses that are not public.
        labels: Labels of the discovered backends.

    Attributes:
        type: Type of the entry.
        url: Normalized base URL of the registry's API.
        id: Identifier of the entry.
        refresh_interval: Seconds between reads of the registry.
        timeout: Seconds per request to the registry.
        versions: Accepted versions of the app's API, as prefixes.
        environments: Accepted environments of services.
        auth: Credentials for the discovered backends.
        allow_private_addresses: Whether private addresses are allowed.
        labels: Labels of the discovered backends.
    """

    type: Literal["ga4gh-service-registry"]
    url: str
    id: str | None = Field(default=None, min_length=1)
    refresh_interval: PositiveInt = 300
    timeout: PositiveFloat = 10
    versions: list[Annotated[str, Field(min_length=1)]] = Field(
        default=["1.0", "1.1"], min_length=1
    )
    environments: list[Annotated[str, Field(min_length=1)]] = []
    auth: Literal["client_credentials", "none"] = "client_credentials"
    allow_private_addresses: bool = False
    labels: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Normalize the URL and default the identifier.

        Returns:
            The entry.

        Raises:
            ValueError: The URL is invalid, has user info or is no `https`
                URL.
        """
        self.url = normalize_url(self.url, schemes=["https"], name="Registry URLs")
        if self.id is None:
            self.id = self.url
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
            seen.add(entry.id or "")
        return self
