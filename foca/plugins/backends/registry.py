"""Discovery of backends in service registries, cached per process.

Cf. :py:mod:`foca.plugins.backends`.
"""

import logging
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from foca.plugins.backends.config import API_TYPES, ApiType, RegistryEntry
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
