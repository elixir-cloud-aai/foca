# FOCA

![FOCA logo](images/foca_logo.svg){ width="160" align=right }

**FOCA** (**F**lask-**O**penAPI-**C**onnexion-**A**rchetype) is an opinionated
archetype for developing [OpenAPI](https://www.openapis.org/)-based HTTP API
microservices in [Flask](https://flask.palletsprojects.com/) with
[Connexion](https://github.com/spec-first/connexion).

FOCA reduces the boilerplate code needed to fire up an app to a bare minimum,
so that you can focus on your application logic, and it makes services that
use it consistent with each other. Write a configuration file, pass it to
FOCA, and you're good to go:

```python
from foca import Foca

app = Foca(config_file="config.yaml").create_app()
app.run()
```

## Features

- **Validated configuration** in a single YAML file, with
  [environment-specific overlays](configuration/index.md#environment-specific-configuration)
  and typed access via [`get_config()`](configuration/index.md#accessing-the-configuration).
- **OpenAPI 2.x/3.x specifications**, including multi-file specifications and
  on-the-fly modifications ([`api`](configuration/api.md)).
- **Problem details** (RFC 9457) for all errors, customizable per exception
  class ([`exceptions`](configuration/exceptions.md)).
- **Bearer token validation** against trusted OpenID Connect issuers
  ([Authentication](authentication.md)).
- **Access control** with Casbin: policies enforced on all operations,
  resource-scoped roles (e.g., owners), and a permissions API
  ([Access control](access-control.md)).
- **MongoDB** databases, collections and indexes ([`db`](configuration/db.md)).
- **Background tasks** with Celery and RabbitMQ
  ([`jobs`](configuration/jobs.md)).
- **CORS**, **logging** and [utilities](utilities.md) for common tasks.
- **Docker** base image ([Getting started](getting-started.md#docker)).

## Apps using FOCA

- [proTES](https://github.com/elixir-cloud-aai/proTES): a proxy service for the
  GA4GH Task Execution Service (TES) API
- [cloud-registry](https://github.com/elixir-cloud-aai/cloud-registry): a
  GA4GH Service Registry implementation
- [More apps](https://github.com/elixir-cloud-aai/foca/network/dependents)

## Project

FOCA is developed by [ELIXIR On-Cloud](https://elixir-cloud-aai.github.io/)
and licensed under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0).
Contributions are welcome; please refer to the [contributing
guidelines](https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CONTRIBUTING.md)
and the [code of
conduct](https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md).
The project adopts [semantic versioning](https://semver.org/); while the major
version is 0, minor releases may contain breaking changes, which are listed in
the [changelog](changelog.md) and migration notes (for
[0.16](migration-0.16.md), [0.15](migration-0.15.md) and
[0.14](migration-0.14.md)).
