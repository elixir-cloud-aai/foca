FROM python:3.6-slim-stretch as base

# Metadata
LABEL base.image="python:3.6-slim-stretch"
LABEL software="FOCA"
LABEL software.version="0.2.0"
LABEL software.description="Kickstart OpenAPI-based microservice development with Flask & Connexion"
LABEL software.website="https://github.com/elixir-cloud-aai/foca"
LABEL software.documentation="https://github.com/elixir-cloud-aai/foca"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

# Build image
FROM base as builder

# Install general dependencies
ENV PACKAGES openssl git build-essential python3-dev curl jq
RUN apt-get update && \
    apt-get install -y --no-install-recommends ${PACKAGES} && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
WORKDIR /app
COPY ./requirements.txt .
RUN pip install \
    --no-warn-script-location \
    --prefix="/install" \
    -r requirements.txt && \
    pip install https://github.com/elixir-cloud-aai/foca/archive/dev.zip

# Final image
FROM base

# Python UserID workaround for OpenShift/K8S
ENV LOGNAME=ipython
ENV USER=ipython
ENV HOME=/tmp/user

# Install general dependencies
ENV PACKAGES openssl git build-essential python3-dev curl jq
RUN apt-get update && \
    apt-get install -y --no-install-recommends ${PACKAGES} && \
    rm -rf /var/lib/apt/lists/*

# Copy Python packages
COPY --from=builder /install /usr/local
