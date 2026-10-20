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
`security.access_control.api_specs` and `security.access_control.model`) are
resolved against the directory of the configuration file (the first one, if
several are used), so that an app finds its files irrespective of the
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

See [Deployment](../deployment.md) for how to split the configuration between
the app and its deployments, e.g., with Helm on Kubernetes.

## Environment variables

| Variable | Description |
| --- | --- |
| `FOCA_CONFIG_OVERLAY` | Config files merged into the configuration ([above](#environment-specific-configuration)). |
| `MONGO_URI`, `MONGO_HOST`, `MONGO_PORT`, `MONGO_USERNAME`, `MONGO_PASSWORD`, `MONGO_DBNAME` | MongoDB connection ([`db`](db.md#environment-variables)). |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | Celery broker and result backend ([`jobs`](jobs.md)). |

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
