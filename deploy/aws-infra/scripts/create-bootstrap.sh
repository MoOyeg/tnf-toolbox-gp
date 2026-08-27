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
  "ParameterKey=ClusterName,ParameterValue=${CLUSTER_NAME}" \
  "ParameterKey=SubnetId,ParameterValue=$(read_state subnet_id)" \
  "ParameterKey=ClusterSecurityGroupId,ParameterValue=$(read_state cluster_sg_id)" \
  "ParameterKey=RhcosAmi,ParameterValue=${AMI}" \
  "ParameterKey=BootstrapInstanceType,ParameterValue=${BOOTSTRAP_INSTANCE_TYPE}" \
  "ParameterKey=SshKeyName,ParameterValue=${SSH_KEY_NAME}" \
  "ParameterKey=BootstrapPrivateIp,ParameterValue=${BOOTSTRAP_PRIVATE_IP}" \
  "ParameterKey=BootstrapUserData,ParameterValue=${USERDATA}"

save_state bootstrap_instance_id "$(stack_output "${BOOTSTRAP_STACK}" BootstrapInstanceId)"
green "bootstrap node launched"
