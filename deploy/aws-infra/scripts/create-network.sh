#!/bin/bash
# Create the VPC, subnet, security groups and private hosted zone.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq

# Settle the public zone before creating anything: the cluster domain derives
# from it, and it is baked into DNS records, certificates and the install-config.
resolve_public_hosted_zone
info "public zone: ${BASE_DOMAIN} (${PUBLIC_ZONE_ID})"
info "cluster domain will be: ${CLUSTER_NAME}.${BASE_DOMAIN}"

create_or_update_stack "${NETWORK_STACK}" "${TEMPLATE_DIR}/network-stack.yaml" \
  "ClusterName=${CLUSTER_NAME}" \
  "BaseDomain=${BASE_DOMAIN}" \
  "VpcCidr=${VPC_CIDR}" \
  "SubnetCidr=${SUBNET_CIDR}" \
  "AvailabilityZone=${AVAILABILITY_ZONE}" \
  "AllowedSshCidr=${ALLOWED_SSH_CIDR}" \
  "AllowedApiCidr=${ALLOWED_API_CIDR:-0.0.0.0/0}" \
  "PeerVpcCidr=${ACM_VPC_CIDR:-}"

save_state vpc_id           "$(stack_output "${NETWORK_STACK}" VpcId)"
save_state route_table_id   "$(stack_output "${NETWORK_STACK}" RouteTableId)"
save_state subnet_id        "$(stack_output "${NETWORK_STACK}" SubnetId)"
save_state cluster_sg_id    "$(stack_output "${NETWORK_STACK}" ClusterSecurityGroupId)"
save_state bastion_sg_id    "$(stack_output "${NETWORK_STACK}" BastionSecurityGroupId)"
save_state hosted_zone_id   "$(stack_output "${NETWORK_STACK}" HostedZoneId)"
save_state cluster_domain   "$(stack_output "${NETWORK_STACK}" ClusterDomain)"

green "network ready: $(read_state cluster_domain) in $(read_state vpc_id)"
