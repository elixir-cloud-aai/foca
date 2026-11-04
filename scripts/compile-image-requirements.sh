#!/bin/sh
# Compile the hashed requirements of the builder stage of `docker/Dockerfile`,
# from `pyproject.toml`, in `docker/requirements/`: one file per Python version
# of the image with FOCA's dependencies, and one for the build tools (the
# `[build-system]` requirements and pip). Pins in the files stay unless they
# no longer fit; `--upgrade` updates all of them. Needs uv and Python 3.11 or
# later. See "Updating dependencies of the image" in
# `docs/contributing/index.md`.
#
# Usage: scripts/compile-image-requirements.sh [--check | --upgrade]
#   --check: only fail if pins don't fit `pyproject.toml`, or if a Python
#     version of the matrix of the image has no file. Hashes aren't compared,
#     as a pinned version can gain wheels at any time.
set -eu

PYTHON_VERSIONS="3.11 3.12 3.13 3.14 3.15"
# See the builder stage of `docker/Dockerfile`: images of Python 3.11 and 3.12
# ship an old pip with known vulnerabilities
PIP_REQUIREMENT="pip>=26.2"
TARGET=docker/requirements
# Resolving for a Python version doesn't need that version
export UV_PYTHON_DOWNLOADS=never

mode="${1:-}"
case "${mode}" in
"" | --check | --upgrade) ;;
*)
    echo "Usage: $0 [--check | --upgrade]" >&2
    exit 2
    ;;
esac

cd "$(dirname "$0")/.."
upgrade=""
[ "${mode}" = "--upgrade" ] && upgrade="--upgrade"

# Compile a file from `pyproject.toml` or another input into a temporary copy,
# so that a failure changes none of the files
compile() {
    file="$1"
    shift
    output="${work}/${file}"
    if ! cp "${TARGET}/${file}" "${output}" 2>/dev/null \
        && [ "${mode}" = "--check" ]; then
        echo "Missing: ${TARGET}/${file}" >&2
        status=1
        return 0
    fi
    # shellcheck disable=SC2086 # `upgrade` is empty or one option
    uv pip compile --quiet --generate-hashes --no-header ${upgrade} \
        --output-file "${output}" "$@"
    if [ "${mode}" = "--check" ]; then
        pins="${work}/pins"
        grep -E '^[A-Za-z0-9._-]+==' "${TARGET}/${file}" >"${pins}.old" || true
        grep -E '^[A-Za-z0-9._-]+==' "${output}" >"${pins}.new" || true
        if ! diff -u "${pins}.old" "${pins}.new"; then
            echo "Stale: ${TARGET}/${file}; run $0" >&2
            status=1
        fi
    fi
}

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
status=0

for version in ${PYTHON_VERSIONS}; do
    compile "py${version}.txt" pyproject.toml \
        --python-version "${version}" --python-platform linux
done

build_requirements="${work}/build.in"
python3 - >"${build_requirements}" <<EOF
import tomllib

with open("pyproject.toml", "rb") as file:
    print(*tomllib.load(file)["build-system"]["requires"], sep="\n")
print("${PIP_REQUIREMENT}")
EOF
compile build.txt "${build_requirements}" \
    --python-version 3.11 --universal --no-annotate

if [ "${mode}" != "--check" ]; then
    mv "${work}"/*.txt "${TARGET}/"
fi

exit "${status}"
