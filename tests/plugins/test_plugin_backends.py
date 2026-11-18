"""Tests of backend lists (`foca.plugins.backends`), without network."""

import json
import logging
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError, field_validator

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
        services_error: Error to raise for reads of the services only, if any.
        reads: Number of reads of the services.
        options: Options of the last request.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self.services: Any = []
        self.verifications: Any = None
        self.error: Exception | None = None
        self.services_error: Exception | None = None
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
                {"serviceId": s["id"], "url": s["url"], "type": s["type"]}
                for s in self.services
                if isinstance(s, dict)
            ]
        if self.services_error is not None:
            raise self.services_error
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
        [{"type": "ga4gh-drs", "url": "https://a.example.org"}],
    ):
        with pytest.raises(ValidationError):
            BackendListConfig.model_validate(invalid)


def test_unknown_api() -> None:
    """Only APIs with a backend type are supported."""
    with pytest.raises(ValueError, match="No backend type"):
        BackendList([], api="drs")


@pytest.mark.parametrize(
    ("api", "entry_type", "other_type"),
    [("tes", "ga4gh-tes", "ga4gh-wes"), ("wes", "ga4gh-wes", "ga4gh-tes")],
)
def test_entry_types(api: str, entry_type: str, other_type: str) -> None:
    """Lists take the entries of their own API only, and registries."""
    entries = [
        {"type": entry_type, "url": "https://a.example.org"},
        {"type": "ga4gh-service-registry", "url": REGISTRY},
    ]
    assert [b.url for b in BackendList(entries, api=api).expand()] == [
        "https://a.example.org"
    ]
    other = {"type": other_type, "url": "https://b.example.org", "id": "b"}
    with pytest.raises(ValueError, match=f"'b'.*{other_type!r}.*{entry_type!r}"):
        BackendList([*entries, other], api=api)


def test_wes_discovery(registry: Registry) -> None:
    """Registry entries of WES lists yield the verified WES instances."""
    wes = {"group": "org.ga4gh", "artifact": "wes", "version": "1.1.0"}
    registry.services = [
        service("t1", "https://tes-a.example.org"),
        service("w1", "https://wes-a.example.org/ga4gh/wes/v1", type=wes),
    ]
    entries = [{"type": "ga4gh-service-registry", "url": REGISTRY}]
    assert [b.url for b in BackendList(entries, api="wes").expand()] == [
        "https://wes-a.example.org"
    ]


@pytest.mark.parametrize(
    ("entry_type", "given", "expected"),
    [
        ("ga4gh-tes", None, "/ga4gh/tes/v1"),
        ("ga4gh-wes", None, "/ga4gh/wes/v1"),
        ("ga4gh-wes", "/", ""),
        ("ga4gh-wes", "", ""),
        ("ga4gh-wes", "/api/wes/v1/", "/api/wes/v1"),
        ("ga4gh-tes", "/a-b/c_d/e.f~g", "/a-b/c_d/e.f~g"),
    ],
)
def test_api_path(entry_type: str, given: str | None, expected: str) -> None:
    """API paths default to the API's and lose trailing slashes."""
    data: dict[str, Any] = {"type": entry_type, "url": "https://a.example.org"}
    if given is not None:
        data["api_path"] = given
    assert ServiceEntry.model_validate(data).api_path == expected


@pytest.mark.parametrize(
    "api_path",
    ["ga4gh/tes/v1", "/a/../b", "/a/./b", "/a//b", "/a?b", "/a#b", "/a b", "//"],
)
def test_invalid_api_path(api_path: str) -> None:
    """API paths are absolute, of unreserved characters, without dot segments."""
    with pytest.raises(ValidationError, match="API path"):
        ServiceEntry(type="ga4gh-tes", url="https://a.example.org", api_path=api_path)


