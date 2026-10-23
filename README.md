[![ELIXIR On-Cloud][logo]][org]

# FOCA

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Docs][badge-docs]][badge-url-docs]
[![Coverage][badge-coverage]][badge-url-coverage]
[![GitHub_tag][badge-github-tag]][badge-url-github-tag]
[![Docker_image][badge-docker]][badge-url-docker]
[![PyPI_release][badge-pypi]][badge-url-pypi]

<!-- --8<-- [start:synopsis] -->
An archetype for [OpenAPI](https://www.openapis.org/)-based web services in
Python, brought to you by [ELIXIR On-Cloud](https://elixir-on.cloud/). FOCA
(**F**lask-**O**penAPI-**C**onnexion-**A**rchetype) builds a
[Connexion](https://github.com/spec-first/connexion) app on
[Flask](https://flask.palletsprojects.com/) from your OpenAPI specifications
and a single configuration file, so that you only write the controllers and
the logic of your app. It provides:

- OpenAPI 2.x/3.x specifications, validated and routed by Connexion and
  modified on the fly;
- a validated, typed configuration, with environment-specific overlays;
- errors as [problem details](https://www.rfc-editor.org/rfc/rfc9457);
- bearer token validation for the OpenID Connect providers that you trust;
- access control with [Casbin](https://casbin.org/): policies on all
  operations, roles of users and groups, resource ownership and a
  permissions API;
- [MongoDB](https://www.mongodb.com/) databases, collections and indexes;
- background tasks with [Celery](https://docs.celeryq.dev/) and
  [RabbitMQ](https://www.rabbitmq.com/);
- CORS and a [GA4GH service
  info](https://github.com/ga4gh-discovery/ga4gh-service-info) endpoint;
- Docker base images and a Helm library chart for Kubernetes, signed and with
  SBOM and provenance attestations.
<!-- --8<-- [end:synopsis] -->

**Read the [documentation][docs]** for the configuration, authentication,
access control, the deployment with Docker and Helm, the utilities, the API
reference and development. Upgrading from an earlier version? See [Migrating
to 0.17.0][docs-migration] (from 0.15 or earlier, first [Migrating to
0.16.0][docs-migration-0.16], [Migrating to 0.15.0][docs-migration-0.15] and
[Migrating to 0.14.0][docs-migration-0.14]).

## Why FOCA

<!-- --8<-- [start:why] -->
FOCA's package has about 9,300 lines of code, tested by about 12,600 lines of
tests, and its Helm library chart about 1,750 lines. [Cloud
Registry](https://github.com/elixir-cloud-aai/cloud-registry), for example,
uses almost all of the package (all but the Celery factory and a database
helper) and has about 2,200 lines of code of its own, plus about 9,000
lines of tests. Its own code is thus about a fifth of what it would be
without FOCA, counting code only, and about a third, counting code and tests.
Even if an app needed only half of FOCA's code, FOCA would still save it about
4,500 lines, plus their tests and the chart. (Lines of Python without blank lines and comments,
docstrings included; FOCA 0.17.0 and Cloud Registry in October 2026.)

The savings add up with every app: if you maintain several OpenAPI-based
apps, they all share the same authentication, access control, configuration,
errors, images and Helm chart, and security fixes and upgrades are made once,
in FOCA, instead of in every app.
<!-- --8<-- [end:why] -->

## Apps built with FOCA

<!-- --8<-- [start:apps] -->
| App | Description |
| --- | --- |
| [Cloud Registry](https://github.com/elixir-cloud-aai/cloud-registry) | A registry of web services, implementing the GA4GH Service Registry API |
| [proTES](https://github.com/elixir-cloud-aai/proTES) | A gateway for the GA4GH Task Execution Service (TES) API that federates TES instances |

See also the [dependents of FOCA on
GitHub](https://github.com/elixir-cloud-aai/foca/network/dependents). Built an
app with FOCA? Open a pull request that adds it to the table in
[`README.md`](https://github.com/elixir-cloud-aai/foca/blob/dev/README.md).
<!-- --8<-- [end:apps] -->

## Quick start

Install the [FOCA package][badge-url-pypi] (Python 3.11 or later) with `pip`:

```bash
pip install foca
```

Write a [configuration][docs-config] file that points to your OpenAPI
specification and the module of its controllers, e.g., `config.yaml`:

```yaml
api:
  specs:
    - path: api/openapi.yaml
      add_operation_fields:
        x-openapi-router-controller: my_app.controllers
```

Then create and run your app:

```python
from foca import Foca

app = Foca(config_file="config.yaml").create_app()  # Connexion app

if __name__ == "__main__":
    app.run()
```

See [Getting started][docs-getting-started] for a step-by-step guide and the
Docker image, and the [Petstore example app][example] to see FOCA in action.

## Contributing

Contributions are welcome! See [`CONTRIBUTING.md`][contributing] and
[Contributing][docs-contributing] in the documentation, and mind our [code of
conduct][code-of-conduct].

[badge-build-status]: <https://github.com/elixir-cloud-aai/foca/actions/workflows/checks.yml/badge.svg>
[badge-coverage]: <https://codecov.io/gh/elixir-cloud-aai/foca/branch/dev/graph/badge.svg?branch=dev>
[badge-docker]: <https://img.shields.io/docker/v/elixircloud/foca?sort=semver&label=image&color=C39BD3>
[badge-docs]: <https://readthedocs.org/projects/foca/badge/>
[badge-github-tag]: <https://img.shields.io/github/v/tag/elixir-cloud-aai/foca?color=C39BD3>
[badge-license]: <https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-pypi]: <https://img.shields.io/pypi/v/foca?color=C39BD3>
[badge-url-build-status]: <https://github.com/elixir-cloud-aai/foca/actions/workflows/checks.yml>
[badge-url-coverage]: <https://codecov.io/gh/elixir-cloud-aai/foca?branch=dev>
[badge-url-docker]: <https://hub.docker.com/r/elixircloud/foca/tags>
[badge-url-docs]: <https://foca.readthedocs.io/en/latest/>
[badge-url-github-tag]: <https://github.com/elixir-cloud-aai/foca/releases>
[badge-url-license]: <https://www.apache.org/licenses/LICENSE-2.0>
[badge-url-pypi]: <https://pypi.org/project/foca/>
[code-of-conduct]: <https://github.com/elixir-cloud-aai/foca/blob/dev/CODE_OF_CONDUCT.md>
[contributing]: <https://github.com/elixir-cloud-aai/foca/blob/dev/CONTRIBUTING.md>
[docs]: <https://foca.readthedocs.io/en/latest/>
[docs-config]: <https://foca.readthedocs.io/en/latest/configuration/>
[docs-contributing]: <https://foca.readthedocs.io/en/latest/contributing/>
[docs-getting-started]: <https://foca.readthedocs.io/en/latest/getting-started/>
[docs-migration]: <https://foca.readthedocs.io/en/latest/migration-0.17/>
[docs-migration-0.14]: <https://foca.readthedocs.io/en/latest/migration-0.14/>
[docs-migration-0.15]: <https://foca.readthedocs.io/en/latest/migration-0.15/>
[docs-migration-0.16]: <https://foca.readthedocs.io/en/latest/migration-0.16/>
[example]: <https://github.com/elixir-cloud-aai/foca/tree/dev/examples/petstore>
[logo]: <https://raw.githubusercontent.com/elixir-cloud-aai/foca/dev/docs/images/logo_elixir_on_cloud.png>
[org]: <https://elixir-on.cloud/>
