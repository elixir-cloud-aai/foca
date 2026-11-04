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

## App charts

foca-common 0.5.0 has autoscaling and topology spread constraints of the app's
pods. Copy `app-values.schema.json` again so that their values are checked.
Autoscaling also needs a template file with `foca-common.hpa`; without it,
enabling it leaves the app with one pod (see [Several replicas](helm-chart.md#several-replicas)). Nothing changes
otherwise.
