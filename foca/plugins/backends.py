"""Typed backend lists of gateway apps, with service registry discovery.

Gateway apps configure the backends that requests may be forwarded to as
one ordered list of typed entries, under a key of their own (e.g., proTES:
`custom.tes.backends`):

```yaml
backends:
  - type: ga4gh-tes
    url: https://tes-a.example.org
    auth: client_credentials       # client_credentials | basic | none
    labels: {region: eu-north}
    location: {latitude: 60.17, longitude: 24.94}
  - type: ga4gh-service-registry
    id: cloud-registry
    url: https://registry.example.org/ga4gh/registry/v1
    versions: ["1.1"]
  - type: ga4gh-tes
    url: https://tes-fallback.example.org
    auth: basic
    basic: {username_env: TES_FALLBACK_USER, password_env: TES_FALLBACK_PASSWORD}
```

:py:class:`BackendList` expands the entries, in order, into the candidates
of a request (:py:class:`foca.plugins.Backend`). Registry entries yield the
backends of the app's API that the registry lists as **verified** (`GET
/verifications`, an extension of the ELIXIR Cloud Registry) with their
current URL and API type, of an accepted version and environment, with
`https` URLs whose hosts have public addresses only (unless allowed). The
registry is the trust anchor, configured by the deployment: it is read
without access token, over `https`, without following redirects (cf.
:py:mod:`foca.utils.http`).

Each process caches the backends of each registry entry, refreshing them
every `refresh_interval` seconds; it keeps the last good list while the
registry is unreachable or answers invalidly (retrying after
`RETRY_INTERVAL` seconds at most), and, for one more read, backends whose
hosts cannot be resolved for the time being. Before the first good read, a
registry yields no candidates.

Plain pydantic models and the guarded HTTP client; no web framework.
"""

import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

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
from foca.utils.http import (
    AddressNotAllowed,
    HostNotResolved,
    HttpError,
    check_public_host,
    fetch_json,
)
from foca.utils.urls import InvalidURL, normalize_url

logger = logging.getLogger(__name__)

RETRY_INTERVAL = 30
"""Maximum seconds until a registry is read again after a failed read."""

MIN_REFRESH_INTERVAL = 10
"""Minimum seconds between reads of a registry requested by
:py:meth:`BackendList.find`."""


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


class RegistryError(Exception):
    """A registry response is invalid."""


@dataclass
class _Selection:
    """Backends selected from the services of a registry.

    Attributes:
        urls: Base URLs of the discovered backends, by service identifier,
            in the order of the registry.
        rejected: Why verified services of the API were not used.
        unresolved: Services whose backends were kept although their hosts
            could not be resolved.
    """

    urls: dict[str, str] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    unresolved: set[str] = field(default_factory=set)


def _same(first: object, second: str) -> bool:
    """Compare parts of API types, case-insensitively.

    Args:
        first: Part of an API type, as listed.
        second: Expected part.

    Returns:
        Whether the first is a string equal to the second, ignoring case.
    """
    return isinstance(first, str) and first.casefold() == second.casefold()


def _is_api(api_type: object, api: ApiType) -> bool:
    """Check whether a service type is that of the API.

    Args:
        api_type: Type of a service (`type`), as listed.
        api: API type.

    Returns:
        Whether group and artifact match, ignoring case.
    """
    return (
        isinstance(api_type, Mapping)
        and _same(api_type.get("group"), api.group)
        and _same(api_type.get("artifact"), api.artifact)
    )


def _version_accepted(version: object, versions: Sequence[str]) -> bool:
    """Check whether the API version of a service is accepted.

    Args:
        version: Version of the service's type.
        versions: Accepted versions, as prefixes.

    Returns:
        Whether the version is one of `versions`, or starts with one of them
        and a dot.
    """
    return isinstance(version, str) and any(
        version == accepted or version.startswith(f"{accepted}.")
        for accepted in versions
    )


def _verifications(document: object) -> dict[str, tuple[object, object]]:
    """Get the verifications of a registry, by service identifier.

    Args:
        document: Response of `GET /verifications`.

    Returns:
        URL and API type of each verified service, as verified.

    Raises:
        RegistryError: The response is not a list of objects with string
            identifiers.
    """
    if not isinstance(document, list):
        raise RegistryError("Verifications are not a list")
    verified: dict[str, tuple[object, object]] = {}
    for item in document:
        if not isinstance(item, dict) or not isinstance(item.get("service_id"), str):
            raise RegistryError("Verifications without service identifier")
        verified[item["service_id"]] = (item.get("url"), item.get("type"))
    return verified


