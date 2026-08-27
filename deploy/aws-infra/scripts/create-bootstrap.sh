#!/bin/bash
# Launch the temporary bootstrap node.
# Usage: create-bootstrap.sh <rhcos-ami> <bootstrap-userdata-file>
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq

[ $# -eq 2 ] || die "usage: $0 <rhcos-ami> <bootstrap-userdata-file>"
AMI="$1"
USERDATA="$(cat "$2")"

create_or_update_stack "${BOOTSTRAP_STACK}" "${TEMPLATE_DIR}/bootstrap-stack.yaml" \
  "ClusterName=${CLUSTER_NAME}" \
  "SubnetId=$(read_state subnet_id)" \
  "ClusterSecurityGroupId=$(read_state cluster_sg_id)" \
  "RhcosAmi=${AMI}" \
  "BootstrapInstanceType=${BOOTSTRAP_INSTANCE_TYPE}" \
  "SshKeyName=${SSH_KEY_NAME}" \
  "BootstrapPrivateIp=${BOOTSTRAP_PRIVATE_IP}" \
  "BootstrapUserData=${USERDATA}"

save_state bootstrap_instance_id "$(stack_output "${BOOTSTRAP_STACK}" BootstrapInstanceId)"
green "bootstrap node launched"
