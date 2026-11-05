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

The attribute must lead to a `Foca` instance. A function must return that instance, not
the Connexion app that `create_app()` returns, as in
[the example of the deployment guide](deployment.md). Apps whose only factory returns
the Connexion app, and that keep no `Foca` instance in a module, can't be targeted: for
them, moving these tasks out of startup needs a change in the app, such as a module-level
`Foca` instance or a function returning one. `foca` can only tell what a function returns
by calling it, so with such a factory, what the function does itself (e.g., writing to
the database) runs before `foca` exits with code 2. `Foca` instances created while the
module loads, the function runs or the command runs (e.g., in a worker module that a
controller imports) don't write to the database on startup. What the module
writes to the standard output goes to the standard error, so that the output of the
commands stays clean.

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
without writing to the database, so it's fit as a CI or Helm test step. It does create
the app, as the checks need it: specifications are checked by registering them with the
app, and the checks of access control and the service info work on it. Checks that your
app's functions run on startup (e.g., [fields of the service info computed at
runtime](configuration/service-info.md)) run too, and what they need must be available.
Configured policy rules are checked. The startup steps that the
[switches](#startup-switches) cover aren't run, so this doesn't find conflicting indexes
or stored documents, nor stored rules of a newer revision. One exception: in a gateway
app without a `db` section, no switch covers the store of plugins, so the command sets
it up as on startup, which writes to its backend, and only logs database errors.

### `create-indexes`

Creates the indexes of `db.dbs`, as on startup, and sets up the store of gateway
plugins. Unlike on startup, database errors of the store fail the command. It fails if no
databases are configured. The policy collection has no configured indexes: its adapter
creates what it needs on its first write.

### `sync-policies`

Replaces the stored configured policy rules with those of
`security.access_control.policies`, as on startup, or removes them if it isn't set. It
honors `policies_revision` and `policies_force`: if the stored rules stay, as their
revision is newer, it fails. It also fails if access control isn't enabled.

### `export-spec`

Writes a merged API specification to the standard output, as the app serves it, with
what FOCA and your configuration add. So `servers` of OpenAPI 3 has the base path of
the API, without the hosts of the file. `--format` is `yaml` (default) or `json`. If the
app has several specifications, `--spec` selects one, by its name in `api.specs` or its
base path. The permissions API isn't exported.


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
  indexes, and warnings of its settings stay.
- **`security.access_control.sync_policies_on_startup`:** with `false`, stored
  configured rules stay as they are until `sync-policies` runs, which also keeps
  replicas of different versions in a rolling upgrade from replacing each other's rules.
  Configured rules are still checked on startup, but the check for stored rules that
  group settings keep from applying moves to the command.

Run the commands before you start the new version, e.g., in a Helm `pre-upgrade` hook or
an init container of a single replica, as without them, the indexes that queries rely on
and the configured rules won't be there.
