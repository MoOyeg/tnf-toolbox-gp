#!/bin/bash
# Format YAML, or with VALIDATE_ONLY=true check it without writing.
set -euo pipefail

CONTAINER_ENGINE=${CONTAINER_ENGINE:-podman}
CONTAINER_IMAGE=${CONTAINER_IMAGE:-ghcr.io/google/yamlfmt:latest}
VALIDATE_ONLY=${VALIDATE_ONLY:-false}
IN_CONTAINER=${IN_CONTAINER:-}

if [ -n "${IN_CONTAINER}" ]; then
  args=(-conf .yamlfmt)
  [ "${VALIDATE_ONLY}" != "false" ] && args+=(-lint)
  yamlfmt "${args[@]}" .
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