def test_api_path_of_subclasses() -> None:
    """Apps may override the API path field, e.g., with a default of their own."""

    class AppEntry(ServiceEntry):
        """Entry with an API path default of the app's."""

        api_path: str = "/custom/"

    assert AppEntry(type="ga4gh-tes", url="https://a.example.org").api_path == (
        "/custom"
    )
    entry = AppEntry(type="ga4gh-tes", url="https://a.example.org", api_path="/x/")
    assert entry.api_path == "/x"
    with pytest.raises(ValidationError, match="API path"):
        AppEntry(type="ga4gh-tes", url="https://a.example.org", api_path="x")

    class InvalidDefault(ServiceEntry):
        """Entry with an invalid API path default."""

        api_path: str = "/a/../b"

    with pytest.raises(ValidationError, match="API path"):
        InvalidDefault(type="ga4gh-tes", url="https://a.example.org")


def test_api_path_without_api_type() -> None:
    """Entries of types without an API type need an API path of their own."""

    class AppEntry(ServiceEntry):
        """Entry of a type without an API type."""

        type: Literal["ga4gh-drs"]  # type: ignore[assignment]

    with pytest.raises(ValidationError, match="No API type for backend type"):
        AppEntry(type="ga4gh-drs", url="https://a.example.org")
    entry = AppEntry(type="ga4gh-drs", url="https://a.example.org", api_path="/")
    assert entry.api_path == ""


def test_api_path_of_candidates() -> None:
    """Apps get the API path of configured backends from their entries."""
    backend_list = BackendList(
        [
            {
                "type": "ga4gh-wes",
                "url": "https://wes.example.org",
                "api_path": "/api/wes/v1/",
            }
        ],
        api="wes",
    )
    entry = backend_list.entry_for(backend_list.expand()[0])
    assert isinstance(entry, ServiceEntry)
    assert entry.api_path == "/api/wes/v1"


def test_api_path_with_validators_of_subclasses() -> None:
    """Validators of apps' own API path fields get the path as given."""
    seen: list[str] = []

    class AppEntry(ServiceEntry):
        """Entry with an API path validator of the app's (as in proTES)."""

        api_path: str = "/ga4gh/tes/v1"

        @field_validator("api_path")
        @classmethod
        def check_api_path(cls, value: str) -> str:
            """Refuse relative paths, and remove trailing slashes.

            Args:
                value: API path.

            Returns:
                The API path.

            Raises:
                ValueError: The path is not absolute.
            """
            seen.append(value)
            if not value.startswith("/"):
                raise ValueError("API path must be absolute")
            return value.removesuffix("/")

    entry = AppEntry(type="ga4gh-tes", url="https://a.example.org", api_path="/")
    assert entry.api_path == ""
    assert seen == ["/"]


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


def test_own_url_ports() -> None:
    """Default ports of both schemes count as the gateway's, others don't."""
    blist = make_list(
        [
            {"type": "ga4gh-tes", "url": url, "url_schemes": ["http", "https"]}
            for url in (
                "http://gw.example.org:443/tes",
                "http://gw.example.org:8080/tes",
                "https://gw.example.org:80/tes",
            )
        ],
        public_url="https://gw.example.org/tes",
    )
    assert [b.url for b in blist.expand()] == [
        "http://gw.example.org:8080/tes",
        "https://gw.example.org:80/tes",
    ]


def test_verified_version(registry: Registry) -> None:
    """Services whose verified API version differs are not used."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    registry.verifications = [
        {
            "serviceId": "s1",
            "url": "https://tes-a.example.org",
            "type": {**TES, "version": "1.0.0"},
        }
    ]
    blist = make_list([{"type": "ga4gh-service-registry", "url": REGISTRY}])
    assert blist.expand() == []


def test_slow_read_times(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """Next reads and staleness count from the end of a read."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    ends: list[float] = []
    fetch = registry.fetch

    def slow(url: str, timeout: float, **options: Any) -> Any:
        time.sleep(0.02)
        ends.append(time.monotonic())
        return fetch(url, timeout, **options)

    monkeypatch.setattr(discovery, "fetch_json", slow)
    assert cache.urls() == ["https://tes-a.example.org"]
    assert cache._next_read >= ends[-1] + entry.refresh_interval
    assert cache._read_good >= ends[-1]
    registry.error = HttpError("down")
    cache._next_read = 0
    cache.urls()
    assert cache._next_read >= ends[-1] + backends.RETRY_INTERVAL


