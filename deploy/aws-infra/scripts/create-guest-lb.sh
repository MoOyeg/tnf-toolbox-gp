#!/bin/bash
# A public entry point for a guest cluster's control plane.
#
# Usage: create-guest-lb.sh <guest-name>
#
# Runs before the guest is created, not after: the NodePorts are pinned in
# config precisely so the load balancer can be built without having to discover
# what HyperShift chose. Building it first also means the published address
# exists by the time the HostedCluster is rendered, and spec.services cannot be
# changed once the HostedCluster exists.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws

GUEST="${1:?usage: $0 <guest-name>}"
STACK="${GUEST}-cp-lb"

INSTANCE="$(read_acm_state sno_instance_id)"
[ -n "${INSTANCE}" ] || die "no ACM hub instance recorded; run 'make acm-site' first"

create_or_update_stack "${STACK}" "${TEMPLATE_DIR}/guest-lb-stack.yaml" \
  "ClusterName=${GUEST}" \
  "VpcId=$(read_acm_state vpc_id)" \
  "SubnetId=$(read_acm_state subnet_id)" \
  "TargetInstanceId=${INSTANCE}" \
  "ApiNodePort=${GUEST_API_NODEPORT}" \
  "OAuthNodePort=${GUEST_OAUTH_NODEPORT}" \
  "ClusterSecurityGroupId=$(read_acm_state cluster_sg_id)" \
  "AllowedApiCidr=${ALLOWED_API_CIDR:-0.0.0.0/0}" \
  "VpcCidr=$(read_acm_state vpc_cidr)"

DNS="$(stack_output "${STACK}" LoadBalancerDns)"
[ -n "${DNS}" ] || die "stack ${STACK} produced no DNS name"

save_acm_state "${GUEST}_lb_dns" "${DNS}"
green "guest ${GUEST} control plane reachable at ${DNS}"
