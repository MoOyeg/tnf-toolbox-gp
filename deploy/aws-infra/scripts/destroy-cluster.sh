#!/bin/bash
# Delete the OpenShift cluster (bootstrap + both control-plane nodes and their
# data volumes) while leaving the network and bastion in place, so a rebuild
# does not have to re-create the VPC, the DNS records, or the fencing endpoint
# -- and so BASTION_PRIVATE_IP, which the fencing address embeds, stays put.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws

delete_stack "${BOOTSTRAP_STACK}"
delete_stack "${COMPUTE_STACK}"

rm -f "${STATE_DIR}"/bootstrap_instance_id \
      "${STATE_DIR}"/master*_instance_id \
      "${STATE_DIR}"/master*_data_volume

green "cluster deleted; network and bastion left in place"
info  "rebuild with: make tnf"
