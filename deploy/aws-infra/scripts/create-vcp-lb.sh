#!/bin/bash
# A public entry point for an all-VM cluster's API and console.
#
# Usage: create-vcp-lb.sh <cluster-name>
#
# An all-VM cluster's nodes sit on a user-defined network that only the infra
# cluster can route, and its DNS zone is private to the infra cluster's VPC. So
# the cluster installs, reports healthy, and is reachable from nowhere -- not a
# workstation, not the ACM bastion. expose.yml publishes its API and ingress as
# NodePort Services on the infra cluster; this fronts those with a public load
# balancer and publishes the names in the account's public zone.
#
# Run after expose.yml, not before: the NodePorts are allocated by the API
# server rather than pinned, so they have to be read back rather than assumed.
# That is the difference from the hosted-cluster load balancer, whose ports are
# fixed in config precisely so its stack can be built first.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws oc jq

CLUSTER="${1:?usage: $0 <cluster-name>}"
STACK="${CLUSTER}-pub-lb"
NS="${VCP_VM_NAMESPACE:-acm-vcp-vms}"
KUBECONFIG_PATH="${STATE_DIR}/${ACM_CLUSTER_NAME}/infra-kubeconfig"
[ -f "${KUBECONFIG_PATH}" ] || KUBECONFIG_PATH="${REPO_ROOT}/deploy/clusters/${CLUSTER_NAME}/kubeconfig"
[ -f "${KUBECONFIG_PATH}" ] || die "no kubeconfig for the infra cluster; run 'make kubeconfig'"

read_port() {
  oc --kubeconfig "${KUBECONFIG_PATH}" get svc "$1" -n "${NS}" \
    -o jsonpath="{.spec.ports[?(@.name=='$2')].nodePort}" 2>/dev/null
}
API_PORT="$(read_port "${CLUSTER}-api" api)"
INGRESS_PORT="$(read_port "${CLUSTER}-ingress" https)"
[ -n "${API_PORT}" ] && [ -n "${INGRESS_PORT}" ] \
  || die "${CLUSTER}'s NodePort Services are missing; 'make sites' publishes them once its machines have registered"

info "${CLUSTER}: api on NodePort ${API_PORT}, ingress on ${INGRESS_PORT}"

create_or_update_stack "${STACK}" "${TEMPLATE_DIR}/vcp-lb-stack.yaml" \
  "ClusterName=${CLUSTER}" \
  "VpcId=$(read_state vpc_id)" \
  "SubnetId=$(read_state subnet_id)" \
  "TargetInstanceId=$(read_state master0_instance_id)" \
  "SecondTargetInstanceId=$(read_state master1_instance_id)" \
  "ApiNodePort=${API_PORT}" \
  "IngressHttpsNodePort=${INGRESS_PORT}" \
  "ClusterSecurityGroupId=$(read_state cluster_sg_id)" \
  "AllowedApiCidr=${ALLOWED_API_CIDR:-0.0.0.0/0}" \
  "VpcCidr=${VPC_CIDR}"

DNS="$(stack_output "${STACK}" LoadBalancerDns)"
[ -n "${DNS}" ] || die "stack ${STACK} produced no DNS name"

# Public records, alongside the private zone rather than instead of it. Inside
# the infra VPC the private zone wins and answers with the nodes' own addresses,
# which is what api-int must resolve to for the cluster to work at all. Outside,
# these answer with the load balancer.
resolve_public_hosted_zone
info "publishing ${CLUSTER}'s public names in ${BASE_DOMAIN}"
for name in "api.${CLUSTER}" "*.apps.${CLUSTER}"; do
  aws route53 change-resource-record-sets --hosted-zone-id "${PUBLIC_ZONE_ID}" \
    --change-batch "$(jq -n --arg n "${name}.${BASE_DOMAIN}" --arg v "${DNS}" \
      '{Changes:[{Action:"UPSERT",ResourceRecordSet:{Name:$n,Type:"CNAME",TTL:60,
        ResourceRecords:[{Value:$v}]}}]}')" >/dev/null
done

green "${CLUSTER} reachable at:"
green "  API     https://api.${CLUSTER}.${BASE_DOMAIN}:6443"
green "  Console https://console-openshift-console.apps.${CLUSTER}.${BASE_DOMAIN}"
