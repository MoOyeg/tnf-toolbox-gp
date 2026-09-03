#!/bin/bash
# Create the ACM site: its own VPC, bastion and DNS.
#
# Reuses the same templates as the TNF site with different parameters. The two
# sites differ in addressing and size, not in shape, so a second set of templates
# would only be two places to fix every future bug.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq
resolve_public_hosted_zone

# Peering will not route between overlapping ranges, and AWS refuses the
# connection outright -- better to say so here than to debug silent packet loss.
if [ "${ACM_VPC_CIDR}" = "${VPC_CIDR}" ]; then
  die "ACM_VPC_CIDR (${ACM_VPC_CIDR}) must not equal VPC_CIDR (${VPC_CIDR}); peering cannot route between overlapping ranges"
fi

info "ACM site: ${ACM_CLUSTER_NAME}.${BASE_DOMAIN} in ${ACM_VPC_CIDR}"

# PeerVpcCidr is set on both sides from static config, so each security group
# admits the other before either peer exists.
create_or_update_stack "${ACM_NETWORK_STACK}" "${TEMPLATE_DIR}/network-stack.yaml" \
  "ClusterName=${ACM_CLUSTER_NAME}" \
  "BaseDomain=${BASE_DOMAIN}" \
  "VpcCidr=${ACM_VPC_CIDR}" \
  "SubnetCidr=${ACM_SUBNET_CIDR}" \
  "AvailabilityZone=${ACM_AVAILABILITY_ZONE}" \
  "AllowedSshCidr=${ALLOWED_SSH_CIDR}" \
  "AllowedApiCidr=${ALLOWED_API_CIDR:-0.0.0.0/0}" \
  "PeerVpcCidr=${VPC_CIDR}"

save_acm_state vpc_id         "$(stack_output "${ACM_NETWORK_STACK}" VpcId)"
save_acm_state vpc_cidr       "${ACM_VPC_CIDR}"
save_acm_state subnet_id      "$(stack_output "${ACM_NETWORK_STACK}" SubnetId)"
save_acm_state route_table_id "$(stack_output "${ACM_NETWORK_STACK}" RouteTableId)"
save_acm_state cluster_sg_id  "$(stack_output "${ACM_NETWORK_STACK}" ClusterSecurityGroupId)"
save_acm_state bastion_sg_id  "$(stack_output "${ACM_NETWORK_STACK}" BastionSecurityGroupId)"
save_acm_state hosted_zone_id "$(stack_output "${ACM_NETWORK_STACK}" HostedZoneId)"
save_acm_state cluster_domain "$(stack_output "${ACM_NETWORK_STACK}" ClusterDomain)"

create_or_update_stack "${ACM_SERVICES_STACK}" "${TEMPLATE_DIR}/services-stack.yaml" \
  "ClusterName=${ACM_CLUSTER_NAME}" \
  "VpcId=$(read_acm_state vpc_id)" \
  "SubnetId=$(read_acm_state subnet_id)" \
  "BastionSecurityGroupId=$(read_acm_state bastion_sg_id)" \
  "HostedZoneId=$(read_acm_state hosted_zone_id)" \
  "PublicHostedZoneId=${PUBLIC_ZONE_ID}" \
  "ClusterDomain=$(read_acm_state cluster_domain)" \
  "BastionPrivateIp=${ACM_BASTION_PRIVATE_IP}" \
  "BastionInstanceType=${BASTION_INSTANCE_TYPE}" \
  "SshKeyName=${SSH_KEY_NAME}"

save_acm_state bastion_instance_id "$(stack_output "${ACM_SERVICES_STACK}" BastionInstanceId)"
save_acm_state private_address     "$(stack_output "${ACM_SERVICES_STACK}" BastionPrivateIp)"
save_acm_state public_address      "$(stack_output "${ACM_SERVICES_STACK}" BastionPublicIp)"
save_acm_state ignition_base_url   "$(stack_output "${ACM_SERVICES_STACK}" IgnitionBaseUrl)"
save_acm_state api_url             "$(stack_output "${ACM_SERVICES_STACK}" ApiUrl)"
save_acm_state ssh_user            "ec2-user"

green "ACM site ready: bastion $(read_acm_state public_address)"
info  "ACM cluster API will be $(read_acm_state api_url)"
