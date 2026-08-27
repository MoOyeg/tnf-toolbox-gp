#!/bin/bash
# Run ansible-lint plus a syntax check over every playbook.
set -e

PLAYBOOK_DIR="deploy/openshift-clusters"
CONTAINER_ENGINE=${CONTAINER_ENGINE:-podman}
CONTAINER_IMAGE=${CONTAINER_IMAGE:-ghcr.io/ansible/community-ansible-dev-tools:latest}

if [ "${IN_CONTAINER}" != "" ]; then
  ansible-lint -c .ansible-lint "${PLAYBOOK_DIR}"
  for pb in "${PLAYBOOK_DIR}"/*.yml; do
    echo "syntax-check: ${pb}"
    ansible-playbook --syntax-check -i "${PLAYBOOK_DIR}/inventory.ini.sample" "${pb}"
  done
else
  $CONTAINER_ENGINE run --rm \
    --env IN_CONTAINER=TRUE \
    --volume "${PWD}:/workdir:z" \
    --workdir /workdir \
    "${CONTAINER_IMAGE}" \
    hack/ansible-lint.sh
fi
