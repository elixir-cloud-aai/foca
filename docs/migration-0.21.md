# Migrating to 0.21.0

FOCA 0.21.0 replaces the MongoDB client in Celery workers, so that it isn't
used across forks. Apps need to change if their workers keep objects taken
from the client.
Upgrading from 0.19 or earlier? Start with [Migrating to
0.20.0](migration-0.20.md).

## Breaking changes

### Workers replace the MongoDB client

A worker of an app with a database now replaces its MongoDB client just before
it starts its pool, and each pool process replaces it again when it starts,
closing the previous one (see [MongoDB in
workers](configuration/jobs.md#mongodb-in-workers)). Until now, pool processes
used the client of the main process, and PyMongo warned `MongoClient opened
before fork`.

Clients, databases and collections kept from earlier (e.g., in a module-level
variable, or in a handler of `worker_process_init` that runs before FOCA's) now
raise `pymongo.errors.InvalidOperation: Cannot use MongoClient after close`.
Get them when you need them instead, e.g., in the task:

```python
from celery import shared_task
from foca import get_collection


@shared_task
def store(document: dict[str, str]) -> None:
    """Store a document."""
    get_collection(db="myDb", collection="myCollection").insert_one(document)
```

The same holds for workers started in the process of an app (e.g., with
`celery.contrib.testing.worker.start_worker` in tests): what the app keeps from
its client, including FOCA's access control, can't be used afterwards. Create
the app after the worker has started, or run the worker in a process of its own.

### Errors of creating indexes

Index conflicts, and stored documents that violate a unique index, raise
`foca.database.register_mongodb.IndexConflictError`, a subclass of `ValueError`, with
the error of PyMongo as its cause, so code catching `ValueError` still works. Other
`OperationFailure`s when creating indexes (e.g., missing permissions) now stay what they
are, instead of a `ValueError` claiming a conflict.

### Workers check the configuration

`Foca.create_celery_app()` now stops on invalid access control settings, as
`Foca.create_app()` does ([startup checks](configuration/jobs.md#startup-checks)). So a
worker with such settings doesn't start anymore.

### Stricter request checks

[Requests that passed before can now fail with `400`](configuration/api.md#stricter-request-checks):
values that don't fit the formats `date-time`, `date` or `uri` where your
specification declares them, unpaired surrogates in JSON bodies, and, with
`strict_validation`, query parameters of operations without parameters. The same
format checks apply to responses with `validate_responses`, which then fail with
`500`, also for what you stored earlier, e.g., timestamps without a time zone from
`datetime.isoformat()`. Check what your specifications declare and what your clients
send. Other formats are checked as before.

### References to environment variables in configuration files

String values in configuration files now [reference environment
variables](configuration/index.md#references-to-environment-variables). What you wrote as
`${...}` now means that: an unset variable stops the app, and invalid forms do too, naming
the key. `$$` now means a literal `$`. Write `$$` for each `$` that's meant literally in
these two cases, e.g., `$${...}`. Other uses of `$`, such as at the end of a regular
expression, stay as they are. API specifications aren't affected.

### Secrets in the configuration

`jobs.backend` is now a `SecretStr`, as are the `client_options`
`authMechanismProperties` and `tlsCertificateKeyFilePassword`, so that they don't show up
in `repr()`. Code reading them must call `.get_secret_value()`. Don't read them outside
of what needs them.

## Request IDs

Responses now have the header `X-Request-ID`, and [log
records](configuration/log.md#context-of-log-records) the identifier of their
request. Check this if a proxy in front of the app sets the header itself, or if your
log format used a field of that name from `extra`, which now wins over `extra`. The
formatter `standard` stays the default, so log lines look the same.

The `request_id()` of `foca.plugins.adapter` follows
[`server.request_id`](configuration/server.md#request-ids), rather than always
reading `X-Request-ID`.

## Command-line tool

FOCA now installs the console script [`foca`](cli.md). If your app installs a script of
that name, one of them replaces the other. Startup does the same as before, unless you
set the new switches `db.create_indexes_on_startup` and
`security.access_control.sync_policies_on_startup`.
## Audit log

Changes of access rights now go to the logger `foca.audit`, with who made them (see
[Audit log](configuration/log.md#audit-log)). This isn't breaking, but log texts
change: the permissions API no longer logs `Policy rule '...' added.`, `updated.` or
`deleted.` on its own logger. Adapt filters and alerts that match them. Role changes
via the operations on owners keep their log lines and now also have audit events. The
actor is personal data, so check where these logs go.

## Changed defaults

### Shorter MongoDB timeouts

`serverSelectionTimeoutMS` and `connectTimeoutMS` of MongoDB clients are now 5
seconds, down from PyMongo's 30 and 20 seconds, so that with MongoDB down,
requests and the startup of the app fail sooner, rather than blocking their
threads. If MongoDB takes longer to become available or to answer in your
deployment, e.g., during failovers of a replica set, set them in
[`db.client_options`](configuration/db.md#client-options) or `MONGO_URI`. Clients
also report a name to MongoDB now (`appname`), which appears in its logs.

## App charts

foca-common 0.5.0 has autoscaling and topology spread constraints of the app's
pods. Copy `app-values.schema.json` again so that their values are checked.
Autoscaling also needs a template file with `foca-common.hpa`; without it,
enabling it leaves the app with one pod (see [Several replicas](helm-chart.md#several-replicas)). Nothing changes
otherwise.

## Building the FOCA image

`docker/Dockerfile` now pins its base images by digest and installs from hashed
requirements (see [Updating dependencies of the
image](contributing/index.md#updating-dependencies-of-the-image)), so the build
argument `PY_IMAGE` only takes `3.11` to `3.15` with `-slim-trixie`. Images built on
the published FOCA images aren't affected.