def test_cache_waits_if_expired_meanwhile(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threads wait for a running read if the cache expires meanwhile."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    assert cache.urls() == ["https://tes-a.example.org"]
    cache._next_read = 0
    states = iter([True, False])
    monkeypatch.setattr(cache, "_usable", lambda: next(states))
    cache._lock.acquire()  # another thread reads
    threading.Timer(0.1, cache._lock.release).start()
    assert cache.urls() == ["https://tes-a.example.org"]
    assert registry.reads == 2


def test_expand_snapshots(registry: Registry) -> None:
    """Expanding records the reads it used."""
    registry.services = [service("s1", "https://tes-a.example.org")]
    blist = make_list(
        [
            {"type": "ga4gh-service-registry", "url": REGISTRY, "id": "reg"},
            {"type": "ga4gh-tes", "url": "https://tes-b.example.org"},
        ]
    )
    snapshots: dict[str, str | None] = {}
    assert len(blist.expand(snapshots=snapshots)) == 2
    assert snapshots == {"reg": blist.snapshots()["reg"]}
    assert snapshots["reg"] is not None


def test_finite_timeouts() -> None:
    """Registry timeouts are finite."""
    with pytest.raises(ValidationError):
        RegistryEntry(type="ga4gh-service-registry", url=REGISTRY, timeout=float("inf"))


def test_ordered_entries() -> None:
    """Unordered entries are refused."""
    with pytest.raises(TypeError, match="ordered"):
        BackendList(frozenset(), api="tes")  # type: ignore[arg-type]


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
        {"serviceId": s["id"], "url": s["url"], "type": s["type"]}
        for s in registry.services[1:]
        if s["id"] != "unverified" and isinstance(s["id"], str)
    ]
    for verification in registry.verifications:
        if verification["serviceId"] == "changed":
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
        {"serviceId": "s1", "url": "https://tes-a.example.org", "type": TES},
        {"id": 1},
        "x",
    ]
    cache._next_read = 0
    with caplog.at_level(logging.WARNING):
        assert cache.urls() == ["https://tes-a.example.org"]
    assert "Skipped 2 verifications" in caplog.text


@pytest.mark.parametrize(
    "identifier",
    [
        {"serviceId": "s1"},  # Cloud Registry 1.0.0rc2 and later
        {"service_id": "s1"},  # before
        {"serviceId": "s1", "service_id": "other"},
    ],
)
def test_verification_identifiers(
    registry: Registry, identifier: dict[str, str]
) -> None:
    """Verifications name their service by `serviceId` or `service_id`."""
    tes = service("s1", "https://tes-a.example.org")
    registry.services = [tes]
    registry.verifications = [
        {
            **identifier,
            "url": tes["url"],
            "type": tes["type"],
            "verifiedAt": "2026-10-20T12:34:56.789Z",
        }
    ]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    assert backends.get_cache(entry, "tes").urls() == ["https://tes-a.example.org"]


