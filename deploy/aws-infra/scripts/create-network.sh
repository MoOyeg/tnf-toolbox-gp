#!/bin/bash
# Create the VPC, subnet, security groups and private hosted zone.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq

create_or_update_stack "${NETWORK_STACK}" "${TEMPLATE_DIR}/network-stack.yaml" \
  "ParameterKey=ClusterName,ParameterValue=${CLUSTER_NAME}" \
  "ParameterKey=BaseDomain,ParameterValue=${BASE_DOMAIN}" \
  "ParameterKey=VpcCidr,ParameterValue=${VPC_CIDR}" \
  "ParameterKey=SubnetCidr,ParameterValue=${SUBNET_CIDR}" \
  "ParameterKey=AvailabilityZone,ParameterValue=${AVAILABILITY_ZONE}" \
  "ParameterKey=AllowedSshCidr,ParameterValue=${ALLOWED_SSH_CIDR}"

save_state vpc_id           "$(stack_output "${NETWORK_STACK}" VpcId)"
save_state subnet_id        "$(stack_output "${NETWORK_STACK}" SubnetId)"
save_state cluster_sg_id    "$(stack_output "${NETWORK_STACK}" ClusterSecurityGroupId)"
save_state bastion_sg_id    "$(stack_output "${NETWORK_STACK}" BastionSecurityGroupId)"
save_state hosted_zone_id   "$(stack_output "${NETWORK_STACK}" HostedZoneId)"
save_state cluster_domain   "$(stack_output "${NETWORK_STACK}" ClusterDomain)"

green "network ready: $(read_state cluster_domain) in $(read_state vpc_id)"
