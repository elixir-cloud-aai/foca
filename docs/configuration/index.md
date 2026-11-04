# Configuration

FOCA apps are configured with a [YAML](https://yaml.org/) file. The following
top-level sections are interpreted by FOCA; all of them are optional:

| Section | Purpose |
| --- | --- |
| [`server`](server.md) | Host, port, debug and testing mode |
| [`api`](api.md) | OpenAPI specifications |
| [`db`](db.md) | MongoDB databases, collections and indexes |
| [`exceptions`](exceptions.md) | Problem details for errors |
| [`jobs`](jobs.md) | Background tasks with Celery |
| [`log`](log.md) | Logging |
| [`service_info`](service-info.md) | GA4GH service info |
| [`security.auth`](auth.md) | Bearer token validation |
| [`security.access_control`](access-control.md) | Access control |
| [`security.cors`](cors.md) | Cross-origin resource sharing |
| [`plugins`](plugins.md) | Gateway plugins: allowlist and pipelines |
| [`custom`](custom.md) | Validated app-specific settings |

Values of these sections are **validated** when the app is created; invalid
values and unknown keys raise a `pydantic.ValidationError`. Other top-level
sections are allowed and passed through as they are.

Start from the [annotated
template](https://github.com/elixir-cloud-aai/foca/blob/dev/templates/config.yaml),
or check the configuration of the [petstore
example](https://github.com/elixir-cloud-aai/foca/blob/dev/examples/petstore/config.yaml).

## Relative paths

Relative file paths in the configuration (`api.specs[].path`,
`api.specs[].patches`, `security.access_control.api_specs` and
`security.access_control.model`) are resolved against the directory of the
configuration file (the first one, if several are used, also for paths set
in later files), so that an app finds its files irrespective of the
directory it is started from. Without a configuration file (e.g., if the
configuration is created in code), they are resolved against the current
working directory. Use `Foca.config_dir` to resolve paths in your custom
configuration the same way.

## Environment-specific configuration

Settings that differ between environments (e.g., development and production)
can be kept in separate files that are merged into a base configuration, in
order: mappings are merged recursively, while lists and other values
(including `null`) replace those of earlier files.

```python
from foca import Foca

foca = Foca(config_file=["config.yaml", "config.dev.yaml"])
```

Files listed in the environment variable `FOCA_CONFIG_OVERLAY` (separated by
`:` on Linux and macOS, `;` on Windows; relative to the current working
directory) are merged afterwards, e.g., to select an overlay at deployment
time, such as from a Kubernetes ConfigMap:

```bash
FOCA_CONFIG_OVERLAY=deployment/config.prod.yaml python app.py
```

```yaml title="deployment/config.prod.yaml"
server:
  port: 80
log:
  root:
    level: 30
```

The merged configuration is validated once, and the merged files are logged.
If your app needs to know its environment, define a value in your
[custom configuration](custom.md) and set it in the overlays.

### Named entries

As lists are replaced, an overlay that changes one API specification or
policy rule would have to restate all of them, and would silently drop
entries that a new version of the app adds. Therefore, `api.specs` and
`security.access_control.policies` can also be given as mappings of names to
entries. Later files then add, change or remove single entries by name:
entries are merged like other mappings, and `null` removes an entry.

```yaml title="config.yaml (app)"
api:
  specs:
    my-api:
      path: specs/openapi.yaml
security:
  access_control:
    policies:
      create-items: [p, "*", /api/items, POST]
      admins-delete: [p, admin, "/api/items/:id", DELETE]
```

```yaml title="overlay.yaml (deployment)"
api:
  specs:
    my-api:
      patches:
        - /etc/my-app/openapi.patch.yaml
security:
  access_control:
    policies:
      admins-delete: null
      read-items: [p, "*", "/api/items/:id", GET]
```

To keep mistakes from going unnoticed, FOCA refuses to start if a file

- gives named entries for a section that earlier files set to a non-empty
  list (the entries of the list would be lost; give the complete list
  instead, which replaces the named entries, too),
- removes (`null`) entries that are not set, e.g., because of a misspelled
  name, or
- changes an API specification that is not set (an entry of `api.specs`
  without `path`).

To remove an entry that an overlay added, remove it from the overlay rather
than setting it to `null` in another one: `null` only removes entries that
earlier files set. Policy rules are removed from the database on the next
startup, also if no rules remain, i.e., if `policies` is no longer set (see
[configuring policies](../access-control.md#configuring-policies)).

Named entries also compose with other tools that merge mappings but replace
lists, e.g., Helm when merging several values files (except for removals, see
[the Helm chart](../helm-chart.md#values)). See [patching
specifications](api.md#patching-specifications) and [configuring
policies](../access-control.md#configuring-policies) for what to do with
them.

See [Deployment](../deployment.md) for how to split the configuration between
the app and its deployments, e.g., with Helm on Kubernetes.

## References to environment variables

String values in configuration files can reference environment variables, e.g., to
keep secrets out of the files:

```yaml
custom:
  storage:
    access_key: ${STORAGE_ACCESS_KEY}
    region: ${STORAGE_REGION:-eu-north-1}
    secret_key: ${STORAGE_SECRET_KEY:?needed for the storage}
    price: $$5
```

| Syntax | Result |
| --- | --- |
| `${VAR}` | Value of `VAR`, which may be empty; an error if it isn't set |
| `${VAR:-default}` | Value of `VAR`, or `default` if it's unset or empty |
| `${VAR:?message}` | Value of `VAR`; an error with `message` if it's unset or empty |
| `$$` | A literal `$` |

This follows the [specification of
Compose](https://github.com/compose-spec/compose-spec/blob/main/spec.md#interpolation),
for these forms; nesting them isn't supported, and neither is `$VAR` without braces,
which stays as it is. Errors name the key. References are replaced per file, after
parsing the YAML and before merging, so all files must resolve, also values that a later
file overrides. Keys aren't changed, and neither are API specifications and other files
that the configuration points to. Values stay strings, also where a variable holds a
number. Most settings convert them, but those that say they're strict refuse them.

Keep secrets in fields that hide them, i.e., of type
[`SecretStr`](https://docs.pydantic.dev/latest/api/types/#pydantic.types.SecretStr) in
[your own models](custom.md#secrets). Those of FOCA are `jobs.backend`, the
`client_options` `authMechanismProperties` and `tlsCertificateKeyFilePassword` of
[`db`](db.md), and `plugins.defaults.store.key_secret`.

## Environment variables

`MONGO_URI`, `MONGO_USERNAME`, `MONGO_PASSWORD`, `CELERY_BROKER_URL` and
`CELERY_RESULT_BACKEND` can come from files, e.g., mounted secrets, as with Docker's
official images: `MONGO_PASSWORD_FILE=/run/secrets/mongo-password` reads the password
from that file, without its final line break. Setting both a variable and its `_FILE`
variable is an error, as is a file that can't be read.

| Variable | Description |
| --- | --- |
| `FOCA_CONFIG_OVERLAY` | Config files merged into the configuration ([above](#environment-specific-configuration)). |
| `MONGO_URI`, `MONGO_HOST`, `MONGO_PORT`, `MONGO_USERNAME`, `MONGO_PASSWORD`, `MONGO_DBNAME` | MongoDB connection ([`db`](db.md#environment-variables)). |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | Celery broker and result backend ([`jobs`](jobs.md)). |
| `FOCA_PLUGINS_STORE_KEY_SECRET` | Key secret of the plugin store, if `plugins.defaults.store.key_secret` is not set or empty ([`plugins`](plugins.md#plugin-store)). |

## Accessing the configuration

Once the app is created, use `foca.get_config()` to access the validated and
typed configuration, e.g., in controllers, security handlers or Celery tasks,
and `foca.get_collection()` for registered MongoDB collections:

```python
from foca import get_collection, get_config

config = get_config()
server = config.server
custom = config.custom  # if a `custom` section is configured

collection = get_collection(db="myDb", collection="myCollection")
```

Inside an application context, `get_config()` returns the configuration of
that app; otherwise (e.g., in Celery workers or scripts), that of the most
recently created FOCA app. It raises a `RuntimeError` if no FOCA app has been
created. Outside of apps, the configuration is also available as
`Foca(...).conf`, and inside apps also as `current_app.config.foca`.

::: foca.models.config.Config
    options:
      show_root_heading: true
      members: false
