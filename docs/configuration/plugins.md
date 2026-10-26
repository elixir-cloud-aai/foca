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
  defaults:
    timeout: 10            # seconds per alternative and call
    pipeline_timeout: 30   # seconds for the request and route phases
    forward_timeout: 60    # seconds for all forwarding attempts
    workers: 8             # threads running hooks, per process
    http:                  # ctx.http of plugins
      allow_private_addresses: false
      allow_http: false
      max_response_size: 16777216  # bytes
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
`client_selectable` or `config`.

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

::: foca.plugins.config.StepConfig
    options:
      members: false

::: foca.plugins.config.AlternativeConfig
    options:
      members: false
