# `plugins`

Gateway apps (e.g., proTES) process requests with installed plugins (cf.
[Gateway plugins](../plugins.md)). This section lists which plugins are
loaded, with their admin settings, and the default pipeline that requests
go through.

```yaml
plugins:
  # Allowlist: only these entry points (group `foca.plugins`) are loaded,
  # even if more are installed
  available:
    org.example.tes.policy.images:
      distribution: example-tes-policies    # optional pin
      version: ">=1.0,<2"                   # optional PEP 440 specifier
      config:                               # the plugin's admin settings
        registries: [registry.example.org]
    org.example.tes.data.decrypt:
      enabled: false                        # kill switch: loaded nowhere
      client_selectable: true               # clients may choose it (default)
      roles: []                             # roles that may choose it; any if empty
      store: {enabled: false}               # its ctx.store in memory, per process
  defaults:
    timeout: 10            # seconds per alternative and call
    pipeline_timeout: 30   # seconds for the request and route phases
    forward_timeout: 60    # seconds for all forwarding attempts
    workers: 8             # threads running hooks, per process
    http:                  # ctx.http of plugins
      allow_private_addresses: false
      allow_http: false
      max_response_size: 16777216  # bytes
    store:                 # ctx.store of plugins; per plugin: available.<id>.store
      enabled: true        # in the app's database, if any; else memory per process
      max_entry_size: 4096 # bytes per value, as compact JSON
      max_ttl: 2592000     # seconds (30 days); also the default TTL
      # key_secret: ...    # HMAC secret for ctx.store.key_for(); don't commit it,
      #                    # use FOCA_PLUGINS_STORE_KEY_SECRET (see Plugin store)
  # Default pipeline: steps, each with ordered alternatives (fallbacks);
  # default: [foca.routing.ordered]
  pipeline:
    - org.example.tes.policy.images        # short form, see below
    - id: decrypt
      alternatives:
        - plugin: org.example.tes.data.decrypt
          params: {volume: /vol/crypt}
      timeout: 5
      on_failure: skip     # fail (default) | skip
    - foca.routing.ordered                 # built-in route step
  # Named pipelines that clients may choose (tag `gateway.pipeline`)
  presets:
    encrypted-inputs:
      - org.example.tes.data.decrypt
  # Pipelines chosen by clients per request (disabled by default)
  client_pipelines:
    enabled: true            # default: false
    allowed: [presets, inline]   # at least one; `enabled: false` allows none
    max_steps: 8
    max_alternatives: 3
    max_size: 8192           # bytes of the tag's value
    roles: []                # who may choose; any authenticated caller if empty
    prepend:                 # admin frame: first in client and preset
      - org.example.tes.policy.images  # pipelines only, not in `pipeline`:
                                       # policies belong in both
    append:                  # always last (or only if a phase is missing)
      - plugin: foca.routing.ordered   # the default
        if_missing_phase: route
        on_failure: skip
  # Backends chosen by clients per request (tag `gateway.backends`)
  client_backends:
    enabled: true
    max_items: 32
  tags:
    prefix: "gateway."       # reserved tag prefix
    max_route_entries: 10    # attempts listed in the route tag
  provenance:
    record_failed: false     # store records of failed runs (audit)
    secret_headers: []       # left out of change hashes, besides credentials
  gateway_id: protes.example.org          # default: host[:port] of server.public_url
```

## Allowlist

