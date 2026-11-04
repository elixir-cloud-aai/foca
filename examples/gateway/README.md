# FOCA gateway example

A minimal gateway app on [FOCA](https://foca.readthedocs.io/) with a real
plugin, for FOCA's integration tests and chart installation in CI. It shows how
the parts of a gateway fit together; start from the [petstore
example](../petstore/README.md) for what FOCA does otherwise.

- `plugin/`: the plugin, a distribution with an entry point of the group
  `foca.plugins`. The image builds it as a wheel and installs it. It adds a
  stamp to requests, counts them in `ctx.store` and adds a key from
  `ctx.store.key_for()`.
- `config.yaml`: allows the plugin and sets the pipeline (`plugins`).
- `service.py`: the `GatewayApp` settings, shared by app and worker, and the
  run of a request through all phases. There is no real backend: the attempt
  is reported as accepted, so that the example needs no other service.
- `gateway.yaml`, `controllers.py`, `wsgi.py`: the API. `POST /messages` runs
  the pipeline in the app, `POST /messages/worker` in the Celery worker.
- `tasks.py`, `worker.py`: the worker.
- `docker-compose.yaml`: app, worker, MongoDB and RabbitMQ, with the app on
  port 8081. Its credentials and key secret are for this example only.
- `chart/`: a thin chart over the library chart `foca-common`, with only what
  CI needs. It sets `pluginStore.existingSecret`, and its `helm test` sends
  messages through the app and the worker.

## Running it

```bash
docker compose build foca-gateway-root  # FOCA image
docker compose up -d --build
curl -X POST http://localhost:8081/messages \
    -H 'Content-Type: application/json' -d '{"message": "hello"}'
```

The response holds the payload after the plugin and the provenance record of
the run. The tests are in `tests/integration_gateway_tests.py`, see the
[contributing guide](../../docs/contributing/index.md).
