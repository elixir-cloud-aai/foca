### TOOLS ###
## uv, to install the app's packages: the FOCA image has no pip. Mounted into
## the step that installs them only, so that the image doesn't contain it;
## pinned by digest (Dependabot updates the tag and digest together)
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

### BASE IMAGE ###
FROM foca-petstore-root:latest

# Metadata: OCI annotations, replacing those of the FOCA image
LABEL org.opencontainers.image.title="Petstore application"
LABEL org.opencontainers.image.description="Example application for FOCA microservice archetype"
LABEL org.opencontainers.image.url="https://github.com/elixir-cloud-aai/foca"
LABEL org.opencontainers.image.source="https://github.com/elixir-cloud-aai/foca"
LABEL org.opencontainers.image.documentation="https://github.com/elixir-cloud-aai/foca/tree/dev/examples/petstore"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.authors="ELIXIR On-Cloud <cloud-service@elixir-europe.org>"
LABEL org.opencontainers.image.vendor="ELIXIR On-Cloud"

## Install the packages of `requirements.txt` (the WSGI server for
## deployments), into the image's Python (`--system`), at the listed versions,
## checked against their hashes, as wheels only, without their dependencies
## (`--no-deps`, so that the packages of the FOCA image stay as they are), and
## compiled to bytecode, as pip would (the root file system may be read-only
## at runtime); then check that the dependencies of all installed packages
## are satisfied (`uv pip check`), as `--no-deps` doesn't. The requirements
## file is mounted for this step; it also lands in `/app`, as the whole
## directory is copied below, which is harmless
USER root
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    UV_NO_CACHE=1 uv pip install --system --require-hashes --only-binary :all: \
        --no-deps --compile-bytecode -r /tmp/requirements.txt \
    && uv pip check --system
USER 1000

## Set working directory
WORKDIR /app

## Copy app files
COPY ./ /app

## Serve the app with Gunicorn, with FOCA's default settings (port 8080, no
## control socket, which fails on a read-only root file system as in the
## chart); other settings via `GUNICORN_CMD_ARGS`, which take precedence
CMD ["gunicorn", "--config", "python:foca.gunicorn_config", "wsgi:create_app()"]
