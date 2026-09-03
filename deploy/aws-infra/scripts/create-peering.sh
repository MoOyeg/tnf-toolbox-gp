#!/bin/bash
# Peer the ACM site's VPC with the TNF site's, and route between them.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq

TNF_VPC="$(read_state vpc_id)"
ACM_VPC="$(read_acm_state vpc_id)"
[ -n "${TNF_VPC}" ] || die "no TNF VPC recorded; run 'make infra' first"
[ -n "${ACM_VPC}" ] || die "no ACM VPC recorded; run 'make acm-infra' first"

TNF_RTB="$(stack_output "${NETWORK_STACK}" RouteTableId)"
ACM_RTB="$(read_acm_state route_table_id)"
[ -n "${TNF_RTB}" ] || die "the TNF network stack predates the RouteTableId output; re-run 'make infra' to update it"

info "peering ${ACM_VPC} (${ACM_VPC_CIDR}) <-> ${TNF_VPC} (${VPC_CIDR})"

# PeerOwnerId is left empty for a same-account peering, which auto-accepts. When
# the sites move to separate profiles this is the account id of the TNF side, and
# the accepter has to accept the request explicitly.
create_or_update_stack "${PEERING_STACK}" "${TEMPLATE_DIR}/peering-stack.yaml" \
  "AcmVpcId=${ACM_VPC}" \
  "AcmVpcCidr=${ACM_VPC_CIDR}" \
  "AcmRouteTableId=${ACM_RTB}" \
  "TnfVpcId=${TNF_VPC}" \
  "TnfVpcCidr=${VPC_CIDR}" \
  "TnfRouteTableId=${TNF_RTB}" \
  "PeerOwnerId=${TNF_ACCOUNT_ID:-}"

save_acm_state peering_id "$(stack_output "${PEERING_STACK}" PeeringConnectionId)"

# A peering connection reports active long before anyone has proven a packet
# crosses it. The ACM bastion reaching the TNF bastion is that proof.
ACM_BASTION="$(read_acm_state public_address)"
TNF_PRIVATE="$(read_state private_address)"
if [ -n "${ACM_BASTION}" ] && [ -n "${TNF_PRIVATE}" ]; then
  info "checking the ACM bastion can reach the TNF bastion across the peering"
  if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
       -o ConnectTimeout=10 -i "${SSH_PRIVATE_KEY}" "ec2-user@${ACM_BASTION}" \
       "timeout 5 bash -c 'echo > /dev/tcp/${TNF_PRIVATE}/22'" 2>/dev/null; then
    green "peering verified: ACM reaches ${TNF_PRIVATE} across the connection"
  else
    red "peering is up but ${TNF_PRIVATE}:22 is unreachable from the ACM bastion"
    die "check the route tables and the PeerVpcCidr security group rules on both sides"
  fi
fi

# Without this the ACM cluster resolves api.<tnf>.<domain> through public DNS to
# the TNF bastion's elastic IP, and the traffic leaves the VPC and comes back in
# over the internet -- working, but not using the peering that was just built.
# Associating TNF's private zone with the ACM VPC makes the same name resolve to
# 10.0.0.5 and stay on the connection.
TNF_PRIVATE_ZONE="$(read_state hosted_zone_id)"
if [ -n "${TNF_PRIVATE_ZONE}" ]; then
  info "associating TNF's private zone with the ACM VPC so its API resolves over the peering"
  aws route53 associate-vpc-with-hosted-zone \
    --hosted-zone-id "${TNF_PRIVATE_ZONE}" \
    --vpc "VPCRegion=${REGION},VPCId=${ACM_VPC}" \
    --query 'ChangeInfo.Status' --output text >/dev/null 2>&1 \
    && green "  TNF private zone now resolves inside the ACM VPC" \
    || info "  already associated"
fi

# And the reverse, so TNF can resolve the ACM cluster by name.
ACM_PRIVATE_ZONE="$(read_acm_state hosted_zone_id)"
if [ -n "${ACM_PRIVATE_ZONE}" ]; then
  aws route53 associate-vpc-with-hosted-zone \
    --hosted-zone-id "${ACM_PRIVATE_ZONE}" \
    --vpc "VPCRegion=${REGION},VPCId=${TNF_VPC}" \
    --query 'ChangeInfo.Status' --output text >/dev/null 2>&1 \
    && green "  ACM private zone now resolves inside the TNF VPC" \
    || info "  already associated"
fi

green "peering ${PEERING_STACK}: $(read_acm_state peering_id)"
