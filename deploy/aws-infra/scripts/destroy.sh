#!/bin/bash
# Tear down every stack, newest dependency first.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws

cat >&2 <<WARN

This deletes the whole environment for cluster '${CLUSTER_NAME}' in ${REGION}:

  - ${BOOTSTRAP_STACK}
  - ${COMPUTE_STACK}   (both g4dn.metal nodes AND their EBS data volumes)
  - ${SERVICES_STACK}  (bastion, elastic IP, DNS records)
  - ${NETWORK_STACK}   (VPC, subnet, hosted zone)

The data volumes hold the guest clusters' hosted control-plane etcd. Deleting
them destroys those clusters' state.

WARN
read -r -p "Type the cluster name to confirm: " confirm
[ "${confirm}" = "${CLUSTER_NAME}" ] || die "confirmation did not match; nothing deleted"

delete_stack "${BOOTSTRAP_STACK}"
delete_stack "${COMPUTE_STACK}"
delete_stack "${SERVICES_STACK}"
delete_stack "${NETWORK_STACK}"

rm -rf "${STATE_DIR}"
green "environment ${CLUSTER_NAME} destroyed"
