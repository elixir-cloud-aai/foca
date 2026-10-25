"""Backend lists of gateway apps (cf. :py:mod:`foca.plugins.backends`)."""

import logging
from collections.abc import Mapping, MutableMapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any
from urllib.parse import urlsplit

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


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _location(url: str) -> tuple[str, int | None, bool, str]:
    """Get the host, port and path of a normalized URL.

    Args:
        url: Normalized URL.

    Returns:
        Host, effective port, whether it is the default port of the URL's
        scheme, and path without trailing slash.
    """
    parts = urlsplit(url)
    default = _DEFAULT_PORTS.get(parts.scheme)
    port = parts.port or default
    return parts.hostname or "", port, port == default, parts.path.rstrip("/")


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
        entries: Sequence[ServiceEntry | RegistryEntry | Mapping[str, Any]]
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
            TypeError: The entries are not ordered (e.g., a set).
            ValueError: The API has no backend type, or two entries have the
                same identifier.
        """
        if isinstance(entries, AbstractSet):
            raise TypeError("Backend entries must be ordered (e.g., a list)")
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

        Schemes are ignored: URLs of the same host, effective port and path
        are the gateway's, and so are those with the default port of their
        scheme if the gateway's URL has the default port of its own (e.g.,
        `http://host` for `https://host`).

        Args:
            url: Normalized URL.

        Returns:
            Whether it is.
        """
        if self._own is None:
            return False
        host, port, default, path = _location(url)
        own_host, own_port, own_default, own_path = self._own
        return (
            host == own_host
            and (port == own_port or (default and own_default))
            and (path == own_path or path.startswith(f"{own_path}/"))
        )

    def expand(
        self,
        refresh: bool = False,
        snapshots: MutableMapping[str, str | None] | None = None,
    ) -> list[Backend]:
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
            snapshots: Mapping that gets the time of the registry read that
                the candidates of each registry entry come from (cf.
                :py:meth:`snapshots`), e.g., for provenance.

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
                urls, read_at = get_cache(entry, self.api).snapshot(refresh=refresh)
                if snapshots is not None:
                    snapshots[entry.id] = read_at
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
                    for url in urls
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
        """Get the times of the last good registry reads.

        A read between :py:meth:`expand` and this call gives its time; pass
        a mapping as `snapshots` to :py:meth:`expand` for the reads that
        candidates come from.

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
