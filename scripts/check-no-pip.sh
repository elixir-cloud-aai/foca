#!/bin/sh
# Check that a Docker image contains no Python installer: no pip (command,
# module or `pip-*.dist-info`, whose vendored packages image scans report)
# and no uv, which images built on FOCA mount from a stage of their own
# instead of copying it (see "Installing Python packages" in
# `docs/getting-started.md`). The wheel that `ensurepip` bundles
# (`pip-*.whl`) is no installed package and is allowed; with `--venv`, the
# script also checks that `python -m venv` installs it. Runs as root, to
# search all directories. Used by CI (`.github/workflows/ci.yml`).
#
# Usage: scripts/check-no-pip.sh [--venv] <image>
set -eu

venv=false
if [ "${1:-}" = "--venv" ]; then
    venv=true
    shift
fi
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 [--venv] <image>" >&2
    exit 2
fi
image="$1"

# Exits with 3 if it finds an installer, so that failures of `docker run`
# itself (e.g., a missing image) don't pass the check
rc=0
docker run --rm --user 0 "${image}" sh -c '
    if command -v pip || command -v pip3 || command -v uv ||
        python -c "import pip" 2>/dev/null ||
        find / -xdev -name "pip-*.dist-info" | grep .; then
        exit 3
    fi' || rc=$?
if [ "${rc}" -eq 3 ]; then
    echo "Image ${image} contains an installer (pip or uv)" >&2
    exit 1
elif [ "${rc}" -ne 0 ]; then
    echo "Checking image ${image} failed (exit code ${rc})" >&2
    exit 1
fi
if [ "${venv}" = true ]; then
    docker run --rm "${image}" sh -c \
        'python -m venv /tmp/venv && /tmp/venv/bin/pip --version'
fi
echo "Image ${image} contains no installer"
