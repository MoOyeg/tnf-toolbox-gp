#!/bin/bash
# Lint every shell script in the repository.
set -euo pipefail

CONTAINER_ENGINE=${CONTAINER_ENGINE:-podman}
CONTAINER_IMAGE=${CONTAINER_IMAGE:-docker.io/koalaman/shellcheck-alpine:stable}
IN_CONTAINER=${IN_CONTAINER:-}

if [ -n "${IN_CONTAINER}" ]; then
  find . -path ./.git -prune -o -type f -name '*.sh' -print0 \
    | xargs -0 -r shellcheck --format=gcc --external-sources
else
  $CONTAINER_ENGINE run --rm \
    --env IN_CONTAINER=TRUE \
    --volume "${PWD}:/workdir:ro,z" \
    --entrypoint sh \
    --workdir /workdir \
    "${CONTAINER_IMAGE}" \
    hack/shellcheck.sh
fi
