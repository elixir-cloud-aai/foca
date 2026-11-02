# Getting started

## Installation

Install the [FOCA package](https://pypi.org/project/foca/) (Python 3.11 to
3.15) with `pip`:

```bash
pip install foca
```

## A minimal app

A FOCA app consists of an OpenAPI specification, controllers implementing its
operations, a configuration file and an entry point.

```yaml title="config.yaml"
server:
  host: 127.0.0.1
  port: 8080

api:
  specs:
    hello-api:
      path: openapi.yaml
      add_operation_fields:
        x-openapi-router-controller: controllers
```

```yaml title="openapi.yaml"
openapi: 3.0.2
info:
  title: Hello API
  version: 1.0.0
paths:
  /hello:
    get:
      operationId: hello
      responses:
        "200":
          description: A greeting.
          content:
            application/json:
              schema:
                type: string
```

```python title="controllers.py"
def hello():
    return "Hello, world!"
```

```python title="app.py"
from foca import Foca

if __name__ == "__main__":
    foca = Foca(config_file="config.yaml")
    app = foca.create_app()  # Connexion app
    app.run()
```

```bash
python app.py
curl http://127.0.0.1:8080/hello
```

Relative paths in the configuration (here: `openapi.yaml`) are resolved
against the directory of the configuration file, so the app can be started
from any directory.

The [annotated configuration template](https://github.com/elixir-cloud-aai/foca/blob/dev/templates/config.yaml)
lists all sections; the [configuration reference](configuration/index.md)
describes them in detail.

## Examples

The repository ships two example apps:

- [Petstore](https://github.com/elixir-cloud-aai/foca/tree/dev/examples/petstore):
  OpenAPI specification, MongoDB and custom problem details.
- [Petstore with access control](https://github.com/elixir-cloud-aai/foca/tree/dev/examples/petstore-access-control):
  bearer token validation, enforced policies and resource ownership.

Both can be run with Docker Compose; see their READMEs.

## Docker

FOCA images with all dependencies installed are published as
[`elixircloud/foca`](https://hub.docker.com/r/elixircloud/foca) (built from
[`docker/Dockerfile`](https://github.com/elixir-cloud-aai/foca/blob/dev/docker/Dockerfile)),
for use as base images of apps:

```dockerfile
FROM elixircloud/foca:latest
COPY ./ /app
CMD ["python", "app.py"]
```

`COPY ./ /app` copies the whole build context: keep local files such as
`.env` files or config overlays out of the image with an allowlist
`.dockerignore` (`*`, then `!app.py` etc. for the files the app needs), as
in the examples.

`python app.py` starts Flask's development server, which is meant for local
development only. In production, run the app with a WSGI server such as
Gunicorn (see [running in production](deployment.md#running-in-production)).

Images are published for each supported Python version (3.11 to 3.15).
Images of releases are tagged with the FOCA and Python versions (e.g.,
`{{ version }}-py3.12`); the FOCA version alone (e.g., `{{ version }}`) tags
the image of the default Python version, currently 3.13, and `latest` that
of the highest stable release (not an alpha, beta or release candidate).
Images built from the `dev` branch are tagged with their date and the
Python version (e.g., `20261019-py3.13`).

The default Python version is the newest one in its security-fix phase (cf.
the [status of Python versions](https://devguide.python.org/versions/));
newer versions, still in their bugfix phase, get suffixed tags only. The
default moves on about once a year, after a new Python version is released,
so pin a suffixed tag to keep the Python version.

Images are signed and come with SBOM and provenance attestations, and CI
scans them for vulnerabilities; see [verifying images and
charts](deployment.md#verifying-images-and-charts).

The image contains no build tools and runs as non-root user `foca` (UID
`1000`, group `0`); `/app` and the home directory are writable by group `0`,
so that the image also runs under arbitrary user IDs (e.g., on OpenShift).
To install system or Python packages in derived images, switch to `root` and
back.

### Installing Python packages

The image has no pip (since FOCA 0.20.0): pip vendors copies of other
packages (e.g., urllib3, msgpack and setuptools), which image scans report,
in every image built on FOCA, although only pip uses them, and only at build
time. Install Python packages with [uv](https://docs.astral.sh/uv/) instead,
from a stage of its own that is pinned by digest, and mount it into the
step that installs them, so that the image doesn't contain it:

```dockerfile title="Dockerfile"
FROM ghcr.io/astral-sh/uv:<version>@sha256:<digest> AS uv

FROM elixircloud/foca:<version>-py3.13@sha256:<digest>
USER root
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    UV_NO_CACHE=1 uv pip install --system --require-hashes --only-binary :all: \
        --no-deps --compile-bytecode -r /tmp/requirements.txt \
    && uv pip check --system
USER 1000
COPY ./ /app
```

- `--system` installs into the image's Python (`/usr/local`), where FOCA
  is, not into a virtual environment.
- `--require-hashes` installs only packages whose hashes are in
  `requirements.txt` (e.g., exported from a lock file with `uv export` or
  written by `uv pip compile --generate-hashes`), `--only-binary :all:`
  only wheels, so that nothing is built.
- `--no-deps` installs nothing that is not listed, so `requirements.txt`
  must list all packages that the image needs on top of the FOCA image,
  dependencies included, and leave out those of the FOCA image (FOCA and
  its dependencies), so that these stay as they are.
- `--compile-bytecode` compiles the packages to bytecode, as pip does, as
  Python can't write the bytecode at runtime if the root file system is
  read-only (as in the [Helm chart](helm-chart.md)).
- `uv pip check` fails the build if a dependency of an installed package
  is missing or doesn't fit, which `--no-deps` doesn't check.
- `UV_NO_CACHE=1` keeps uv's cache out of the image.
- `requirements.txt` is mounted from the build context (in the allowlist of
  `.dockerignore`), so the step needs no copy of it.

Install the app itself from its wheel, built in a build stage of its own
(e.g., `build`, with the wheel in `/tmp/dist`), without an index and without
its dependencies, which the step above installed; a freshly built wheel has
no known hash, so without `--require-hashes`:

```dockerfile
RUN --network=none --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=from=build,source=/tmp/dist,target=/tmp/dist \
    UV_NO_CACHE=1 uv pip install --system --no-index --no-deps \
        --compile-bytecode /tmp/dist/*.whl
```

This also applies to images that add [plugins](plugins.md) to an app's
image: list the plugin distributions and their dependencies that the app's
image doesn't have, with their hashes, and install them as above.

Build stages may use pip, e.g., in a virtual environment (`python -m venv`,
which installs the pip that the image's Python bundles), as long as the
final stage doesn't. The [petstore
example](https://github.com/elixir-cloud-aai/foca/tree/dev/examples/petstore)
installs Gunicorn this way, and
[cloud-registry](https://github.com/elixir-cloud-aai/cloud-registry) is a
complete example of an app image.

!!! warning "Debug mode"
    Flask's debug mode (`server.debug`) includes an interactive debugger
    that allows executing arbitrary code. It is off by default; only enable it
    for local development.
