"""Tests of backend lists (`foca.plugins.backends`), without network."""

import logging
import os
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from foca.plugins import Backend, backends
from foca.plugins.backends import (
    BackendList,
    BackendListConfig,
    RegistryEntry,
    ServiceEntry,
    select_services,
)
from foca.plugins.backends import (
    registry as discovery,
)
from foca.utils.http import AddressNotAllowed, HostNotResolved, HttpError

REGISTRY = "https://registry.example.org/ga4gh/registry/v1"
TES = {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}


def service(service_id: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """Build a registry service.

    Args:
        service_id: Identifier.
        url: URL.
        kwargs: Other fields.

    Returns:
        Service.
    """
    return {"id": service_id, "url": url, "type": TES, **kwargs}


class Registry:
    """Fake registry, served by a patched `fetch_json`.

    Attributes:
        services: Services.
        verifications: Verifications; by default, of all services.
        error: Error to raise, if any.
        reads: Number of reads of the services.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self.services: Any = []
        self.verifications: Any = None
        self.error: Exception | None = None
        self.reads = 0

    def fetch(self, url: str, timeout: float) -> Any:
        """Answer a request.

        Args:
            url: URL.
            timeout: Timeout.

        Returns:
            Document.

        Raises:
            Exception: The configured error.
        """
        if self.error is not None:
            raise self.error
        if url.endswith("/verifications"):
            if self.verifications is not None:
                return self.verifications
            return [
                {"service_id": s["id"], "url": s["url"], "type": s["type"]}
                for s in self.services
                if isinstance(s, dict)
            ]
        self.reads += 1
        return self.services


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[Registry]:
    """Serve a fake registry; hosts with `internal` in their name are private.

    Yields:
        Registry.
    """
    fake = Registry()
    hosts: dict[str, Exception] = {}

    def check(url: str) -> None:
        if "internal" in url:
            raise AddressNotAllowed(f"{url} is private")
        if url in hosts:
            raise hosts[url]

    monkeypatch.setattr(discovery, "fetch_json", fake.fetch)
    monkeypatch.setattr(discovery, "check_public_host", check)
    fake.hosts = hosts  # type: ignore[attr-defined]
    backends.clear_caches()
    yield fake
    backends.clear_caches()


def make_list(entries: list[dict[str, Any]], **kwargs: Any) -> BackendList:
    """Create a backend list.

    Args:
        entries: Entries.
        kwargs: Other arguments.

    Returns:
        List.
    """
    return BackendList(BackendListConfig.model_validate(entries), api="tes", **kwargs)


def test_entries() -> None:
    """Entries are typed, normalized and unique."""
    entries = BackendListConfig.model_validate(
        [
            {"type": "ga4gh-tes", "url": "https://TES.example.org:443/"},
            {
                "type": "ga4gh-tes",
                "url": "http://tes.example.org",
                "url_schemes": ["https", "http"],
                "id": "local",
                "auth": "basic",
                "basic": {"username_env": "U", "password_env": "P"},
            },
            {"type": "ga4gh-service-registry", "url": REGISTRY},
        ]
    ).root
    assert entries[0].id == "https://tes.example.org"
    assert entries[1].id == "local"
    assert entries[2].id == REGISTRY
    for invalid in (
        [{"type": "ga4gh-tes", "url": "http://tes.example.org"}],
        [{"type": "ga4gh-tes", "url": "https://u:p@tes.example.org"}],
        [{"type": "ga4gh-tes", "url": "https://a.example.org", "auth": "basic"}],
        [
            {
                "type": "ga4gh-tes",
                "url": "https://a.example.org",
                "basic": {"username_env": "U", "password_env": "P"},
            }
        ],
        [{"type": "ga4gh-service-registry", "url": "http://registry.example.org"}],
        [{"type": "ga4gh-tes", "url": "https://a.example.org"}] * 2,
        [{"type": "ga4gh-wes", "url": "https://a.example.org"}],
    ):
        with pytest.raises(ValidationError):
            BackendListConfig.model_validate(invalid)


def test_unknown_api() -> None:
    """Only APIs with a backend type are supported."""
    with pytest.raises(ValueError, match="No backend type"):
        BackendList([], api="wes")


def test_expand(registry: Registry) -> None:
    """Entries expand in order, without duplicates or the gateway itself."""
    registry.services = [
        service("s1", "https://tes-b.example.org/ga4gh/tes/v1"),
        service("s2", "https://tes-a.example.org"),
        service("s3", "https://gw.example.org/sub"),
    ]
    blist = make_list(
        [
            {
                "type": "ga4gh-tes",
                "url": "https://tes-c.example.org",
                "labels": {"region": "eu"},
                "location": {"latitude": 1, "longitude": 2},
            },
            {
                "type": "ga4gh-service-registry",
                "url": REGISTRY,
                "id": "reg",
                "auth": "none",
                "labels": {"source": "registry"},
            },
            {"type": "ga4gh-tes", "url": "https://tes-a.example.org", "id": "a"},
            {"type": "ga4gh-tes", "url": "https://gw.example.org"},
        ],
        public_url="https://gw.example.org/",
    )
    candidates = blist.expand()
    assert [(b.id, b.source, b.group) for b in candidates] == [
        ("https://tes-c.example.org", "configured", None),
        ("https://tes-b.example.org", "discovered", "reg"),
        ("a", "configured", None),
    ]
    assert candidates[0].location is not None
    assert candidates[0].labels == {"region": "eu"}
    assert candidates[1].auth == "none"
    assert candidates[1].labels == {"source": "registry"}
    assert candidates[2].auth == "client_credentials"
    assert blist.snapshots()["reg"] is not None
    assert blist.entry_for(candidates[1]).id == "reg"
    entry = blist.entry_for(candidates[0])
    assert isinstance(entry, ServiceEntry)
    with pytest.raises(KeyError):
        blist.entry_for(Backend(id="x", url="https://x.example.org"))
    assert blist.describe()[1] == {
        "id": "https://tes-b.example.org",
        "url": "https://tes-b.example.org",
        "source": "discovered",
        "group": "reg",
        "labels": {"source": "registry"},
    }


def test_several_registries(registry: Registry) -> None:
    """Several registries yield their backends in order."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    blist = make_list(
        [
            {"type": "ga4gh-service-registry", "url": REGISTRY, "id": "one"},
            {"type": "ga4gh-service-registry", "url": REGISTRY, "id": "two"},
        ]
    )
    assert [(b.url, b.group) for b in blist.expand()] == [
        ("https://tes-a.example.org", "one")
    ]
    assert BackendList(blist.entries, api="tes").expand()[0].group == "one"


