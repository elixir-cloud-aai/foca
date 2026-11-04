### TOOLS ###
## uv, to build the plugin and install the app's packages: the FOCA image has
## no pip. Mounted into the steps that use it only, so that the image doesn't
## contain it; pinned by digest (Dependabot updates the tag and digest together)
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

### PLUGIN ###
## The plugin as a wheel, from a stage of its own, so that neither its build
## tools nor its sources are in the image
FROM foca-gateway-root:latest AS plugin
USER root
COPY plugin /src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    UV_NO_CACHE=1 uv build --wheel --out-dir /wheels /src

### BASE IMAGE ###
FROM foca-gateway-root:latest

# Metadata: OCI annotations, replacing those of the FOCA image
LABEL org.opencontainers.image.title="Gateway example"
LABEL org.opencontainers.image.description="Example gateway application with a plugin for FOCA microservice archetype"
LABEL org.opencontainers.image.url="https://github.com/elixir-cloud-aai/foca"
LABEL org.opencontainers.image.source="https://github.com/elixir-cloud-aai/foca"
LABEL org.opencontainers.image.documentation="https://github.com/elixir-cloud-aai/foca/tree/dev/examples/gateway"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.authors="ELIXIR On-Cloud <cloud-service@elixir-europe.org>"
LABEL org.opencontainers.image.vendor="ELIXIR On-Cloud"

## Install the packages of `requirements.txt` as in the petstore example (see
## its `Dockerfile`) and the plugin, as a real distribution with its entry
## point, without its dependency on FOCA, which the image has; then check that
## the dependencies of all installed packages are satisfied
USER root
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    --mount=from=plugin,source=/wheels,target=/tmp/wheels \
    UV_NO_CACHE=1 uv pip install --system --require-hashes --only-binary :all: \
        --no-deps --compile-bytecode -r /tmp/requirements.txt \
    && UV_NO_CACHE=1 uv pip install --system --no-deps --compile-bytecode \
        /tmp/wheels/*.whl \
    && uv pip check --system
USER 1000

## Set working directory
WORKDIR /app

## Copy app files, except the plugin, which is installed
COPY config.yaml controllers.py gateway.yaml service.py tasks.py worker.py wsgi.py /app/

## Serve the app with Gunicorn, with FOCA's default settings (see the petstore
## example)
CMD ["gunicorn", "--config", "python:foca.gunicorn_config", "wsgi:create_app()"]
