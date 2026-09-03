#!/bin/bash
# Format YAML, or with VALIDATE_ONLY=true check it without writing.
set -euo pipefail

CONTAINER_ENGINE=${CONTAINER_ENGINE:-podman}
CONTAINER_IMAGE=${CONTAINER_IMAGE:-ghcr.io/google/yamlfmt:latest}
VALIDATE_ONLY=${VALIDATE_ONLY:-false}
IN_CONTAINER=${IN_CONTAINER:-}

# The in-container branch runs under the image's sh, not bash, so it stays
# POSIX: an array here is a syntax error the moment the container starts.
if [ -n "${IN_CONTAINER}" ]; then
  if [ "${VALIDATE_ONLY}" != "false" ]; then
    yamlfmt -conf .yamlfmt -lint .
  else
    yamlfmt -conf .yamlfmt .
  fi
else
  $CONTAINER_ENGINE run --rm \
    --env IN_CONTAINER=TRUE \
    --env "VALIDATE_ONLY=${VALIDATE_ONLY}" \
    --volume "${PWD}:/workdir:z" \
    --entrypoint sh \
    --workdir /workdir \
    "${CONTAINER_IMAGE}" \
    hack/yamlfmt.sh
fi