def select_services(
    services: object,
    verifications: object,
    entry: RegistryEntry,
    api: ApiType,
    previous: Mapping[str, str] | None = None,
) -> _Selection:
    """Select the backends to use from the services of a registry.

    Args:
        services: Response of `GET /services`.
        verifications: Response of `GET /verifications`.
        entry: Registry entry.
        api: API type of the backends.
        previous: Base URLs discovered before, by service identifier, which
            are kept for this read if their hosts cannot be resolved.

    Returns:
        Selected backends, unique by URL, and why verified services of the
        API were rejected.

    Raises:
        RegistryError: A response is not a list (of objects, for
            verifications).
    """
    if not isinstance(services, list):
        raise RegistryError("Services are not a list")
    verified = _verifications(verifications)
    previous = previous or {}
    selection = _Selection()
    for service in services:
        if not isinstance(service, dict):
            continue
        api_type = service.get("type")
        if (
            not _is_api(api_type, api)
            or not _version_accepted(api_type.get("version"), entry.versions)  # type: ignore[union-attr]
            or (
                entry.environments
                and service.get("environment") not in entry.environments
            )
        ):
            continue
        service_id = service.get("id")
        if not isinstance(service_id, str) or service_id not in verified:
            logger.debug("Registry service %r is not verified", service_id)
            continue
        url = service.get("url")
        verified_url, verified_type = verified[service_id]
        if (
            not isinstance(url, str)
            or url != verified_url
            or not _is_api(verified_type, api)
        ):
            selection.rejected.append(
                f"{service_id!r}: URL or API type differ from the verified ones"
            )
            continue
        try:
            base = normalize_url(url, schemes=["https"], name="Service URLs")
            base = base.removesuffix(api.path)
            if not entry.allow_private_addresses:
                _check_host(service_id, base, previous, selection)
        except (InvalidURL, AddressNotAllowed, HostNotResolved) as exc:
            selection.rejected.append(f"{service_id!r}: {exc}")
            continue
        if base not in selection.urls.values():
            selection.urls[service_id] = base
    return selection


def _check_host(
    service_id: str, url: str, previous: Mapping[str, str], selection: _Selection
) -> None:
    """Check that the host of a discovered backend has public addresses only.

    Args:
        service_id: Identifier of the service.
        url: Base URL of the backend.
        previous: Base URLs discovered before, by service identifier.
        selection: Selection, whose `unresolved` services are updated.

    Raises:
        AddressNotAllowed: The host has an address that is not public.
        HostNotResolved: The host cannot be resolved, and the backend was
            not discovered with the same URL before.
    """
    try:
        check_public_host(url)
    except HostNotResolved:
        if previous.get(service_id) != url:
            raise
        # requests to the backend check its address themselves
        logger.warning("Host of backend %s not resolved; keeping it for this read", url)
        selection.unresolved.add(service_id)


