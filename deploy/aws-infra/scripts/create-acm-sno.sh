#!/bin/bash
# Launch the ACM cluster's single bare-metal node, plus a temporary bootstrap.
#
# Called by the sno-cluster role once ignition exists, not directly: the
# user-data is a pointer built from the installer's output.
#
# Usage: create-acm-sno.sh <rhcos-ami> <sno-userdata-file> <bootstrap-userdata-file>
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq

[ $# -eq 3 ] || die "usage: $0 <rhcos-ami> <sno-userdata-file> <bootstrap-userdata-file>"
AMI="$1"
SNO_USERDATA="$(cat "$2")"
BOOTSTRAP_USERDATA="$(cat "$3")"

[ ${#SNO_USERDATA} -le 4096 ] || die "SNO user-data is ${#SNO_USERDATA} bytes; CloudFormation caps parameters at 4096"

# Bare metal launches fail transiently, and one failure rolls back the stack.
export STACK_CREATE_ATTEMPTS="${ACM_SNO_STACK_ATTEMPTS:-3}"

create_or_update_stack "${ACM_SNO_STACK}" "${TEMPLATE_DIR}/sno-compute-stack.yaml" \
  "ClusterName=${ACM_CLUSTER_NAME}" \
  "SubnetId=$(read_acm_state subnet_id)" \
  "ClusterSecurityGroupId=$(read_acm_state cluster_sg_id)" \
  "AvailabilityZone=${ACM_AVAILABILITY_ZONE}" \
  "RhcosAmi=${AMI}" \
  "SnoInstanceType=${ACM_SNO_INSTANCE_TYPE}" \
  "SshKeyName=${SSH_KEY_NAME}" \
  "SnoPrivateIp=${ACM_SNO_PRIVATE_IP}" \
  "SnoUserData=${SNO_USERDATA}" \
  "RootVolumeSizeGiB=${ACM_SNO_ROOT_VOLUME_GIB}" \
  "DataVolumeSizeGiB=${ACM_SNO_DATA_VOLUME_GIB}"

save_acm_state sno_instance_id  "$(stack_output "${ACM_SNO_STACK}" SnoInstanceId)"
save_acm_state sno_data_volume  "$(stack_output "${ACM_SNO_STACK}" SnoDataVolumeId)"

# The bootstrap machine reuses the TNF site's bootstrap template: same shape, a
# throwaway node that serves a temporary control plane and is then deleted.
create_or_update_stack "${ACM_CLUSTER_NAME}-bootstrap" "${TEMPLATE_DIR}/bootstrap-stack.yaml" \
  "ClusterName=${ACM_CLUSTER_NAME}" \
  "SubnetId=$(read_acm_state subnet_id)" \
  "ClusterSecurityGroupId=$(read_acm_state cluster_sg_id)" \
  "RhcosAmi=${AMI}" \
  "BootstrapInstanceType=${BOOTSTRAP_INSTANCE_TYPE}" \
  "SshKeyName=${SSH_KEY_NAME}" \
  "BootstrapPrivateIp=${ACM_BOOTSTRAP_PRIVATE_IP:-10.1.0.9}" \
  "BootstrapUserData=${BOOTSTRAP_USERDATA}"

green "ACM node and bootstrap launched"