def test_filters(registry: Registry, caplog: pytest.LogCaptureFixture) -> None:
    """Only verified services of the API, version and environment are used."""
    other = {"group": "org.ga4gh", "artifact": "wes", "version": "1.1.0"}
    registry.services = [
        "not a service",
        service("wes", "https://wes.example.org", type=other),
        service("old", "https://old.example.org", type={**TES, "version": "1.10"}),
        service("noversion", "https://nv.example.org", type={**TES, "version": 1}),
        service("dev", "https://dev.example.org", environment="dev"),
        service("unverified", "https://u.example.org", environment="prod"),
        service("changed", "https://c.example.org", environment="prod"),
        service("http", "http://h.example.org", environment="prod"),
        service("private", "https://internal.example.org", environment="prod"),
        service("ok", "https://ok.example.org", environment="prod"),
        service("dup", "https://ok.example.org/", environment="prod"),
        service(7, "https://n.example.org", environment="prod"),  # type: ignore[arg-type]
    ]
    registry.verifications = [
        {"service_id": s["id"], "url": s["url"], "type": s["type"]}
        for s in registry.services[1:]
        if s["id"] != "unverified" and isinstance(s["id"], str)
    ]
    for verification in registry.verifications:
        if verification["service_id"] == "changed":
            verification["url"] = "https://other.example.org"
    entry = RegistryEntry(
        type="ga4gh-service-registry",
        url=REGISTRY,
        versions=["1.1"],
        environments=["prod"],
    )
    with caplog.at_level(logging.WARNING):
        urls = backends.get_cache(entry, "tes").urls()
    assert urls == ["https://ok.example.org"]
    assert "'changed': URL or API type differ" in caplog.text
    assert "'http'" in caplog.text
    assert "'private'" in caplog.text
    caplog.clear()
    backends.get_cache(entry, "tes")._next_read = 0
    with caplog.at_level(logging.WARNING):
        backends.get_cache(entry, "tes").urls()
    assert "not used" not in caplog.text  # logged once