@pytest.mark.parametrize(
    "identifier",
    [
        {"serviceId": 1},
        {"serviceId": ""},
        {"serviceId": "", "service_id": "s1"},
        {"serviceId": None, "service_id": "s1"},
        {"serviceId": 1, "service_id": "s1"},
        {"service_id": 1},
    ],
)
def test_invalid_verification_identifiers(
    registry: Registry, identifier: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """`service_id` doesn't stand in for an invalid `serviceId`."""
    tes = service("s1", "https://tes-a.example.org")
    registry.services = [tes]
    registry.verifications = [{**identifier, "url": tes["url"], "type": tes["type"]}]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    with caplog.at_level(logging.WARNING):
        assert backends.get_cache(entry, "tes").urls() == []
    assert "Skipped 1 verifications" in caplog.text


def test_api_type_spelling(registry: Registry) -> None:
    """API types match as in Cloud Registry: ignoring case and outer spaces."""
    api_type = {"group": " ORG.ga4gh", "artifact": "TES ", "version": "1.1.0"}
    registry.services = [
        {"id": "s1", "url": "https://tes-a.example.org", "type": api_type}
    ]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    assert backends.get_cache(entry, "tes").urls() == ["https://tes-a.example.org"]


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


def verification(service_id: str, url: str | None, **kwargs: Any) -> dict[str, Any]:
    """Build a verification, by default of the API type `TES`.

    Args:
        service_id: Identifier of the service.
        url: URL of the service.
        kwargs: Other fields.

    Returns:
        Verification.
    """
    return {"serviceId": service_id, "url": url, "type": TES, **kwargs}


@pytest.fixture
def two_backends(registry: Registry) -> Any:
    """Create a cache that has read two verified backends.

    Args:
        registry: Fake registry.

    Returns:
        Cache.
    """
    registry.services = [
        service("s1", "https://tes-a.example.org/ga4gh/tes/v1"),
        service("s2", "https://tes-b.example.org/ga4gh/tes/v1"),
    ]
    entry = RegistryEntry(
        type="ga4gh-service-registry", url=REGISTRY, refresh_interval=10, max_stale=20
    )
    cache = backends.get_cache(entry, "tes")
    assert cache.urls() == ["https://tes-a.example.org", "https://tes-b.example.org"]
    return cache


@pytest.mark.parametrize(
    "error",
    [HttpError("down"), {}, RuntimeError("bug")],
    ids=["unreachable", "invalid", "bug"],
)
def test_revocation_without_services(
    registry: Registry,
    two_backends: Any,
    error: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Revocations apply when the verifications are read and the services not."""
    cache = two_backends
    read_at, read_good = cache.read_at, cache._read_good
    if isinstance(error, Exception):
        registry.services_error = error
    else:
        registry.services = error
    registry.verifications = [
        verification("s1", "https://tes-a.example.org/ga4gh/tes/v1")
    ]
    cache._next_read = 0
    with caplog.at_level(logging.WARNING):
        assert cache.urls() == ["https://tes-a.example.org"]
    assert "not used any more: https://tes-b.example.org\n" in caplog.text
    assert "keeping 1 backends" in caplog.text
    # it isn't a good read: its time and the limit of the staleness stay
    assert cache.read_at == read_at
    assert cache._read_good == read_good
    assert cache._next_read <= time.monotonic() + backends.RETRY_INTERVAL
    cache._read_good -= 21
    assert cache.urls() == []


@pytest.mark.parametrize(
    "item",
    [
        verification("s2", "https://tes-b.example.org/ga4gh/tes/v1"),
        # another URL, or none
        verification("s1", "https://tes-a.example.org/other"),
        verification("s1", "https://tes-x.example.org/ga4gh/tes/v1"),
        verification("s1", "http://tes-a.example.org/ga4gh/tes/v1"),
        verification("s1", "not a url"),
        verification("s1", None),
        # another API, a version that isn't accepted, or no API type
        verification(
            "s1",
            "https://tes-a.example.org/ga4gh/tes/v1",
            type={"group": "org.ga4gh", "artifact": "wes", "version": "1.1.0"},
        ),
        verification(
            "s1",
            "https://tes-a.example.org/ga4gh/tes/v1",
            type={"group": "org.ga4gh", "artifact": "tes", "version": "2.0.0"},
        ),
        verification("s1", "https://tes-a.example.org/ga4gh/tes/v1", type=None),
    ],
)
def test_changed_verification_without_services(
    registry: Registry, two_backends: Any, item: dict[str, Any]
) -> None:
    """Backends must still be verified with their URL and API type."""
    cache = two_backends
    registry.services_error = HttpError("down")
    registry.verifications = [item]
    cache._next_read = 0
    expected = ["https://tes-b.example.org"] if item["serviceId"] == "s2" else []
    assert cache.urls() == expected


def test_verification_spelling_without_services(
    registry: Registry, two_backends: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Backends with unchanged verifications stay, in the order of the read."""
    cache = two_backends
    registry.services_error = HttpError("down")
    registry.verifications = [
        verification("s2", "https://TES-B.example.org:443/ga4gh/tes/v1/"),
        verification(
            "s1",
            "https://tes-a.example.org/ga4gh/tes/v1",
            # updating a service to another accepted version keeps its verification
            type={"group": " ORG.ga4gh", "artifact": "TES ", "version": "1.1.1"},
        ),
    ]
    cache._next_read = 0
    with caplog.at_level(logging.WARNING):
        assert cache.urls() == [
            "https://tes-a.example.org",
            "https://tes-b.example.org",
        ]
    assert "no longer verified" not in caplog.text


@pytest.mark.parametrize("document", [{}, None, "x"])
def test_invalid_verifications_keep_backends(
    registry: Registry,
    two_backends: Any,
    monkeypatch: pytest.MonkeyPatch,
    document: Any,
) -> None:
    """Verifications that can't be read revoke nothing, with services or without."""
    cache = two_backends
    urls = cache.urls()
    read_at = cache.read_at
    fetch = registry.fetch

    def invalid(url: str, timeout: float, **options: Any) -> Any:
        if url.endswith("/verifications"):
            return document
        return fetch(url, timeout, **options)

    monkeypatch.setattr(discovery, "fetch_json", invalid)
    cache._next_read = 0
    assert cache.urls() == urls
    assert cache.read_at == read_at
    assert registry.reads == 1  # the services aren't read either


def test_malformed_verifications_without_services(
    registry: Registry, two_backends: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed items are skipped, and logged once, as in a good read."""
    cache = two_backends
    registry.services_error = HttpError("down")
    registry.verifications = [
        verification("s1", "https://tes-a.example.org/ga4gh/tes/v1"),
        {"id": 1},
    ]
    cache._next_read = 0
    with caplog.at_level(logging.WARNING):
        assert cache.urls() == ["https://tes-a.example.org"]
    assert caplog.text.count("Skipped 1 verifications") == 1
    # and once per read, if the services can be read too
    caplog.clear()
    registry.services_error = None
    cache._next_read = 0
    with caplog.at_level(logging.WARNING):
        assert cache.urls() == ["https://tes-a.example.org"]
    assert caplog.text.count("Skipped 1 verifications") == 1


def test_same_url_without_services(registry: Registry) -> None:
    """Of services with the same URL, the verification of the listed one decides."""
    url = "https://tes-a.example.org/ga4gh/tes/v1"
    registry.services = [service("s1", url), service("s2", url)]
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    cache = backends.get_cache(entry, "tes")
    assert cache.urls() == ["https://tes-a.example.org"]
    registry.services_error = HttpError("down")
    registry.verifications = [verification("s2", url)]
    cache._next_read = 0
    assert cache.urls() == []
    registry.services_error = None
    cache._next_read = 0
    assert cache.urls() == ["https://tes-a.example.org"]


def test_verified_again(registry: Registry, two_backends: Any) -> None:
    """Revoked backends come back with the next good read, not before."""
    cache = two_backends
    verifications = [
        verification("s1", "https://tes-a.example.org/ga4gh/tes/v1"),
        verification("s2", "https://tes-b.example.org/ga4gh/tes/v1"),
    ]
    registry.services_error = HttpError("down")
    registry.verifications = verifications[:1]
    cache._next_read = 0
    assert cache.urls() == ["https://tes-a.example.org"]
    registry.verifications = verifications
    cache._next_read = 0
    assert cache.urls() == ["https://tes-a.example.org"]
    registry.services_error = None
    cache._next_read = 0
    assert cache.urls() == ["https://tes-a.example.org", "https://tes-b.example.org"]


def test_revocation_during_read(
    registry: Registry, two_backends: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other threads don't get revoked backends while the services are read."""
    cache = two_backends
    registry.verifications = [
        verification("s1", "https://tes-a.example.org/ga4gh/tes/v1")
    ]
    during: list[list[str]] = []
    fetch = registry.fetch

    def recording(url: str, timeout: float, **options: Any) -> Any:
        if url.endswith("/services"):
            during.append(cache._current()[0])
        return fetch(url, timeout, **options)

    monkeypatch.setattr(discovery, "fetch_json", recording)
    cache._next_read = 0
    assert cache.urls() == ["https://tes-a.example.org"]
    assert during == [["https://tes-a.example.org"]]


def test_registry_options(registry: Registry) -> None:
    """Registries are read with their own address and scheme options."""
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    backends.get_cache(entry, "tes").urls()
    assert registry.options == {
        "allow_private_addresses": False,
        "allow_http": False,
        "ca_bundle": None,
    }
    entry = RegistryEntry(
        type="ga4gh-service-registry",
        url="http://registry.internal",
        url_schemes=["http"],
        registry_allow_private_addresses=True,
        registry_ca_bundle="/etc/ssl/private-ca.pem",
    )
    backends.get_cache(entry, "tes").urls()
    assert registry.options == {
        "allow_private_addresses": True,
        "allow_http": True,
        "ca_bundle": "/etc/ssl/private-ca.pem",
    }


def test_registry_ca_bundle_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative CA bundles of registries are kept as absolute paths."""
    monkeypatch.chdir(tmp_path)
    entry = RegistryEntry(
        type="ga4gh-service-registry", url=REGISTRY, registry_ca_bundle="ca.pem"
    )
    assert entry.registry_ca_bundle == str(tmp_path / "ca.pem")
    entry = RegistryEntry(type="ga4gh-service-registry", url=REGISTRY)
    assert entry.registry_ca_bundle is None


@pytest.mark.parametrize("ca_bundle", [False, True, 0, "", b"/etc/ssl/certs", []])
def test_registry_ca_bundle_never_disables_verification(ca_bundle: Any) -> None:
    """CA bundles of registries that are no paths, or empty, are refused."""
    with pytest.raises(ValidationError):
        RegistryEntry(
            type="ga4gh-service-registry", url=REGISTRY, registry_ca_bundle=ca_bundle
        )


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
                {
                    "serviceId": "s1",
                    "url": tes["url"],
                    "type": tes["type"],
                    "verifiedAt": "2026-10-20T12:34:56.789Z",
                }
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


def test_local_registry_revocation() -> None:
    """A revocation applies when the real client gets an error for the services."""

    class Handler(RegistryHandler):
        """Registry that has revoked its verification and fails for the services."""

        def do_GET(self) -> None:
            """Answer `GET /verifications` with no items, else with an error."""
            status = 200 if self.path.endswith("/verifications") else 503
            body = b"[]"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
        cache = backends.get_cache(entry, "tes")
        assert cache.urls() == ["https://tes.example.org"]
        read_at = cache.read_at
        # requests from now on are answered by the other handler
        httpd.RequestHandlerClass = Handler
        cache._next_read = 0
        assert cache.urls() == []
        assert cache.read_at == read_at
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
    start = time.monotonic()
    with caplog.at_level(logging.WARNING):
        assert cache.urls() == ["https://tes-a.example.org"]
    end = time.monotonic()
    assert "keeping 1 backends" in caplog.text
    # the retry counts from the end of the read, which lies between both clock reads
    assert start + backends.RETRY_INTERVAL <= cache._next_read
    assert cache._next_read <= end + backends.RETRY_INTERVAL
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


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "ga4gh-tes", "url": "https://t", "basic": {"password": "hunter2"}},
        {"type": "ga4gh-tes", "url": "https://u:hunter2@t.example.org"},
        {"type": "other", "url": "https://u:hunter2@t.example.org"},
    ],
)
def test_errors_leave_out_values(entry: dict[str, Any]) -> None:
    """Validation errors of backend lists don't show what entries hold."""
    with pytest.raises(ValidationError) as info:
        BackendListConfig.model_validate([entry])
    assert "hunter2" not in str(info.value)
