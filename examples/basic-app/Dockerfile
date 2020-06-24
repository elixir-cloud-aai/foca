### BASE IMAGE ###
FROM python:3.6-slim-stretch

# Metadata
LABEL base.image="python:3.6-slim-stretch"
LABEL software="Petstore Application"
LABEL software.version="0.0.1"
LABEL software.description="Kickstart OpenAPI-based microservice development with Flask & Connexion"
LABEL software.website="https://github.com/elixir-cloud-aai/foca"
LABEL software.documentation="https://github.com/elixir-cloud-aai/foca"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

## Set working directory
WORKDIR /app

## Install Python dependencies
RUN cd /app \
  && pip install foca
