"""Tests of backend lists (`foca.plugins.backends`), without network."""

import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        options: Options of the last request.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self.services: Any = []
        self.verifications: Any = None
        self.error: Exception | None = None
        self.reads = 0
        self.options: dict[str, Any] = {}

    def fetch(self, url: str, timeout: float, **options: Any) -> Any:
        """Answer a request.

        Args:
            url: URL.
            timeout: Timeout.
            options: Options of `fetch_json`.

        Returns:
            Document.

        Raises:
            Exception: The configured error.
        """
        self.options = options
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


def test_discovered_ids_unique(
    registry: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    """Discovered backends whose URL is a configured entry's id are skipped."""
    registry.services = [
        service("s1", "https://tes-b.example.org"),
        service("s2", "https://tes-d.example.org"),
    ]
    blist = make_list(
        [
            {"type": "ga4gh-service-registry", "url": REGISTRY, "id": "reg"},
            {
                "type": "ga4gh-tes",
                "url": "https://tes-x.example.org",
                "id": "https://tes-b.example.org",
            },
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="foca.plugins.backends.lists"):
        candidates = blist.expand()
    assert [(b.id, b.url) for b in candidates] == [
        ("https://tes-d.example.org", "https://tes-d.example.org"),
        ("https://tes-b.example.org", "https://tes-x.example.org"),
    ]
    assert "id is taken" in caplog.text


def test_own_url_any_scheme() -> None:
    """Backends at the gateway's host and path are left out, whatever the scheme."""
    blist = make_list(
        [
            {"type": "ga4gh-tes", "url": url, "url_schemes": ["http", "https"]}
            for url in (
                "http://gw.example.org/tes",
                "http://gw.example.org/tes/sub",
                "https://gw.example.org:8443/tes",
                "https://gw.example.org/tesx",
                "https://gw.example.org",
            )
        ],
        public_url="https://gw.example.org/tes/",
    )
    assert [b.url for b in blist.expand()] == [
        "https://gw.example.org:8443/tes",
        "https://gw.example.org/tesx",
        "https://gw.example.org",
    ]
    assert not blist.has_registries()


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


def test_malformed_verifications(
    registry: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed verifications are skipped, so that revocations apply."""
    registry.services = [
        service("s1", "https://tes-a.example.org"),
        service("s2", "https://tes-b.example.org"),
    ]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    assert len(cache.urls()) == 2
    # s2 is revoked, and the registry lists a malformed item
    registry.verifications = [
        {"service_id": "s1", "url": "https://tes-a.example.org", "type": TES},
        {"id": 1},
        "x",
    ]
    cache._next_read = 0
    with caplog.at_level(logging.WARNING):
        assert cache.urls() == ["https://tes-a.example.org"]
    assert "Skipped 2 verifications" in caplog.text


def test_max_stale(registry: Registry, caplog: pytest.LogCaptureFixture) -> None:
    """Old lists are not used while the registry cannot be read."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    entry = RegistryEntry(
        type="ga4gh-service-registry", url=REGISTRY, refresh_interval=10, max_stale=20
    )
    cache = backends.get_cache(entry, "tes")
    assert cache.urls() == ["https://tes-a.example.org"]
    registry.error = HttpError("down")
    cache._next_read = 0
    assert cache.urls() == ["https://tes-a.example.org"]
    cache._read_good -= 21
    assert cache.urls() == []  # not due: too old anyway
    cache._next_read = 0
    with caplog.at_level(logging.WARNING):
        assert cache.urls() == []
    assert "older than max_stale" in caplog.text
    registry.error = None
    cache._next_read = 0
    assert cache.urls() == ["https://tes-a.example.org"]
    with pytest.raises(ValidationError, match="max_stale"):
        RegistryEntry(
            type="ga4gh-service-registry",
            url=REGISTRY,
            refresh_interval=60,
            max_stale=30,
        )


def test_registry_options(registry: Registry) -> None:
    """Registries are read with their own address and scheme options."""
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    backends.get_cache(entry, "tes").urls()
    assert registry.options == {"allow_private_addresses": False, "allow_http": False}
    entry = RegistryEntry(
        type="ga4gh-service-registry",
        url="http://registry.internal",
        url_schemes=["http"],
        registry_allow_private_addresses=True,
    )
    backends.get_cache(entry, "tes").urls()
    assert registry.options == {"allow_private_addresses": True, "allow_http": True}


class RegistryHandler(BaseHTTPRequestHandler):
    """Local registry with one verified TES."""

    def log_message(self, format: str, *args: Any) -> None:
        """Don't log.

        Args:
            format: Format.
            args: Arguments.
        """

    def do_GET(self) -> None:
        """Answer `GET /services` and `GET /verifications`."""
        tes = service("s1", "https://tes.example.org/ga4gh/tes/v1")
        if self.path.endswith("/verifications"):
            document: Any = [
                {"service_id": "s1", "url": tes["url"], "type": tes["type"]}
            ]
        else:
            document = [tes]
        body = json.dumps(document).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_local_registry() -> None:
    """Registries are read with the real client (local server, no DNS)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RegistryHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    backends.clear_caches()
    try:
        entry = RegistryEntry(
            type="ga4gh-service-registry",
            url=f"http://127.0.0.1:{httpd.server_address[1]}/ga4gh/registry/v1",
            url_schemes=["http"],
            registry_allow_private_addresses=True,
            allow_private_addresses=True,  # no DNS for the TES host
            timeout=5,
        )
        assert backends.get_cache(entry, "tes").urls() == ["https://tes.example.org"]
        private = entry.model_copy(update={"registry_allow_private_addresses": False})
        assert backends.get_cache(private, "tes").urls() == []
    finally:
        backends.clear_caches()
        httpd.shutdown()
        httpd.server_close()


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
    # no usable read: the thread waits for the lock, then finds the read done
    dues = iter([False])
    monkeypatch.setattr(cache, "_due", lambda refresh: next(dues))
    assert cache.urls() == []
    assert registry.reads == 0


def blocking_reads(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make reads of the registry wait for a release.

    Args:
        registry: Fake registry.
        monkeypatch: Pytest fixture.

    Returns:
        Events `started` (a read waits) and `release`.
    """
    started, release = threading.Event(), threading.Event()
    fetch = registry.fetch

    def slow(url: str, timeout: float, **options: Any) -> Any:
        started.set()
        assert release.wait(5)
        return fetch(url, timeout, **options)

    monkeypatch.setattr(discovery, "fetch_json", slow)
    return started, release


def in_thread(function: Any) -> tuple[threading.Thread, list[Any]]:
    """Call a function in a thread.

    Args:
        function: Function.

    Returns:
        Started thread, and a list that gets the result.
    """
    results: list[Any] = []
    thread = threading.Thread(target=lambda: results.append(function()))
    thread.start()
    return thread, results


@pytest.mark.parametrize("stale", [False, True])
def test_cache_waits_without_usable_read(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    """Without a usable read, threads wait for the one that reads."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    if stale:  # a good read, older than max_stale (e.g., after an idle gap)
        assert cache.urls() == ["https://tes-a.example.org"]
        cache._read_good -= entry.max_stale + 1
        cache._next_read = 0
    started, release = blocking_reads(registry, monkeypatch)
    reader, first = in_thread(cache.urls)
    assert started.wait(5)
    waiter, second = in_thread(cache.urls)
    waiter.join(0.2)
    assert waiter.is_alive()  # waits for the read
    release.set()
    reader.join(5)
    waiter.join(5)
    assert first == second == [["https://tes-a.example.org"]]


def test_cache_serves_during_refresh(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a usable read, threads get it while another thread reads."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    assert cache.urls() == ["https://tes-a.example.org"]
    cache._next_read = 0
    started, release = blocking_reads(registry, monkeypatch)
    reader, _ = in_thread(cache.urls)
    assert started.wait(5)
    assert cache.urls() == ["https://tes-a.example.org"]  # at once
    release.set()
    reader.join(5)


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
    caches_lock = discovery._caches_lock
    discovery._reset_after_fork()
    assert cache._lock is not lock
    assert discovery._caches_lock is not caches_lock