class RegistryCache:
    """Backends of a registry entry, cached in this process.

    Thread-safe. The registry is read by one thread at a time; meanwhile,
    other threads get the cached backends, except before the first read,
    which they wait for.

    Attributes:
        entry: Registry entry.
        api: API type of the backends.
        read_at: RFC 3339 UTC time of the last good read, if any.
    """

    def __init__(self, entry: RegistryEntry, api: ApiType) -> None:
        """Create an empty cache.

        Args:
            entry: Registry entry.
            api: API type of the backends.
        """
        self.entry = entry
        self.api = api
        self.read_at: str | None = None
        self._lock = threading.Lock()
        self._urls: dict[str, str] = {}
        self._read = False
        self._last_read = 0.0
        self._next_read = 0.0
        self._rejected: set[str] = set()
        self._unresolved: set[str] = set()

    def reset_lock(self) -> None:
        """Replace the lock, e.g., in a forked process."""
        self._lock = threading.Lock()

    def urls(self, refresh: bool = False) -> list[str]:
        """Get the base URLs of the backends, reading the registry when due.

        Args:
            refresh: Whether to read the registry now, unless it was read in
                the last `MIN_REFRESH_INTERVAL` seconds.

        Returns:
            Base URLs, in the order of the registry; none before the first
            good read.
        """
        if not self._due(refresh):
            return list(self._urls.values())
        if not self._lock.acquire(blocking=not self._read):
            return list(self._urls.values())
        try:
            if self._due(refresh):
                self._refresh()
            return list(self._urls.values())
        finally:
            self._lock.release()

    def _due(self, refresh: bool) -> bool:
        """Check whether the registry is to be read.

        Args:
            refresh: Whether a read is requested.

        Returns:
            Whether the next read is due, or a read is requested and the last
            one is at least `MIN_REFRESH_INTERVAL` seconds ago.
        """
        now = time.monotonic()
        return now >= self._next_read or (
            refresh and now - self._last_read >= MIN_REFRESH_INTERVAL
        )

    def _refresh(self) -> None:
        """Read the registry, keeping the last good backends on errors.

        Errors of any kind are logged, so that registry responses never fail
        the requests that read the registry.
        """
        entry = self.entry
        now = time.monotonic()
        self._last_read = now
        self._next_read = now + min(entry.refresh_interval, RETRY_INTERVAL)
        try:
            verifications = fetch_json(f"{entry.url}/verifications", entry.timeout)
            services = fetch_json(f"{entry.url}/services", entry.timeout)
            selection = select_services(
                services,
                verifications,
                entry,
                self.api,
                # backends kept once already are not kept again
                previous={
                    service_id: url
                    for service_id, url in self._urls.items()
                    if service_id not in self._unresolved
                },
            )
        except Exception as exc:
            # `HttpError` and `RegistryError` for failed reads; others are bugs
            expected = isinstance(exc, HttpError | RegistryError)
            log = logger.warning if expected else logger.exception
            log(
                "Backends not discovered at registry %s (%s); keeping %d"
                " backends discovered earlier",
                entry.url,
                exc,
                len(self._urls),
            )
            return
        self._next_read = now + entry.refresh_interval
        for reason in selection.rejected:
            if reason not in self._rejected:
                logger.warning("Registry service not used: %s", reason)
        self._rejected = set(selection.rejected)
        self._unresolved = selection.unresolved
        if not self._read or list(selection.urls.values()) != list(self._urls.values()):
            logger.info(
                "Backends discovered at registry %s: %s",
                entry.url,
                ", ".join(selection.urls.values()) or "none",
            )
        self._urls = selection.urls
        self._read = True
        self.read_at = (
            datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )


# Caches of this process, by API and registry entry
_caches: dict[tuple[str, str], RegistryCache] = {}
_caches_lock = threading.Lock()


def _reset_after_fork() -> None:
    """Replace the locks in a forked process."""
    global _caches_lock  # noqa: PLW0603
    _caches_lock = threading.Lock()
    for cache in _caches.values():
        cache.reset_lock()


os.register_at_fork(after_in_child=_reset_after_fork)


def get_cache(entry: RegistryEntry, api: str) -> RegistryCache:
    """Get the cache of a registry entry in this process.

    Args:
        entry: Registry entry.
        api: API of the backends, e.g., `tes`.

    Returns:
        Cache; a new one if the entry's configuration changed.
    """
    key = (api, entry.model_dump_json())
    with _caches_lock:
        cache = _caches.get(key)
        if cache is None:
            cache = _caches[key] = RegistryCache(entry, API_TYPES[api])
        return cache


def clear_caches() -> None:
    """Forget the backends of all registries, e.g., in tests."""
    with _caches_lock:
        _caches.clear()


