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
/verifications`, an extension of the Cloud Registry, whose items name their
service by `serviceId`, or `service_id` before its version 1.0.0rc2) with
their current URL and API type, of an accepted version and environment, with
`https` URLs whose hosts have public addresses only (unless allowed). The
registry is the trust anchor, configured by the deployment: it is read
without access token, over `https` (unless `url_schemes` allows `http`),
at public addresses (unless `registry_allow_private_addresses`), without
following redirects (cf. :py:mod:`foca.utils.http`). Registries that require
login for reads therefore count as unreachable.

Each process caches the backends of each registry entry, refreshing them
every `refresh_interval` seconds; it keeps the last good list while the
registry is unreachable or answers invalidly (retrying after
`RETRY_INTERVAL` seconds at most), for `max_stale` seconds at most, and,
for one more read, backends whose hosts cannot be resolved for the time
being. Verifications that can be read apply in any case: backends that they
no longer list, with the same URL and API type, are not kept. Before the
first good read, and after `max_stale`, a registry yields no candidates.

Plain pydantic models and the guarded HTTP client; no web framework.
"""

from foca.plugins.backends.config import (
    API_TYPES,
    ApiType,
    BackendEntry,
    BackendListConfig,
    BasicCredentialsConfig,
    LocationConfig,
    RegistryEntry,
    ServiceEntry,
)
from foca.plugins.backends.lists import BackendList
from foca.plugins.backends.registry import (
    MIN_REFRESH_INTERVAL,
    RETRY_INTERVAL,
    RegistryCache,
    RegistryError,
    clear_caches,
    get_cache,
    select_services,
)

__all__ = [
    "API_TYPES",
    "MIN_REFRESH_INTERVAL",
    "RETRY_INTERVAL",
    "ApiType",
    "BackendEntry",
    "BackendList",
    "BackendListConfig",
    "BasicCredentialsConfig",
    "LocationConfig",
    "RegistryCache",
    "RegistryEntry",
    "RegistryError",
    "ServiceEntry",
    "clear_caches",
    "get_cache",
    "select_services",
]
