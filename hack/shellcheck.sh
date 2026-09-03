#!/bin/bash
# Lint every shell script in the repository.
#
# Gated at 'warning' and above. The notes below that are advisory and all
# currently false: SC1091 fires because the container cannot follow a relative
# source across its read-only mount, and the SC2015/SC2016 hits are deliberate.
# Left ungated they made this target permanently red, which teaches people to
# skip it -- and it is wired into the pre-commit hook.
set -euo pipefail

CONTAINER_ENGINE=${CONTAINER_ENGINE:-podman}
CONTAINER_IMAGE=${CONTAINER_IMAGE:-docker.io/koalaman/shellcheck-alpine:stable}
IN_CONTAINER=${IN_CONTAINER:-}

if [ -n "${IN_CONTAINER}" ]; then
  # ansible_collections is vendored by 'ansible-galaxy collection install' at
  # deploy time. It is gitignored, but find does not know that, so without the
  # prune this lints other people's code and 'make verify' fails on any machine
  # that has ever run a deploy.
  find . -path ./.git -prune \
    -o -name ansible_collections -prune \
    -o -type f -name '*.sh' -print0 \
    | xargs -0 -r shellcheck --format=gcc --external-sources --severity=warning
else
  $CONTAINER_ENGINE run --rm \
    --env IN_CONTAINER=TRUE \
    --volume "${PWD}:/workdir:ro,z" \
    --entrypoint sh \
    --workdir /workdir \
    "${CONTAINER_IMAGE}" \
    hack/shellcheck.sh
fi