FOCA's built-in plugins (e.g., the default route step
`foca.routing.ordered`, cf. [default routing](../plugins.md#default-routing))
are always available; list them only to change `enabled`, `roles`,
`client_selectable`, `config` or `store`.

Plugins are Python classes registered under an entry point of the group
`foca.plugins` by an installed distribution. Only entry points listed in
`available` are imported, so the code of other installed plugins never
runs. On startup, the app refuses to start if a listed plugin is not
installed, is provided by several distributions, does not match its pins
(`distribution`, `version`), does not support the app's API, or its admin
settings (`config`) are invalid.

`enabled: false` keeps an entry, but loads nothing: the plugin's
alternatives are dropped from all pipelines. A step left without
alternatives is dropped if it has `on_failure: skip`; otherwise, the app
refuses to start. Plugins cannot be enabled or disabled at runtime: change
the configuration and restart (e.g., with a rolling update).

## Pipelines

A pipeline is a list of **steps**; each step has one or more
**alternatives** (a plugin with `params`), tried in order until one
succeeds. The short form `- <plugin id>` or `- plugin: <id>` (with optional
`params`) is a step with one alternative, whose `id` is the plugin's
identifier; steps with the same plugin need explicit, unique `id`s.

- `timeout` limits each call of an alternative (default:
  `defaults.timeout`).
- `on_failure` says what happens when all alternatives failed: `fail`
  (the request fails with `500`) or `skip` (the step is left out).
- The keys `when` and `needs` are reserved for conditions and dependencies
  of later versions; they and all unknown keys are errors.

## Per-request choices

!!! warning "Experimental: per-request pipelines and backends"

    The client-facing tags `gateway.pipeline` and `gateway.backends`, and
    the settings `client_pipelines` and `client_backends` that control
    them, are experimental: their shape and semantics may change in a later
    release, with the design of federated backend selection (cf.
    [#528](https://github.com/elixir-cloud-aai/foca/issues/528)). Client
    pipelines are disabled by default.

!!! warning "Policies that must always run belong in `pipeline` and `prepend`"

    A client's pipeline (or a preset) **replaces** the default pipeline
    (`pipeline`) entirely, and an empty client pipeline (`[]`) is allowed.
    The frame (`client_pipelines.prepend` and `append`) runs only around
    client pipelines and presets, not around `pipeline`, which requests
    without a choice get. So, with `client_pipelines` enabled, list policy
    steps that must not be skipped in **both** `pipeline` and `prepend`.
    On startup, the app warns about steps of `pipeline` that the frame
    lacks: steps without the same alternatives (plugins and `params`),
    `on_failure` and `timeout` in `prepend`, or in `append` without `if_missing_phase`
    (which clients can drop by sending a step of that phase), unless
    clients may choose presets only and every preset has the step; only
    the default route step without `params` is exempt.

Authenticated clients choose presets or their own pipelines
(`client_pipelines`) and backends (`client_backends`) in reserved tags (cf.
[per-request pipelines and
backends](../plugins.md#per-request-pipelines-and-backends)). Client
pipelines are disabled by default, so that no client can skip steps of
`pipeline` unless an admin allows it; backend choices are enabled by
default, as they can only narrow or reorder the candidates. Both are
limited to the allowlist (the keys `client_selectable` and `roles` of the
entries in `available`) and to the candidates of each request. Presets are
written by admins, so they may use plugins that are not
`client_selectable`, but only callers with the plugins' `roles` may choose
them. Presets and frames are checked on startup like the default pipeline
(also that no preset step has the identifier of a frame step); presets that
need a disabled plugin are unavailable.

The backends themselves are not part of `plugins`: gateway apps configure
them as a [backend list](../plugins.md#backend-lists) under a key of their
own (e.g., proTES: `custom.tes.backends`).

## Provenance

Records describe each plugin's changes as hashes before and after and the
changed JSON Pointers, without values. Credentials (`Authorization`,
`Cookie`, `Proxy-Authorization`), the headers in
`provenance.secret_headers` (e.g., API keys) and the credential type of
outbound requests are left out of the hashes, as hashes of short secrets
can be guessed offline. `provenance.record_failed` stores records of failed
runs (e.g., rejected requests) for audits, in a collection of the app with
a TTL index (`foca.plugins.provenance.FAILURE_INDEXES`: 30 days; set the
retention you need); it is off by default, so that no record of rejected
input is kept (cf. [provenance](../plugins.md#provenance)).

## Plugin store

Plugins keep small data across requests in `ctx.store` (cf. [plugin
store](../plugins.md#plugin-store)), e.g., caches and per-user counters.
With a database configured (`db.dbs`), the entries are in the collection
`plugin_store` of the app's **first** database, shared by all processes and
replicas. The policy database that access control adds when the app has no
database of that name (e.g., `access_control_db` without `db` section) is
not the app's: without an app database, the store stays in memory. Gateway
apps create the collection's indexes at startup (a TTL index on
`expires_at`, and one on `ns`), so the database user needs the right to
create indexes. If that fails because the database is down or slow (more
than 10 s), the app logs a warning and starts anyway, and writes to the
store create the indexes first, failing until they can. Errors that last
until the operator acts (a conflicting index, failed authentication or a
missing right: MongoDB codes 85, 86, 18 and 13) are logged as errors, with
their cause; writes to the store fail until it is fixed. Startup then also
cuts the expiry of entries to the current `max_ttl` (see below), within
another 10 s for all namespaces; failures are logged per namespace, and
the app starts anyway. Entries are always read from the primary of a
replica set, also if the app's connection prefers secondaries.

Without database, each process keeps its entries in memory: they are lost
on restart, counters (e.g., budgets) hold per process (per Gunicorn worker
and replica), and memory use is **unbounded**: it grows with the entries
that plugins write within `max_ttl` (there is no size limit per
namespace), plus expired entries until they are removed (when read, and
all of them every 1024 writes). The same holds for plugins whose store is
turned off. Without database, configure a short `max_ttl` (e.g., an hour)
and a single worker per replica; apps log a warning once at startup if
plugins that declare stored data (`data_stored`) keep it in memory.

- `defaults.store.enabled: false`, or `available.<id>.store.enabled:
  false` for one plugin, keeps the entries in the memory of each process
  even with a database, e.g., when a plugin's data must not be stored
  (counters and caches then hold per process only). Set it for all
  plugins of a distribution: otherwise, they don't see each other's
  entries (the app logs a warning).
- `max_entry_size` limits each value (bytes of compact JSON); larger
  values fail the plugin call.
- `max_ttl` limits how long entries live (default: 30 days, at most 10
  years); entries without TTL get it, longer TTLs are cut to it. MongoDB
  removes expired entries about once a minute; until then, they are no
  longer read. A lowered `max_ttl` applies to existing entries when the app
  starts: each process cuts the expiry of the entries of a namespace to at
  most `max_ttl` from then (the longest `max_ttl` of the namespace's
  plugins that use the database). Entries then live at most `max_ttl`
  after the last start with the new setting, provided the cut succeeded
  (the app logs it otherwise): during a rolling update, processes with the
  old setting still write entries with the old `max_ttl`, so restart the
  app once more after all processes have the new one.
- Turning the store off for plugins keeps their entries in the database
  until they expire (they are no longer read). To remove them earlier,
  delete the namespace's entries, e.g., in `mongosh`:
  `db.plugin_store.deleteMany({ns: "<namespace>"})`, with the namespace of
  the plugins' distribution (cf. [plugin store](../plugins.md#plugin-store));
  the same removes the entries of a distribution that is no longer
  installed.
- Plugins of one distribution share a namespace; plugins of other
  distributions can't read it. Entries keyed by users are personal data:
  their retention is at most `max_ttl`.
- `key_secret` turns the keys that plugins derive with
  `ctx.store.key_for()` (e.g., from user identifiers) into HMAC-SHA256
  keys, with the prefix `hk:`. Without it, the keys are plain SHA-256
  hashes (prefix `h:`): anyone with access to the database or a backup who
  knows an identifier (e.g., an LS Login `sub`, which is in tokens, logs
  and other services) can hash it again and find that user's entries, so
  set the secret whenever the store is in the database. FOCA logs a
  warning at startup if plugins keep their entries in the database without
  a secret. It applies to all plugins (not per plugin).
    - **Never commit it.** Don't write it in the configuration file; set the
      environment variable `FOCA_PLUGINS_STORE_KEY_SECRET` (used if
      `key_secret` is not set and the variable is not empty), e.g., from a
      Kubernetes Secret with `valueFrom.secretKeyRef`, or merge a file that
      only holds the setting and is mounted from a secret (cf.
      [environment-specific configuration](index.md#environment-specific-configuration)).
      A number (e.g., unquoted in YAML) is taken as a string. The secret
      isn't shown in logs, error messages, `repr()` or the plugin listing;
      use a long random value (e.g., `openssl rand -base64 32`).
    - **All processes and replicas must have the same secret**, or they
      don't find each other's entries (budgets and caches then reset
      for the callers they would apply to).
    - **Changing, adding or removing the secret** changes all derived keys:
      existing entries are no longer found (e.g., per-user budgets start
      anew) and expire after at most `max_ttl`; delete them earlier as
      described above if needed. During a rolling update, processes with the
      old and the new secret don't see each other's entries either. Rotate
      the secret in quiet times, and restart all replicas after changing it.

## Migrating from middlewares

Apps that had gateway middlewares (e.g., proTES's `custom.middlewares`)
refuse to start with that key, naming the new sections: list the plugins
in `available` and the steps in `pipeline`.

::: foca.plugins.config.PluginsConfig
    options:
      members: false

::: foca.plugins.config.ClientPipelinesConfig
    options:
      members: false

::: foca.plugins.config.ClientBackendsConfig
    options:
      members: false

::: foca.plugins.config.TagsConfig
    options:
      members: false

::: foca.plugins.config.AvailablePluginConfig
    options:
      members: false

::: foca.plugins.config.PluginDefaultsConfig
    options:
      members: false

::: foca.plugins.config.StoreConfig
    options:
      members: false

::: foca.plugins.config.StoreOverrideConfig
    options:
      members: false

::: foca.plugins.config.StepConfig
    options:
      members: false

::: foca.plugins.config.AlternativeConfig
    options:
      members: false
