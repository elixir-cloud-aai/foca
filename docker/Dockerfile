ARG PY_IMAGE=3.13-slim-trixie
FROM python:$PY_IMAGE AS base

# Metadata: OCI annotations (the workflows add none); images built from this
# one inherit them, so they set their own
LABEL org.opencontainers.image.title="FOCA"
LABEL org.opencontainers.image.description="Kickstart OpenAPI-based microservice development with Flask & Connexion"
LABEL org.opencontainers.image.url="https://github.com/elixir-cloud-aai/foca"
LABEL org.opencontainers.image.source="https://github.com/elixir-cloud-aai/foca"
LABEL org.opencontainers.image.documentation="https://foca.readthedocs.io/"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.authors="ELIXIR On-Cloud <cloud-service@elixir-europe.org>"
LABEL org.opencontainers.image.vendor="ELIXIR On-Cloud"

# Images of Python 3.11 ship setuptools and wheel (later ones do not), with
# known vulnerabilities; FOCA needs neither, as pip builds packages in
# isolated environments, so remove them for the same contents on all versions.
# Images of Python 3.11 and 3.12 also ship an old pip (24.0 and 25.0.1) with
# known vulnerabilities (fixed by 26.2: CVE-2025-8869, CVE-2026-1703,
# CVE-2026-3219, CVE-2026-6357, CVE-2026-8643, CVE-2026-13346); pip stays in
# the image, as images built from this one install their packages with it, so
# upgrade it to the latest release (a no-op where the image already ships
# that one)
RUN pip uninstall --yes --no-cache-dir setuptools wheel && \
    pip install --no-cache-dir --upgrade "pip>=26.2"

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
