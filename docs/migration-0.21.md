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

## Request IDs

Responses now have the header `X-Request-ID`, and [log
records](configuration/log.md#context-of-log-records) the identifier of their
request. Check this if a proxy in front of the app sets the header itself, or if your
log format used a field of that name from `extra`, which now wins over `extra`. The
formatter `standard` stays the default, so log lines look the same.

The `request_id()` of `foca.plugins.adapter` follows
[`server.request_id`](configuration/server.md#request-ids), rather than always
reading `X-Request-ID`.

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
