#!/bin/bash
# Create the bastion: haproxy, the Redfish fencing shim, the ignition server,
# and the cluster's DNS records.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq

stack_exists "${NETWORK_STACK}" || die "run create-network.sh first"

create_or_update_stack "${SERVICES_STACK}" "${TEMPLATE_DIR}/services-stack.yaml" \
  "ParameterKey=ClusterName,ParameterValue=${CLUSTER_NAME}" \
  "ParameterKey=VpcId,ParameterValue=$(read_state vpc_id)" \
  "ParameterKey=SubnetId,ParameterValue=$(read_state subnet_id)" \
  "ParameterKey=BastionSecurityGroupId,ParameterValue=$(read_state bastion_sg_id)" \
  "ParameterKey=HostedZoneId,ParameterValue=$(read_state hosted_zone_id)" \
  "ParameterKey=ClusterDomain,ParameterValue=$(read_state cluster_domain)" \
  "ParameterKey=BastionPrivateIp,ParameterValue=${BASTION_PRIVATE_IP}" \
  "ParameterKey=BastionInstanceType,ParameterValue=${BASTION_INSTANCE_TYPE}" \
  "ParameterKey=SshKeyName,ParameterValue=${SSH_KEY_NAME}"

save_state bastion_instance_id "$(stack_output "${SERVICES_STACK}" BastionInstanceId)"
save_state private_address     "$(stack_output "${SERVICES_STACK}" BastionPrivateIp)"
save_state public_address      "$(stack_output "${SERVICES_STACK}" BastionPublicIp)"
save_state fencing_base_url    "$(stack_output "${SERVICES_STACK}" FencingBaseUrl)"
save_state ignition_base_url   "$(stack_output "${SERVICES_STACK}" IgnitionBaseUrl)"
save_state ssh_user            "ec2-user"

green "bastion ready at $(read_state public_address)"
info  "fencing base URL: $(read_state fencing_base_url)"
