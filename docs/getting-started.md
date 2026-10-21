# Getting started

## Installation

Install the [FOCA package](https://pypi.org/project/foca/) (Python 3.10 or
later) with `pip`:

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
    - path: openapi.yaml
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

`python app.py` starts Flask's development server, which is meant for local
development only. In production, run the app with a WSGI server such as
Gunicorn (see [running in production](deployment.md#running-in-production)).

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

!!! warning "Debug mode"
    Flask's debug mode (`server.debug`) includes an interactive debugger
    that allows executing arbitrary code. It is off by default; only enable it
    for local development.
