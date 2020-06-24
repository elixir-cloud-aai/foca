### BASE IMAGE ###
FROM elixircloud/foca:latest

# Metadata
LABEL software="Petstore application"
LABEL software.description="Example application for FOCA microservice archetype"
LABEL software.website="https://github.com/elixir-cloud-aai/foca"
LABEL software.documentation="https://github.com/elixir-cloud-aai/foca"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

## Set working directory
WORKDIR /app

## Copy app files
COPY ./ /app
