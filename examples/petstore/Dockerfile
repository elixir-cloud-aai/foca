### BASE IMAGE ###
FROM foca-petstore-root:latest

# Metadata
LABEL software="Petstore application"
LABEL software.description="Example application for FOCA microservice archetype"
LABEL software.website="https://github.com/elixir-cloud-aai/foca"
LABEL software.documentation="https://github.com/elixir-cloud-aai/foca"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR On-Cloud"

## Install the WSGI server for deployments (e.g., with the Helm chart in
## `chart/`); Docker Compose runs the development server instead. Pinned,
## but not updated by Dependabot: bump it manually
USER root
RUN pip install --no-cache-dir gunicorn==26.2.0
USER 1000

## Set working directory
WORKDIR /app

## Copy app files
COPY ./ /app

## Serve the app with Gunicorn; further options via `GUNICORN_CMD_ARGS`.
## Without Gunicorn's control socket (`gunicornc`), which the app does not
## use and which fails on a read-only root file system (as in the chart)
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--no-control-socket", "wsgi:create_app()"]
