# ![FOCA_logo][foca-logo] &ensp;_Develop Flask microservices quickly!_

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Docs][badge-docs]][badge-url-docs]
[![Coverage][badge-coverage]][badge-url-coverage]
[![GitHub_tag][badge-github-tag]][badge-url-github-tag]
[![PyPI_release][badge-pypi]][badge-url-pypi]

**FOCA** (**F**lask-**O**penAPI-**C**onnexion-**A**rchetype) is an opinionated
archetype that enables fast development of [OpenAPI][res-openapi]-based HTTP
API microservices in [Flask][res-flask], leveraging the excellent
[Connexion][res-connexion] framework.

FOCA reduces the required boilerplate code to fire up your app to a bare
minimum and allows you to _**focus on your application logic**_. It also avoids
unnecessary code repetition and introduces cross-service consistency when
developing multiple applications. Simply write a configuration file, pass
it to FOCA and you're good to go!

Features:

* Validated, typed app configuration, with environment-specific overlays
* OpenAPI 2.x/3.x specifications, merged and modified on the fly
* Errors as [problem details][res-rfc-9457], with configurable media type
* Bearer token ([JWT][res-jwt]) validation for trusted OpenID Connect
  issuers
* Policy-based access control with [Casbin][res-casbin], including a
  permissions API
* [MongoDB][res-mongo-db] databases, collections and indexes
* Background tasks with [RabbitMQ][res-rabbitmq] & [Celery][res-celery]
* Configurable [CORS][res-cors]
* [GA4GH service info][res-service-info] served from the configuration

**Read the [documentation][docs] for details.** Upgrading from an earlier
version? See [Migrating to 0.15.0][docs-migration] (from 0.13 or earlier,
first [Migrating to 0.14.0][docs-migration-0.14]).

## Usage

(1) Install the [FOCA package][badge-url-pypi] with `pip`:

```bash
pip install foca
```

(2) Create a [configuration][docs-config] file, e.g.:

```yaml
api:
  specs:
    - path: api/openapi.yaml
      add_operation_fields:
        x-openapi-router-controller: my_app.controllers
```

(3) Create and run your app:

```python
from foca import Foca

app = Foca(config_file="config.yaml").create_app()  # Connexion app

if __name__ == "__main__":
    app.run()
```

![Hint][img-hint] _**Check out the [Petstore example application][example]
shipped with this repository to see FOCA in action!**_ See [Getting
started][docs-getting-started] for a step-by-step guide.

### Docker image

FOCA images with all dependencies installed are published as
[`elixircloud/foca`][res-foca-docker] (built from
[`docker/Dockerfile`][foca-dockerfile]), for use as base images of apps:

```dockerfile
FROM elixircloud/foca:latest
COPY ./ /app
CMD ["python", "app.py"]
```

`python app.py` starts Flask's development server, which is meant for local
development only. In production, run the app with a WSGI server such as
Gunicorn (see [running in production][docs-production]).

Images of releases are tagged with the FOCA and Python versions (e.g.,
`0.14.0-py3.12`); the FOCA version alone (e.g., `0.14.0`) tags the Python
3.13 image, and `latest` that of the latest release. Images built from the
`dev` branch are tagged with their date and the Python version (e.g.,
`20261019-py3.13`).

The image contains no build tools and runs as non-root user `foca` (UID
`1000`, group `0`); `/app` and the home directory are writable by group `0`,
so that the image also runs under arbitrary user IDs (e.g., on OpenShift).
To install system or Python packages in derived images, switch to `root` and
back:

```dockerfile
USER root
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
USER 1000
```

## Contributing

This project is a community effort and lives off your contributions, be it in
the form of bug reports, feature requests, discussions, or fixes and other code
changes. Please refer to our organization's [contributing
guidelines][res-elixir-cloud-contributing] if you are interested to contribute.
Please mind the [code of conduct][res-elixir-cloud-coc] for all interactions
with the community.

To build the documentation locally, install the `docs` extra and run MkDocs:

```bash
pip install -e .[docs]
mkdocs serve
```

## Versioning

The project adopts [semantic versioning][res-semver]. Currently the service
is in beta stage, so the API may change without further notice.

The version is derived from git tags (via [setuptools-scm][res-setuptools-scm])
and available as `foca.__version__`; builds between releases get
development versions (e.g., `0.14.1.dev3+g1a2b3c4`).

