### BASE IMAGE ###
FROM foca-petstore-root:latest

# Metadata
LABEL software="Petstore application"
LABEL software.description="Example application for FOCA microservice archetype"
LABEL software.website="https://github.com/elixir-cloud-aai/foca"
LABEL software.documentation="https://github.com/elixir-cloud-aai/foca"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

## Install the WSGI server for deployments (e.g., with the Helm chart in
## `chart/`); Docker Compose runs the development server instead
USER root
RUN pip install --no-cache-dir gunicorn==26.2.2
USER 1000

## Set working directory
WORKDIR /app

## Copy app files
COPY ./ /app

## Serve the app with Gunicorn; further options via `GUNICORN_CMD_ARGS`
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "wsgi:create_app()"]
