###################################################
#   Stage 1: Get poetry                           #
###################################################
ARG PY_IMAGE=3.12-slim-bookworm
FROM python:$PY_IMAGE AS poetry

ENV PYTHONDONTWRITEBYTECODE=1 \
PYTHONUNBUFFERED=1 

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        && rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 - && \
        ln -s /root/.local/bin/poetry /usr/local/bin/poetry

###################################################
#   Stage 2: Get deps                             #
###################################################
FROM poetry AS builder

WORKDIR /app

COPY . /app/

RUN poetry config virtualenvs.create false \
    && poetry build

# ###################################################
# #   Stage 3: Add Metadata and install wheel       #
# ###################################################
FROM python:$PY_IMAGE

COPY --from=builder /app/dist /app/dist

RUN pip install /app/dist/*.whl

LABEL org.opencontainers.image.title="FOCA"
LABEL org.opencontainers.image.description="Kickstart OpenAPI-based microservice development with Flask & Connexion"
LABEL org.opencontainers.image.source="https://github.com/elixir-cloud-aai/foca"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.authors="alexander.kanitz@alumni.ethz.ch"
LABEL org.opencontainers.image.vendor="ELIXIR Cloud & AAI"

# Python UserID workaround for OpenShift/K8S
ENV LOGNAME=ipython
ENV USER=ipython
ENV HOME=/tmp/user

WORKDIR /app