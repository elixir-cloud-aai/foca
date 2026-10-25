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
  defaults:
    timeout: 10            # seconds per alternative and call
    pipeline_timeout: 30   # seconds for the request and route phases
    forward_timeout: 60    # seconds for all forwarding attempts
    workers: 8             # threads running hooks, per process
  # Default pipeline: steps, each with ordered alternatives (fallbacks)
  pipeline:
    - org.example.tes.policy.images        # short form, see below
    - id: decrypt
      alternatives:
        - plugin: org.example.tes.data.decrypt
          params: {volume: /vol/crypt}
      timeout: 5
      on_failure: skip     # fail (default) | skip
  gateway_id: protes.example.org          # default: host of server.public_url
```

## Allowlist

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

## Migrating from middlewares

Apps that had gateway middlewares (e.g., proTES's `custom.middlewares`)
refuse to start with that key, naming the new sections: list the plugins
in `available` and the steps in `pipeline`.

::: foca.plugins.config.PluginsConfig
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
