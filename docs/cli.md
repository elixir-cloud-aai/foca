# Command-line tool

The console script `foca` runs admin tasks once, rather than at the startup of each
worker of each replica, and checks a configuration or exports a specification without
serving the app. It's installed with FOCA and builds the configuration as your app
does, including `FOCA_CONFIG_OVERLAY`.

```shell
foca --app my_app.app:foca check-config
foca --app my_app.app:foca create-indexes
foca --app my_app.app:foca sync-policies
foca --app my_app.app:foca export-spec --format json > openapi.json
```

`--app` is `module:attribute`, naming a `Foca` instance or a function returning one,
which is called without arguments. The environment variable `FOCA_APP` works as well;
`--app` wins. Modules are also searched for in the current directory.

```python
# my_app/app.py
from foca import Foca

foca = Foca(config_file="config.yaml")

if __name__ == "__main__":
    foca.create_app().run()
```

The module must not create the app when it's imported, or what the app does on startup
happens before the command runs, as far as the [switches](#startup-switches) allow.

`foca --help` and, e.g., `foca export-spec --help` tell the options.

| Exit code | Meaning |
|-----------|---------|
| 0 | Success |
| 1 | Errors of the configuration or the task, e.g., a validation error, or the database not being available; written to the standard error, without traceback |
| 2 | Errors of usage, e.g., an unknown option or an `--app` that can't be found |

## Commands

### `check-config`

Loads and validates the configuration and runs the startup checks of the app and, if
`jobs` is set, of its workers. It runs `Foca.create_app()` without serving the app and
without writing to the database, so it's fit as a CI or Helm test step. Checks that your
app's functions run on startup (e.g., [fields of the service info computed at
runtime](configuration/service-info.md)) run too, and what they need must be available.
The startup steps that the [switches](#startup-switches) cover aren't run, so this
doesn't find conflicting indexes or stored documents.

### `create-indexes`

Creates the indexes of `db.dbs`, as on startup, and sets up the store of gateway
plugins, whose database errors are logged rather than failing. It fails if no databases
are configured.

### `sync-policies`

Replaces the stored configured policy rules with those of
`security.access_control.policies`, as on startup, or removes them if it isn't set. It
honors `policies_revision` and `policies_force`. It fails if access control isn't
enabled.

### `export-spec`

Writes a merged API specification to the standard output, as the app serves it, with
what FOCA and your configuration add. `--format` is `yaml` (default) or `json`. If the
app has several specifications, `--spec` selects one, by its name in `api.specs` or its
base path. The permissions API isn't exported.

Logs go where the [log configuration](configuration/log.md) sends them, by default to
the standard error. If yours writes to the standard output, it mixes with the
specification.

## Startup switches

With these, replicas leave the tasks to the commands. Both default to `true`.

```yaml
db:
  create_indexes_on_startup: false
security:
  access_control:
    sync_policies_on_startup: false
```

- **`db.create_indexes_on_startup`:** with `false`, `Foca.create_app()` and
  `Foca.create_celery_app()` don't create the indexes of `db.dbs`, and apps with gateway
  plugins don't set up the store on startup, so lowered `max_ttl`s apply to entries
  written before only once `create-indexes` runs. Writes to the store still create its
  indexes.
- **`security.access_control.sync_policies_on_startup`:** with `false`, stored
  configured rules stay as they are until `sync-policies` runs, which also keeps
  replicas of different versions in a rolling upgrade from replacing each other's rules.
  The check for stored rules that group settings keep from applying moves there, too.

Run the commands before you start the new version, e.g., in a Helm `pre-upgrade` hook or
an init container of a single replica, as without them, the indexes that queries rely on
and the configured rules won't be there.
