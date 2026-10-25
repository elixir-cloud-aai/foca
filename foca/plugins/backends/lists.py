"""Backend lists of gateway apps (cf. :py:mod:`foca.plugins.backends`)."""

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from foca.plugins.backends.config import (
    API_TYPES,
    BackendListConfig,
    RegistryEntry,
    ServiceEntry,
)
from foca.plugins.backends.registry import get_cache
from foca.plugins.model import Backend
from foca.utils.urls import InvalidURL, normalize_url

logger = logging.getLogger(__name__)


def _location(url: str) -> tuple[str, str]:
    """Get the host, port and path of a normalized URL, without its scheme.

    Args:
        url: Normalized URL.

    Returns:
        Host with port (if any), and path without trailing slash.
    """
    rest = url.split("://", 1)[-1]
    host, _, path = rest.partition("/")
    return host, f"/{path}".rstrip("/")


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
        entries: Iterable[ServiceEntry | RegistryEntry | Mapping[str, Any]]
        | BackendListConfig,
        api: str,
        public_url: str | None = None,
    ) -> None:
        """Create the list.

        Args:
            entries: Entries, in order, as models or mappings (validated by
                :py:class:`BackendListConfig`).
            api: API of the backends, e.g., `tes`.
            public_url: Public URL of the gateway (e.g.,
                `server.public_url`).

        Raises:
            ValueError: The API has no backend type, or two entries have the
                same identifier.
        """
        if isinstance(entries, BackendListConfig):
            entries = entries.root
        self.entries = list(BackendListConfig.model_validate(list(entries)).root)
        if api not in API_TYPES:
            raise ValueError(f"No backend type for API {api!r}")
        # With a second API type, check `entry.type` against
        # `API_TYPES[api].entry_type` here.
        self.api = api
        self.public_url = public_url.rstrip("/") if public_url else None
        self._own = _location(self.public_url) if self.public_url else None

    def _is_own(self, url: str) -> bool:
        """Check whether a URL is at or below the gateway's public URL.

        Schemes are ignored: `http` and `https` URLs of the same host and
        port are the gateway's.

        Args:
            url: Normalized URL.

        Returns:
            Whether it is.
        """
        if self._own is None:
            return False
        host, path = _location(url)
        return host == self._own[0] and (
            path == self._own[1] or path.startswith(f"{self._own[1]}/")
        )

    def expand(self, refresh: bool = False) -> list[Backend]:
        """Get the candidates, in the order of the entries.

        Registry entries yield their discovered backends, in the order of
        the registry. Backends with the same URL are listed once, at the
        first position, with the settings of a configured entry if there is
        one; backends at or below the gateway's public URL are left out, and
        so are discovered backends whose identifier (their URL) is a
        configured entry's identifier for another URL, so that identifiers
        stay unique among the candidates.

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
        taken = {backend.id: backend.url for backend in configured.values()}
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
                if taken.get(backend.id, backend.url) != backend.url:
                    logger.debug(
                        "Discovered backend %s skipped: its id is taken by a"
                        " configured entry",
                        backend.url,
                    )
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
            entry.id: get_cache(entry, self.api).read_at
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

    def has_registries(self) -> bool:
        """Check whether any entry is a registry entry.

        Returns:
            Whether backends are discovered.
        """
        return any(isinstance(entry, RegistryEntry) for entry in self.entries)

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