def test_private_addresses_allowed(registry: Registry) -> None:
    """Registries may allow private addresses of their backends."""
    registry.services = [service("s", "https://internal.example.org")]
    entry = RegistryEntry(
        type="ga4gh-service-registry", url=REGISTRY, allow_private_addresses=True
    )
    assert backends.get_cache(entry, "tes").urls() == ["https://internal.example.org"]


def test_invalid_responses() -> None:
    """Responses of other shapes are errors."""
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    api = backends.API_TYPES["tes"]
    with pytest.raises(backends.RegistryError, match="Services"):
        select_services({}, [], entry, api)
    with pytest.raises(backends.RegistryError, match="Verifications are"):
        select_services([], {}, entry, api)
    with pytest.raises(backends.RegistryError, match="identifier"):
        select_services([], [{"id": 1}], entry, api)


def test_cache(registry: Registry, caplog: pytest.LogCaptureFixture) -> None:
    """Registries are read when due; the last good list is kept on errors."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    assert cache.urls() == ["https://tes-a.example.org"]
    assert cache.urls() == ["https://tes-a.example.org"]
    assert registry.reads == 1
    # failed reads keep the last good list, and are retried soon
    registry.error = HttpError("down")
    cache._next_read = 0
    with caplog.at_level(logging.WARNING):
        assert cache.urls() == ["https://tes-a.example.org"]
    assert "keeping 1 backends" in caplog.text
    assert cache._next_read - cache._last_read == backends.RETRY_INTERVAL
    # bugs are logged with the trace
    registry.error = RuntimeError("bug")
    cache._next_read = 0
    with caplog.at_level(logging.ERROR):
        assert cache.urls() == ["https://tes-a.example.org"]
    assert "Traceback" in caplog.text
    # requested reads are rate-limited
    registry.error = None
    cache._next_read = float("inf")
    cache.urls(refresh=True)
    assert registry.reads == 1
    cache._last_read -= backends.MIN_REFRESH_INTERVAL
    registry.services = []
    assert cache.urls(refresh=True) == []
    assert registry.reads == 2


def test_cache_busy(registry: Registry) -> None:
    """While another thread reads, the cached list is returned."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    cache.urls()
    cache._next_read = 0
    with cache._lock:
        assert cache.urls() == ["https://tes-a.example.org"]
    assert registry.reads == 1


def test_cache_read_meanwhile(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads done by another thread while waiting are not repeated."""
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    dues = iter([True, False])
    monkeypatch.setattr(cache, "_due", lambda refresh: next(dues))
    assert cache.urls() == []
    assert registry.reads == 0


def test_unresolved_hosts(registry: Registry) -> None:
    """Backends whose hosts are not resolved are kept for one more read."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    hosts: dict[str, Exception] = registry.hosts  # type: ignore[attr-defined]
    hosts["https://tes-a.example.org"] = HostNotResolved("unknown")
    assert cache.urls() == []
    del hosts["https://tes-a.example.org"]
    cache._next_read = 0
    assert cache.urls() == ["https://tes-a.example.org"]
    hosts["https://tes-a.example.org"] = HostNotResolved("unknown")
    for expected in (["https://tes-a.example.org"], []):
        cache._next_read = 0
        assert cache.urls() == expected


def test_find(registry: Registry) -> None:
    """Backends are found by URL, refreshing the registries if needed."""
    blist = make_list(
        [
            {"type": "ga4gh-tes", "url": "https://tes-c.example.org"},
            {"type": "ga4gh-service-registry", "url": REGISTRY},
        ]
    )
    assert blist.find("https://TES-C.example.org/").id == "https://tes-c.example.org"  # type: ignore[union-attr]
    assert blist.find("not a url") is None
    assert blist.find("https://tes-a.example.org", refresh=False) is None
    registry.services = [service("s1", "https://tes-a.example.org")]
    cache = backends.get_cache(blist.entries[1], "tes")  # type: ignore[arg-type]
    cache._last_read -= backends.MIN_REFRESH_INTERVAL
    found = blist.find("https://tes-a.example.org")
    assert found is not None
    assert found.source == "discovered"
    assert blist.find("https://tes-x.example.org") is None


def test_fork_resets_locks(registry: Registry) -> None:
    """Forked processes get new locks."""
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    lock = cache._lock
    discovery._reset_after_fork()
    assert cache._lock is not lock
    assert os.getpid()
