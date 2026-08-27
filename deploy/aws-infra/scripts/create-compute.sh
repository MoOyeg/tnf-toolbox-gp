#!/bin/bash
# Launch the two g4dn.metal control-plane nodes.
#
# Called by the tnf-install role once ignition exists, not directly: the
# per-node user-data is an ignition pointer that must be built from the
# installer's output.
#
# Usage: create-compute.sh <rhcos-ami> <master0-userdata-file> <master1-userdata-file>
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq

[ $# -eq 3 ] || die "usage: $0 <rhcos-ami> <master0-userdata-file> <master1-userdata-file>"
AMI="$1"
M0_USERDATA="$(cat "$2")"
M1_USERDATA="$(cat "$3")"

# CloudFormation caps a String parameter at 4096 characters. The pointers are a
# few hundred bytes, so this only fires if someone has inlined a full config
# instead of a pointer -- a mistake worth catching before a 15-minute launch.
[ ${#M0_USERDATA} -le 4096 ] || die "master-0 user-data is ${#M0_USERDATA} bytes; CloudFormation caps parameters at 4096"
[ ${#M1_USERDATA} -le 4096 ] || die "master-1 user-data is ${#M1_USERDATA} bytes; CloudFormation caps parameters at 4096"

create_or_update_stack "${COMPUTE_STACK}" "${TEMPLATE_DIR}/compute-stack.yaml" \
  "ParameterKey=ClusterName,ParameterValue=${CLUSTER_NAME}" \
  "ParameterKey=SubnetId,ParameterValue=$(read_state subnet_id)" \
  "ParameterKey=ClusterSecurityGroupId,ParameterValue=$(read_state cluster_sg_id)" \
  "ParameterKey=AvailabilityZone,ParameterValue=${AVAILABILITY_ZONE}" \
  "ParameterKey=RhcosAmi,ParameterValue=${AMI}" \
  "ParameterKey=MasterInstanceType,ParameterValue=${MASTER_INSTANCE_TYPE}" \
  "ParameterKey=SshKeyName,ParameterValue=${SSH_KEY_NAME}" \
  "ParameterKey=Master0PrivateIp,ParameterValue=${MASTER0_PRIVATE_IP}" \
  "ParameterKey=Master1PrivateIp,ParameterValue=${MASTER1_PRIVATE_IP}" \
  "ParameterKey=Master0UserData,ParameterValue=${M0_USERDATA}" \
  "ParameterKey=Master1UserData,ParameterValue=${M1_USERDATA}" \
  "ParameterKey=RootVolumeSizeGiB,ParameterValue=${ROOT_VOLUME_GIB}" \
  "ParameterKey=DataVolumeSizeGiB,ParameterValue=${DATA_VOLUME_GIB}"

save_state master0_instance_id  "$(stack_output "${COMPUTE_STACK}" Master0InstanceId)"
save_state master1_instance_id  "$(stack_output "${COMPUTE_STACK}" Master1InstanceId)"
save_state master0_data_volume  "$(stack_output "${COMPUTE_STACK}" Master0DataVolumeId)"
save_state master1_data_volume  "$(stack_output "${COMPUTE_STACK}" Master1DataVolumeId)"

green "control-plane nodes launched"
