ARG PY_IMAGE=3.13-slim-trixie
FROM python:$PY_IMAGE AS base

# Metadata
LABEL software="FOCA"
LABEL software.description="Kickstart OpenAPI-based microservice development with Flask & Connexion"
LABEL software.website="https://github.com/elixir-cloud-aai/foca"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="alexander.kanitz@alumni.ethz.ch"
LABEL maintainer.organisation="ELIXIR On-Cloud"

# Build image
FROM base AS builder

# Install general dependencies
ENV PACKAGES="openssl git build-essential python3-dev curl jq"
RUN apt-get update && \
    apt-get install -y --no-install-recommends ${PACKAGES} && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app

# Install FOCA and its dependencies; without git history, the version needs
# to be passed explicitly (e.g., `--build-arg FOCA_VERSION=1.2.3`)
COPY pyproject.toml MANIFEST.in README.md LICENSE ./
COPY foca/ ./foca/
ARG FOCA_VERSION=0+unknown
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_FOCA="${FOCA_VERSION}" pip install . \
        --no-warn-script-location \
        --prefix="/install"

# Final image: runtime only, no build tools
FROM base

# Copy Python packages
COPY --from=builder /install /usr/local

# Run as non-root user; group 0 owns the home and app directories, so that
# arbitrary user IDs (e.g., on OpenShift, always in group 0) can write there
ENV HOME=/home/foca
RUN useradd --uid 1000 --gid 0 --create-home --home-dir "${HOME}" foca && \
    mkdir -p /app && \
    chown -R 1000:0 "${HOME}" /app && \
    chmod -R g=u "${HOME}" /app
WORKDIR /app
USER 1000
