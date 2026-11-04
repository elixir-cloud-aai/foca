# `jobs`

Background tasks with [Celery](https://docs.celeryq.dev/), using
[RabbitMQ](https://www.rabbitmq.com/) as message broker.

```yaml
jobs:
  host: rabbitmq
  port: 5672
  backend: rpc://
  include:
    - my_app.tasks.my_task_1
    - my_app.tasks.my_task_2
```

This config attaches the `rabbitmq` broker host running on port `5672` and
registers the tasks found in modules `my_task_1` and `my_task_2`.

To pass credentials or use TLS, set the broker URL via environment variable
`CELERY_BROKER_URL` (e.g., `amqps://user:password@rabbitmq:5671//`), which
takes precedence over `jobs.host` and `jobs.port`. Likewise,
`CELERY_RESULT_BACKEND` takes precedence over `jobs.backend`. Both are logged
without credentials. On Kubernetes, the [Helm library chart](../helm-chart.md)
can deploy a worker and a RabbitMQ broker, and sets both variables from a
Secret.

Create the Celery app in your worker entry point with
`Foca.create_celery_app()`; tasks run in the app context, so they can use
[`get_config()`](index.md#accessing-the-configuration):

```python
from foca import Foca

celery_app = Foca(config_file="my_app/config.yaml").create_celery_app()
```

## MongoDB in workers

PyMongo clients [must not be used across
forks](https://dochub.mongodb.org/core/pymongo-fork-deadlock), and a worker
forks its pool processes after `Foca.create_celery_app()` has created the
client and the configured indexes. So with Celery's default `prefork` pool, FOCA
replaces the client just before the worker starts its pool, and again in each
pool process when it starts. The new clients connect on first use.

Get collections when you need them, e.g., with
[`get_collection()`](index.md#accessing-the-configuration) in a task. Clients,
databases and collections kept from earlier (e.g., in a module-level variable,
or in a handler of `worker_process_init` that runs before FOCA's) belong to a
closed client, and using them raises `pymongo.errors.InvalidOperation`. This
also holds for what an app keeps from its client if a worker starts in its
process (e.g., `celery.contrib.testing.worker.start_worker` in tests); see
[Migrating to 0.21.0](../migration-0.21.md).

## Worker shutdown

Workers get a `SIGTERM` when they are stopped, e.g., on every rollout of a
Deployment. Without further settings, this can lose tasks, so decide what
your tasks need before you run them in production. FOCA changes none of
Celery's defaults.

On `SIGTERM`, a worker shuts down warmly: it stops taking new tasks, waits for
the running tasks to finish, and then exits. Messages it has fetched but not
acknowledged go back to the queue. If the running tasks take longer than the
platform allows (on Kubernetes, `terminationGracePeriodSeconds`), the worker
gets a `SIGKILL`.

By default, Celery acknowledges a task just before running it, so a task
that is killed is lost: the broker won't deliver it again. These settings
change that:

| Setting | Default | Effect |
|---|---|---|
| `task_acks_late` | `False` | Acknowledge after the task ran, so that the broker delivers it again if the worker's connection is lost before the acknowledgment |
| `task_reject_on_worker_lost` | `False` | With `task_acks_late`, put the task back on the queue if the process running it dies (by default, it is acknowledged and marked as failed) |
| `worker_prefetch_multiplier` | `4` | Messages each worker process fetches ahead; `1` suits long tasks, as fewer tasks then wait behind a running one |

`jobs` has no field for these settings. Set them on the app that
`Foca.create_celery_app()` returns, in your worker entry point:

```python
from foca import Foca

celery_app = Foca(config_file="my_app/config.yaml").create_celery_app()
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
)
```

With these settings, tasks run **at least once** instead of at most once, so:

- **Tasks must be idempotent.** A task that was killed halfway runs again from
  the start, possibly after it had already done part of its work. Tasks that
  aren't safe to repeat (e.g., that charge for or submit something) should
  stay with the defaults, or record their progress and check it first.
- **A task that kills its worker runs again and again.** With
  `task_reject_on_worker_lost`, a task that runs out of memory, for example,
  goes back to the queue each time. Bound this, e.g., with a [delivery
  limit](https://www.rabbitmq.com/docs/quorum-queues#poison-message-handling),
  which needs a quorum queue; Celery declares classic queues by default.
- **Long tasks need a longer broker timeout.** RabbitMQ closes the channel of
  a consumer that hasn't acknowledged a message for 30 minutes by default
  ([`consumer_timeout`](https://www.rabbitmq.com/docs/consumers#acknowledgement-timeout)),
  which delivers the task again. Raise it above your longest task.

Set the grace period above the time your longest task takes, or accept that
such tasks are killed on rollouts. With the [Helm library
chart](../helm-chart.md), it is `worker.terminationGracePeriodSeconds`
(default: 30 seconds). A worker must get the signal itself: start it directly
(e.g., `command: [celery, --app, my_app.worker:celery_app, worker]`), not through a
shell that doesn't pass signals on.

::: foca.models.config.JobsConfig
    options:
      members: false
