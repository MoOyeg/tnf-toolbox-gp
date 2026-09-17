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
  - the ACM site (${ACM_NETWORK_STACK}, ${ACM_SERVICES_STACK}, ${ACM_SNO_STACK})
    and the peering connection joining the two sites

The data volumes hold the guest clusters' hosted control-plane etcd. Deleting
them destroys those clusters' state.

WARN
read -r -p "Type the cluster name to confirm: " confirm
[ "${confirm}" = "${CLUSTER_NAME}" ] || die "confirmation did not match; nothing deleted"

# Resolve the account's public zone first, because BASE_DOMAIN is deliberately
# blank in config -- it is discovered per account -- and the match below is on
# the domain. Left unresolved it fell back to the literal "tnf", which matched
# the cluster's own zone and nothing else, so a guest cluster's zone survived
# the teardown and the next run inherited stale records for addresses that no
# longer existed.
resolve_public_hosted_zone

# Created by the SNO and guest stages with the AWS CLI, so no stack owns them.
info "removing any private hosted zone created for a cluster"
for zone_id in $(aws route53 list-hosted-zones \
      --query "HostedZones[?Config.PrivateZone && contains(Name, '${BASE_DOMAIN}')].Id" \
      --output text 2>/dev/null | sed 's|[^ ]*/||g'); do
  name="$(aws route53 get-hosted-zone --id "${zone_id}" --query 'HostedZone.Name' --output text 2>/dev/null || true)"
  case "${name}" in
    "${CLUSTER_NAME}."*) continue ;;   # the TNF zone belongs to CloudFormation
  esac
  info "  deleting records and zone ${name} (${zone_id})"
  aws route53 list-resource-record-sets --hosted-zone-id "${zone_id}" \
    --query "ResourceRecordSets[?Type!='NS' && Type!='SOA']" --output json 2>/dev/null \
    | jq -c '.[]' | while read -r rr; do
        aws route53 change-resource-record-sets --hosted-zone-id "${zone_id}" \
          --change-batch "$(jq -n --argjson r "${rr}" '{Changes:[{Action:"DELETE",ResourceRecordSet:$r}]}')" \
          >/dev/null 2>&1 || true
      done
  aws route53 delete-hosted-zone --id "${zone_id}" >/dev/null 2>&1 || true
done

# The public names a guest's load balancer publishes. They live in the account's
# own zone, which this must not delete -- only the records it added. Left behind
# they resolve to a load balancer that no longer exists, which is worse than not
# resolving at all: a browser reports a connection failure rather than an
# unknown host, and the address looks live.
info "removing any public records published for a guest cluster"
aws route53 list-resource-record-sets --hosted-zone-id "${PUBLIC_ZONE_ID}" \
  --query "ResourceRecordSets[?Type=='CNAME']" --output json 2>/dev/null \
  | jq -c --arg d ".${BASE_DOMAIN}." '.[] | select(.Name | endswith($d))' \
  | while read -r rr; do
      aws route53 change-resource-record-sets --hosted-zone-id "${PUBLIC_ZONE_ID}" \
        --change-batch "$(jq -n --argjson r "${rr}" '{Changes:[{Action:"DELETE",ResourceRecordSet:$r}]}')" \
        >/dev/null 2>&1 || true
    done

# The two sites at once, because nothing about one instance's termination waits
# on the other's -- and terminating a bare-metal instance takes about twenty
# minutes. Run one site after the other and the same work costs three quarters
# of an hour of waiting; the measured run that prompted this had the ACM SNO
# start terminating at 19:57 and the TNF masters not until 20:21.
#
# Ordering that does still matter is kept: the peering connection references the
# TNF VPC, so destroy-acm.sh removes it before either network stack goes, and
# TNF's own services and network stacks are held back until everything above has
# finished.
declare -a teardown_pids=() teardown_labels=()

if stack_exists "${PEERING_STACK}" || stack_exists "${ACM_NETWORK_STACK}"; then
  info "removing the ACM site and the peering connection"
  "$(dirname "${BASH_SOURCE[0]}")/destroy-acm.sh" &
  teardown_pids+=("$!")
  teardown_labels+=("the ACM site")
fi

delete_stack "${BOOTSTRAP_STACK}" &
teardown_pids+=("$!")
teardown_labels+=("${BOOTSTRAP_STACK}")

delete_stack "${COMPUTE_STACK}" &
teardown_pids+=("$!")
teardown_labels+=("${COMPUTE_STACK}")

teardown_failed=0
for i in "${!teardown_pids[@]}"; do
  if ! wait "${teardown_pids[$i]}"; then
    red "${teardown_labels[$i]} failed to delete"
    teardown_failed=1
  fi
done
[ "${teardown_failed}" -eq 0 ] \
  || die "teardown did not finish; the errors above say which part, and re-running is safe"

# Only now. The TNF network stack cannot go while the peering exists, and the
# services stack owns security groups the instances were still in.
delete_stack "${SERVICES_STACK}"
delete_stack "${NETWORK_STACK}"

rm -rf "${STATE_DIR}"
green "environment ${CLUSTER_NAME} destroyed"
