# Migrating to 0.21.0

FOCA 0.21.0 replaces the MongoDB client in Celery workers, so that it isn't
used across forks, and checks configurations, requests and responses more strictly.
It also adds request IDs, the `foca` command-line tool and an audit log. Apps need to
change if:

- their [workers keep objects taken from the client](#workers-replace-the-mongodb-client)
  or have [invalid access control settings](#workers-check-the-configuration);
- they rely on other [errors of creating indexes](#errors-of-creating-indexes) being
  `ValueError`s;
- they rely on [a client's `X-Request-ID`](#request-ids), as gateways could until now,
  or a proxy in front of the app sets that header;
- their [`log` section names filters or handlers that it doesn't
  configure](#unknown-filters-and-handlers-in-log-stop-the-app);
- their [requests or stored data don't fit declared formats](#stricter-request-checks);
- they use [`is_admin()` with a model of their own](#is_admin-with-other-models);
- they use the [plugins of foca-plugins](#plugins-of-foca-plugins);
- their filters or alerts match log texts that changed, of [changes of access
  rights](#audit-log) or [invalid responses](#log-of-invalid-responses).

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

### Unknown filters and handlers in `log` stop the app

A handler or logger in `log`, or its `root`, that names a filter or handler the section
doesn't configure now fails validation, so the app doesn't start. Until now, FOCA fell
back to its default logging with a warning. The error lists what's unknown, e.g.,
`root: console` for an app that sets `handlers` to its own handler `stream`, as the
default of `root` still names `console`. Other errors of logging settings, such as an
unknown formatter or a handler class that can't be loaded, still fall back with a
warning. Add the missing entry, or set `root.handlers` as well:

```yaml
log:
  handlers:
    stream:
      class: logging.StreamHandler
      formatter: standard
  root:
    handlers: [stream]
```

### Stricter request checks

[Requests that passed before can now fail with `400`](configuration/api.md#stricter-request-checks):
values that don't fit the formats `date-time`, `date` or `uri` where your
specification declares them, unpaired surrogates in JSON bodies, and, with
`strict_validation`, query parameters of operations without parameters. The same
format checks apply to responses with `validate_responses`, which then fail with
`500`, also for what you stored earlier, e.g., timestamps without a time zone from
`datetime.isoformat()`, or `uri` values such as a URL with surrounding spaces. One
such document fails the whole list that holds it, so that, e.g., a `GET` of a collection
answers `500` until the document is repaired. Check what your specifications declare,
what your clients send and what you have stored. Other formats are checked as before.

### `is_admin()` with other models

`is_admin()` of `foca.security.access_control` is now `False` for [models with global
and resource-scoped roles](access-control.md#resource-ownership) whose matcher doesn't
start with exactly the override of the reference model, `g(r.sub, "admin") ||`. An
override in parentheses or in the last place, and `r.sub == "admin"`, don't count.
Helpers such as `resource_filter()` then treat admins as anyone else, which limits them
to what they have roles on. If admins of your model should see everything, put the
override first in its matcher.

Admins also get further on resources without owners, where 0.20 answered `404`: if an
operation of `ownership_server` is passed an existence check as `exists`, admins can
list the roles of such a resource and assign its first owner (see [Adopting
ownership in an existing
app](access-control.md#adopting-ownership-in-an-existing-app)).

### Plugins of foca-plugins

The packages of foca-plugins cap the version of FOCA, and at the time of this release
none allows 0.21: foca-plugins 0.1.0 requires `foca<0.20`, and the 0.2.0 planned
next `foca<0.21`. Apps with these plugins (e.g., proTES) can't install FOCA 0.21 until
then, as dependency resolution or `uv pip check` fails. So the order is the FOCA tag,
then foca-plugins with `foca<0.22`, tested against 0.21, then the apps.

## Request IDs

Responses now have the header `X-Request-ID`, and [log
records](configuration/log.md#context-of-log-records) the identifier of their
request. Check this if your log format used a field of that name from `extra`, which
now wins over `extra`. The formatter `standard` stays the default, so log lines look
the same.

By default, the app generates the identifier and ignores what the request header says,
as [`server.request_id.trust_incoming`](configuration/server.md#request-ids) is
`false`. So [audit events](configuration/log.md#audit-log), too, have an identifier that
clients can't choose. A proxy in front of the app that sets the header doesn't have its
identifier in the app's logs, and sees the app's in the response. If you want the
proxy's, set the option to `true`, but only if the proxy sets or checks the header and
clients can't reach the app past it, as they could otherwise choose the identifier in
all of these. Not all proxies replace the header; ingress-nginx, for one, passes on a
client's value.

This changes the behaviour of gateways: the `request_id()` of `foca.plugins.adapter`
took a valid `X-Request-ID` of any client in 0.20, unconditionally, and now follows
[`server.request_id`](configuration/server.md#request-ids). By default, the field
`request_id` of [pipeline runs and their records](plugins.md#provenance) therefore has the
generated identifier, not what the client sent. Clients and dashboards that look up
these records by their own value need to take the identifier from the response header
instead, or a deployment that meets the conditions above sets the option to `true`.

## Command-line tool

FOCA now installs the console script [`foca`](cli.md). If your app installs a script of
that name, one of them replaces the other. Startup does the same as before, unless you
set the new switches `db.create_indexes_on_startup` and
`security.access_control.sync_policies_on_startup`.

## Audit log

Changes of access rights via the permissions API and the helpers for resource roles now
go to the logger `foca.audit`, with who made them (see [Audit
log](configuration/log.md#audit-log)). Configured rules don't: what the startup,
`foca sync-policies` or `policies_force` store, including grants such as
`[g, user:alice, admin]`, leaves no audit event, so review changes of these in your
configuration. The actor is personal data, so check where these logs go.

This isn't breaking, but log texts change: the permissions API no longer logs
`Policy rule '...' added.`, `updated.` or `deleted.` on its own logger. Adapt filters
and alerts that match them. Role changes via the operations on owners keep their log
lines and now also have audit events.

## Log of invalid responses

With `validate_responses`, the message of Connexion's validation logger on invalid
responses, which has the whole response body, is now filtered out. Instead, the logger
`foca.errors.exceptions` logs the operation and the validation error on level `ERROR`
(see [Invalid responses](configuration/exceptions.md#invalid-responses)). Adapt filters
and alerts that match Connexion's message.

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

foca-common has releases of its own, and 0.5.0 isn't out with FOCA 0.21.0 (the latest
is 0.4.0, tag `chart-v0.4.0`). Wait for it if you want what it adds; apps can stay on
0.4.0 with FOCA 0.21.

foca-common 0.5.0 has autoscaling and topology spread constraints of the app's
pods, `extraEnv`, `extraEnvFrom`, `rolloutOnSecrets`, and templates for [gateway
apps](helm-chart.md#gateway-apps) and [Flower](helm-chart.md#flower). Copy
`app-values.schema.json` again so that their values are checked.
Autoscaling also needs a template file with `foca-common.hpa`; without it,
enabling it leaves the app with one pod (see [Several replicas](helm-chart.md#several-replicas)).

Two things change without setting anything:

- The app's container gets `PORT` set to `8080`, the port of the Service and the probes,
  which rolls its pods once. Setting `PORT` in `extraEnv` fails when rendering, and in
  `extraEnvFrom`, the chart's value wins.
- Charts that add `extraEnv` in their own templates now render these variables twice,
  e.g., proTES with `protes.extraEnv`. Remove that when adopting 0.5.0. The library's
  checks then apply to these values, and rendering fails on them otherwise: remove
  variables that the chart sets, and move secrets in literal values to `valueFrom`.

## Building the FOCA image

`docker/Dockerfile` now pins its base images by digest and installs from hashed
requirements (see [Updating dependencies of the
image](contributing/index.md#updating-dependencies-of-the-image)), so the build
argument `PY_IMAGE` only takes `3.11` to `3.15` with `-slim-trixie`. Images built on
the published FOCA images aren't affected.
