#!/bin/bash
# Tear down every stack, newest dependency first.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws

cat >&2 <<WARN

This deletes the whole environment for cluster '${CLUSTER_NAME}' in ${REGION}:

  - ${BOOTSTRAP_STACK}
  - ${COMPUTE_STACK}   (both g4dn.metal nodes AND their EBS data volumes)
  - ${SERVICES_STACK}  (bastion, elastic IP, DNS records)
  - ${NETWORK_STACK}   (VPC, subnet, hosted zone)

The data volumes hold the guest clusters' hosted control-plane etcd. Deleting
them destroys those clusters' state.

WARN
read -r -p "Type the cluster name to confirm: " confirm
[ "${confirm}" = "${CLUSTER_NAME}" ] || die "confirmation did not match; nothing deleted"

# Created by the SNO stage with the AWS CLI, so no stack owns it.
info "removing any private hosted zone created for a SNO cluster"
for zone_id in $(aws route53 list-hosted-zones \
      --query "HostedZones[?Config.PrivateZone && contains(Name, '${BASE_DOMAIN:-tnf}')].Id" \
      --output text 2>/dev/null | sed 's|[^ ]*/||g'); do
  name="$(aws route53 get-hosted-zone --id "${zone_id}" --query 'HostedZone.Name' --output text 2>/dev/null || true)"
  case "${name}" in
    "${CLUSTER_NAME}."*) continue ;;   # the TNF zone belongs to CloudFormation
  esac
  info "  deleting records and zone ${name} (${zone_id})"
  aws route53 list-resource-record-sets --hosted-zone-id "${zone_id}" \
    --query "ResourceRecordSets[?Type=='A']" --output json 2>/dev/null \
    | jq -c '.[]' | while read -r rr; do
        aws route53 change-resource-record-sets --hosted-zone-id "${zone_id}" \
          --change-batch "$(jq -n --argjson r "${rr}" '{Changes:[{Action:"DELETE",ResourceRecordSet:$r}]}')" \
          >/dev/null 2>&1 || true
      done
  aws route53 delete-hosted-zone --id "${zone_id}" >/dev/null 2>&1 || true
done

delete_stack "${BOOTSTRAP_STACK}"
delete_stack "${COMPUTE_STACK}"
delete_stack "${SERVICES_STACK}"
delete_stack "${NETWORK_STACK}"

rm -rf "${STATE_DIR}"
green "environment ${CLUSTER_NAME} destroyed"
