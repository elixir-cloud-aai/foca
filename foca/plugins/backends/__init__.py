"""Typed backend lists of gateway apps, with service registry discovery.

Gateway apps configure the backends that requests may be forwarded to as
one ordered list of typed entries, under a key of their own (e.g., proTES:
`custom.tes.backends`). Backends of TES gateways have `type: ga4gh-tes`,
those of WES gateways `type: ga4gh-wes` (`entry_type` of the API's type in
`API_TYPES`); a list takes the backends of its app's API only:

```yaml
backends:
  - type: ga4gh-tes
    url: https://tes-a.example.org
    auth: client_credentials       # client_credentials | basic | none
    labels: {region: eu-north}
    location: {latitude: 60.17, longitude: 24.94}
    api_path: /ga4gh/tes/v1        # default: the API's; `/` if at `url`
  - type: ga4gh-service-registry
    id: cloud-registry
    url: https://registry.example.org/ga4gh/registry/v1
    versions: ["1.1"]
  - type: ga4gh-tes
    url: https://tes-fallback.example.org
    auth: basic
    basic: {username_env: TES_FALLBACK_USER, password_env: TES_FALLBACK_PASSWORD}
client_credentials:                # the gateway's client, next to the list
  issuer: https://login.example.org/
  audience_parameter: resource     # optional: a token per backend
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
at public addresses (unless `registry_allow_private_addresses`), with its
TLS certificate verified with the public certificate authorities (unless
`registry_ca_bundle` names others; never unverified), without
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

Gateways authenticate at their backends with credentials of their own, as
named by `auth` (cf. :py:mod:`foca.plugins.backends.credentials`): access
tokens of the gateway's client (:py:class:`ClientCredentialsConfig`,
:py:class:`TokenProvider`), HTTP Basic credentials from the environment, or
none; :py:func:`get_credentials` resolves them per request, and
:py:func:`check_backends` checks them on startup.

Plain pydantic models, `requests` and the guarded HTTP client; no web
framework.
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
from foca.plugins.backends.credentials import (
    NO_CREDENTIALS,
    BackendSession,
    ClientCredentialsConfig,
    Credentials,
    CredentialsError,
    TokenKey,
    TokenProvider,
    check_backends,
    check_client_credentials,
    get_credentials,
    get_env,
    token_audience,
    uses_tokens,
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
    "NO_CREDENTIALS",
    "RETRY_INTERVAL",
    "ApiType",
    "BackendEntry",
    "BackendList",
    "BackendListConfig",
    "BackendSession",
    "BasicCredentialsConfig",
    "ClientCredentialsConfig",
    "Credentials",
    "CredentialsError",
    "LocationConfig",
    "RegistryCache",
    "RegistryEntry",
    "RegistryError",
    "ServiceEntry",
    "TokenKey",
    "TokenProvider",
    "check_backends",
    "check_client_credentials",
    "clear_caches",
    "get_cache",
    "get_credentials",
    "get_env",
    "select_services",
    "token_audience",
    "uses_tokens",
]
