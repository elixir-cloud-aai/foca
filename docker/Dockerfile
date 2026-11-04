ARG PY_IMAGE=3.13-slim-trixie

# Base images by digest, updated by Dependabot; one stage per Python version,
# of which only that of `PY_IMAGE` is built (a new version also needs a file in
# `docker/requirements/`)
FROM python:3.11-slim-trixie@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893 AS python-3.11-slim-trixie
FROM python:3.12-slim-trixie@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS python-3.12-slim-trixie
FROM python:3.13-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS python-3.13-slim-trixie
FROM python:3.14-slim-trixie@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966 AS python-3.14-slim-trixie
FROM python:3.15-slim-trixie@sha256:8c13a3f37a6a5b71498228e902c4b9f51c1f7900d49fabf71f9a650c6120e0d0 AS python-3.15-slim-trixie

FROM python-$PY_IMAGE AS base

# Metadata: OCI annotations (the workflows add none; version and revision
# come from build arguments, at the end); images built from this one inherit
# them, so they set their own, including version and revision
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
# isolated environments, so remove them for the same contents on all versions
RUN pip uninstall --yes --no-cache-dir setuptools wheel

# Build image: installs FOCA with the pip of the Python image, which is
# upgraded first, as images of Python 3.11 and 3.12 ship an old pip (24.0 and
# 25.0.1) with known vulnerabilities (fixed by 26.2: CVE-2025-8869,
# CVE-2026-1703, CVE-2026-3219, CVE-2026-6357, CVE-2026-8643,
# CVE-2026-13346)
FROM base AS builder

# Install general dependencies
ENV PACKAGES="openssl git build-essential python3-dev curl jq"
RUN apt-get update && \
    apt-get install -y --no-install-recommends ${PACKAGES} && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app

# Install pip and the build tools, and FOCA's dependencies, as compiled in
# `docker/requirements/` with their hashes (only the latter go into the final
# image), then FOCA alone; files copied here must be in the allowlist of
# `.dockerignore`
COPY docker/requirements/ ./requirements/
ARG PY_IMAGE
RUN pip install --no-cache-dir --require-hashes -r requirements/build.txt
RUN pip install --no-cache-dir --require-hashes --no-deps \
        --no-warn-script-location \
        --prefix="/install" \
        -r "requirements/py${PY_IMAGE%%-*}.txt"

# Without git history, the version needs to be passed explicitly (e.g.,
# `--build-arg FOCA_VERSION=1.2.3`)
COPY pyproject.toml MANIFEST.in README.md LICENSE NOTICE ./
COPY foca/ ./foca/
ARG FOCA_VERSION=0+unknown
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_FOCA="${FOCA_VERSION}" pip install . \
        --no-cache-dir \
        --no-deps \
        --no-build-isolation \
        --no-warn-script-location \
        --prefix="/install"

# Final image: runtime only, no build tools and no installer. pip is
# removed, with its vendored copies of other packages (e.g., urllib3, msgpack
# and setuptools), which image scans report although only pip uses them, and
# only at build time; images built from this one install their packages with
# uv instead, from a stage of their own, without copying it into the image
# (see "Installing Python packages" in `docs/getting-started.md`); the
# integration and scan jobs of CI check that (`scripts/check-no-pip.sh`).
# The pip wheel that `ensurepip` bundles stays (image scans don't report
# it), so that `python -m venv` keeps working, e.g., in build stages of
# images built from this one
FROM base
RUN pip uninstall --yes --no-cache-dir pip

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

# Version and commit of FOCA, as OCI annotations; set by the workflows (e.g.,
# `--build-arg FOCA_VERSION=1.2.3 --build-arg FOCA_REVISION=<commit>`); last,
# so that other layers stay cached (declared again, as arguments are per stage)
ARG FOCA_VERSION=0+unknown
ARG FOCA_REVISION=unknown
LABEL org.opencontainers.image.version="${FOCA_VERSION}"
LABEL org.opencontainers.image.revision="${FOCA_REVISION}"