### Releasing

Features and fixes are merged into `dev`. Branch `main` only ever moves
forward to commits on `dev` that are released. To release version `X.Y.Z`
from commit `<sha>` on `dev`, after CI passed for it:

```bash
git push origin <sha>:main  # fails unless it is a fast-forward
git tag -s vX.Y.Z <sha> -m "vX.Y.Z"
git push origin vX.Y.Z
```

Pushing the tag triggers the [release workflow][release-workflow], which
checks that the tagged commit is on `main`, publishes the distributions to
PyPI (after approval in the `pypi` environment), pushes Docker images
tagged `X.Y.Z` and `latest`, and creates a GitHub release with generated
notes. Tags of pre-releases (e.g., `vX.Y.Zrc1`) create pre-releases and do
not update `latest`.

## License

This project is covered by the [Apache License 2.0][license-apache] also
[shipped with this repository][license].

## Contact

The project is a collaborative effort under the umbrella of [ELIXIR Cloud &
AAI][org-elixir-cloud]. Follow the link to get in touch with us via chat or
email. Please mention the name of this service for any inquiry, proposal,
question etc.

![Logo_banner][img-logo-banner]

[badge-build-status]: <https://github.com/elixir-cloud-aai/foca/actions/workflows/checks.yml/badge.svg>
[badge-coverage]: <https://codecov.io/gh/elixir-cloud-aai/foca/branch/dev/graph/badge.svg?branch=dev>
[badge-docs]: <https://readthedocs.org/projects/foca/badge/>
[badge-github-tag]: <https://img.shields.io/github/v/tag/elixir-cloud-aai/foca?color=C39BD3>
[badge-license]: <https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-pypi]: <https://img.shields.io/pypi/v/foca.svg?style=flat&color=C39BD3>
[badge-url-build-status]: <https://github.com/elixir-cloud-aai/foca/actions/workflows/checks.yml>
[badge-url-coverage]: <https://codecov.io/gh/elixir-cloud-aai/foca?branch=dev>
[badge-url-docs]: <https://foca.readthedocs.io/en/latest/>
[badge-url-github-tag]: <https://github.com/elixir-cloud-aai/foca/releases>
[badge-url-license]: <http://www.apache.org/licenses/LICENSE-2.0>
[badge-url-pypi]: <https://pypi.python.org/pypi/foca>
[docs]: <https://foca.readthedocs.io/en/latest/>
[docs-config]: <https://foca.readthedocs.io/en/latest/configuration/>
[docs-getting-started]: <https://foca.readthedocs.io/en/latest/getting-started/>
[docs-migration]: <https://foca.readthedocs.io/en/latest/migration-0.15/>
[docs-migration-0.14]: <https://foca.readthedocs.io/en/latest/migration-0.14/>
[docs-production]: <https://foca.readthedocs.io/en/latest/deployment/#running-in-production>
[example]: examples/petstore/README.md
[foca-dockerfile]: docker/Dockerfile
[foca-logo]: images/foca_logo_192px.png
[img-hint]: images/hint.svg
[img-logo-banner]: images/logo-banner.svg
[license]: LICENSE
[license-apache]: <https://www.apache.org/licenses/LICENSE-2.0>
[org-elixir-cloud]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai>
[release-workflow]: .github/workflows/release.yml
[res-casbin]: <https://casbin.org/>
[res-celery]: <https://docs.celeryq.dev/>
[res-connexion]: <https://github.com/spec-first/connexion>
[res-cors]: <https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS>
[res-elixir-cloud-coc]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md>
[res-elixir-cloud-contributing]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CONTRIBUTING.md>
[res-flask]: <https://flask.palletsprojects.com/>
[res-foca-docker]: <https://hub.docker.com/r/elixircloud/foca>
[res-jwt]: <https://jwt.io>
[res-mongo-db]: <https://www.mongodb.com/>
[res-openapi]: <https://www.openapis.org/>
[res-rabbitmq]: <https://www.rabbitmq.com/>
[res-rfc-9457]: <https://www.rfc-editor.org/rfc/rfc9457>
[res-semver]: <https://semver.org/>
[res-service-info]: <https://github.com/ga4gh-discovery/ga4gh-service-info>
[res-setuptools-scm]: <https://setuptools-scm.readthedocs.io/>
