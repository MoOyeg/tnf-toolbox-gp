#!/bin/bash
# A public entry point for a guest cluster's control plane.
#
# Usage: create-guest-lb.sh <guest-name> [site]
#
#   site: tnf (default) or acm -- which cluster hosts the control plane.
#
# Runs before the guest is created, not after: the NodePorts are pinned in
# config precisely so the load balancer can be built without having to discover
# what HyperShift chose. Building it first also means the published address
# exists by the time the HostedCluster is rendered, and spec.services cannot be
# changed once the HostedCluster exists.
#
# The site matters because a NodePort only answers on nodes of the cluster
# running the pods behind it. 'make guests' creates the control plane on TNF, so
# the targets are TNF's two masters in the TNF VPC; 'make hcp-make-guests-from-acm'
# creates it on the hub, so the target is the hub's single node in the ACM VPC.
# Point this at the wrong site and the stack builds, the targets never turn
# healthy, and the console times out with nothing to say why.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws

GUEST="${1:?usage: $0 <guest-name> [tnf|acm]}"
SITE="${2:-tnf}"
STACK="${GUEST}-cp-lb"

case "${SITE}" in
  tnf)
    # Both masters. TNF is two nodes and either can be fenced, so registering
    # one would put the guest's console behind the node most likely to go away.
    TARGET="$(read_state master0_instance_id)"
    SECOND_TARGET="$(read_state master1_instance_id)"
    VPC_ID="$(read_state vpc_id)"
    SUBNET_ID="$(read_state subnet_id)"
    SG_ID="$(read_state cluster_sg_id)"
    # TNF's own CIDR is configuration rather than discovered state, unlike the
    # ACM site's, which create-acm-infra.sh records.
    CIDR="${VPC_CIDR}"
    [ -n "${TARGET}" ] \
      || die "no TNF master instance recorded; run 'make tnf' first"
    ;;
  acm)
    TARGET="$(read_acm_state sno_instance_id)"
    SECOND_TARGET=""          # the hub is a single node
    VPC_ID="$(read_acm_state vpc_id)"
    SUBNET_ID="$(read_acm_state subnet_id)"
    SG_ID="$(read_acm_state cluster_sg_id)"
    CIDR="$(read_acm_state vpc_cidr)"
    [ -n "${TARGET}" ] \
      || die "no ACM hub instance recorded; run 'make acm-site' first"
    ;;
  *)
    die "unknown site '${SITE}'; expected tnf or acm"
    ;;
esac

for required in VPC_ID SUBNET_ID SG_ID CIDR; do
  [ -n "${!required}" ] \
    || die "${SITE} site has no ${required} recorded; its infra stage has not run"
done

info "guest ${GUEST}: load balancer at the ${SITE} site, target ${TARGET}${SECOND_TARGET:+ and ${SECOND_TARGET}}"

create_or_update_stack "${STACK}" "${TEMPLATE_DIR}/guest-lb-stack.yaml" \
  "ClusterName=${GUEST}" \
  "VpcId=${VPC_ID}" \
  "SubnetId=${SUBNET_ID}" \
  "TargetInstanceId=${TARGET}" \
  "SecondTargetInstanceId=${SECOND_TARGET}" \
  "ApiNodePort=${GUEST_API_NODEPORT}" \
  "OAuthNodePort=${GUEST_OAUTH_NODEPORT}" \
  "ClusterSecurityGroupId=${SG_ID}" \
  "AllowedApiCidr=${ALLOWED_API_CIDR:-0.0.0.0/0}" \
  "VpcCidr=${CIDR}"

DNS="$(stack_output "${STACK}" LoadBalancerDns)"
[ -n "${DNS}" ] || die "stack ${STACK} produced no DNS name"

# Recorded under the ACM state either way: it is the hcp-guest role that reads
# this back, and it looks in one place regardless of which site hosts the guest.
save_acm_state "${GUEST}_lb_dns" "${DNS}"
green "guest ${GUEST} control plane reachable at ${DNS}"
