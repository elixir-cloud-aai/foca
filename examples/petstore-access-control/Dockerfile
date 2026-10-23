### BASE IMAGE ###
FROM foca-petstore-access-control-root:latest

# Metadata: OCI annotations, replacing those of the FOCA image
LABEL org.opencontainers.image.title="Petstore access control application"
LABEL org.opencontainers.image.description="Example application for FOCA microservice archetype with access control"
LABEL org.opencontainers.image.url="https://github.com/elixir-cloud-aai/foca"
LABEL org.opencontainers.image.source="https://github.com/elixir-cloud-aai/foca"
LABEL org.opencontainers.image.documentation="https://github.com/elixir-cloud-aai/foca/tree/dev/examples/petstore-access-control"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.authors="ELIXIR On-Cloud <cloud-service@elixir-europe.org>"
LABEL org.opencontainers.image.vendor="ELIXIR On-Cloud"

## Set working directory
WORKDIR /app

## Copy app files
COPY ./ /app
