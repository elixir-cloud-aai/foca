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

::: foca.models.config.JobsConfig
    options:
      members: false