class BackendList:
    """Backends of a gateway app, from its backend list.

    Cheap to create: registry caches are shared per process.

    Attributes:
        entries: Entries, in order.
        api: API of the backends, e.g., `tes`.
        public_url: Public URL of the gateway, normalized, if known:
            backends at or below it are never candidates.
    """

    def __init__(
        self,
        entries: Iterable[ServiceEntry | RegistryEntry] | BackendListConfig,
        api: str,
        public_url: str | None = None,
    ) -> None:
        """Create the list.

        Args:
            entries: Entries, in order (validated, e.g., by
                :py:class:`BackendListConfig`).
            api: API of the backends, e.g., `tes`.
            public_url: Public URL of the gateway (e.g.,
                `server.public_url`).

        Raises:
            ValueError: The API has no backend type, an entry is of the
                type of another API, or two entries have the same
                identifier.
        """
        if isinstance(entries, BackendListConfig):
            entries = entries.root
        self.entries = list(BackendListConfig.model_validate(list(entries)).root)
        if api not in API_TYPES:
            raise ValueError(f"No backend type for API {api!r}")
        self.api = api
        entry_type = API_TYPES[api].entry_type
        for entry in self.entries:
            if isinstance(entry, ServiceEntry) and entry.type != entry_type:
                raise ValueError(  # pragma: no cover (one API type for now)
                    f"Backend {entry.id!r} is no {entry_type} backend"
                )
        self.public_url = public_url.rstrip("/") if public_url else None

    def _is_own(self, url: str) -> bool:
        """Check whether a URL is at or below the gateway's public URL.

        Args:
            url: Normalized URL.

        Returns:
            Whether it is.
        """
        if self.public_url is None:
            return False
        return url == self.public_url or url.startswith(f"{self.public_url}/")

    def expand(self, refresh: bool = False) -> list[Backend]:
        """Get the candidates, in the order of the entries.

        Registry entries yield their discovered backends, in the order of
        the registry. Backends with the same URL are listed once, at the
        first position, with the settings of a configured entry if there is
        one; backends at or below the gateway's public URL are left out.

        Args:
            refresh: Whether to read the registries now (rate-limited).

        Returns:
            Candidates.
        """
        configured = {
            entry.url: entry.backend()
            for entry in reversed(self.entries)
            if isinstance(entry, ServiceEntry)
        }
        candidates: list[Backend] = []
        seen: set[str] = set()
        for entry in self.entries:
            if isinstance(entry, ServiceEntry):
                found = [configured[entry.url]]
            else:
                found = [
                    configured.get(url)
                    or Backend(
                        id=url,
                        url=url,
                        source="discovered",
                        group=entry.id,
                        labels=dict(entry.labels),
                        auth=entry.auth,
                    )
                    for url in get_cache(entry, self.api).urls(refresh=refresh)
                ]
            for backend in found:
                if backend.url in seen or self._is_own(backend.url):
                    continue
                seen.add(backend.url)
                candidates.append(backend)
        return candidates

    def snapshots(self) -> dict[str, str | None]:
        """Get the times of the registry reads that candidates come from.

        Returns:
            RFC 3339 UTC time of the last good read (`None` before it), by
            registry entry identifier, e.g., for provenance.
        """
        return {
            entry.id or entry.url: get_cache(entry, self.api).read_at
            for entry in self.entries
            if isinstance(entry, RegistryEntry)
        }

    def entry_for(self, backend: Backend) -> ServiceEntry | RegistryEntry:
        """Get the entry behind a candidate, e.g., for its credentials.

        Args:
            backend: Candidate (cf. :py:meth:`expand`).

        Returns:
            Configured entry of the backend, or the registry entry that
            discovered it.

        Raises:
            KeyError: No entry yields the backend.
        """
        key = backend.group if backend.source == "discovered" else backend.id
        for entry in self.entries:
            if entry.id == key:
                return entry
        raise KeyError(f"No backend entry for {backend.id!r}")

    def find(self, url: str, refresh: bool = True) -> Backend | None:
        """Look a backend up by URL, e.g., in another process than routing.

        Args:
            url: Base URL of the backend (normalized here).
            refresh: Whether to read the registries again if no candidate
                has the URL (rate-limited to one read per
                `MIN_REFRESH_INTERVAL` seconds per registry).

        Returns:
            Candidate with the URL, or `None` if there is none.
        """
        try:
            key = normalize_url(url)
        except InvalidURL:
            return None
        for again in (False, True) if refresh else (False,):
            for backend in self.expand(refresh=again):
                if backend.url == key:
                    return backend
        return None

    def describe(self) -> list[dict[str, Any]]:
        """Describe the candidates for clients, e.g., in the service info.

        Returns:
            Identifier, URL, source, group and labels of each candidate,
            without credentials.
        """
        return [
            {
                "id": backend.id,
                "url": backend.url,
                "source": backend.source,
                "group": backend.group,
                "labels": dict(backend.labels),
            }
            for backend in self.expand()
        ]
