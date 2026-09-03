#!/bin/bash
# Publish DNS for the SNO cluster in both hosted zones.
#
# Public zone  -> the bastion's elastic IP, so the ACM console and API are
#                 reachable from outside, with names the SNO's own certificates
#                 match.
# Private zone -> the bastion's private IP, so anything inside the VPC (the TNF
#                 nodes, the guest VMs) reaches it without leaving the network.
#
# api-int is deliberately not published: it is internal to the SNO, which is a
# single node that resolves it for itself.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq
resolve_public_hosted_zone

[ $# -eq 1 ] || die "usage: $0 <sno-cluster-name>"
SNO_NAME="$1"
SNO_DOMAIN="${SNO_NAME}.${BASE_DOMAIN}"

PUBLIC_IP="$(read_state public_address)"
PRIVATE_IP="$(read_state private_address)"
VPC_ID="$(read_state vpc_id)"
[ -n "${VPC_ID}" ] || die "no VPC recorded; run 'make infra' first"

# The TNF cluster's private zone is <cluster>.<base>, so the SNO's names are not
# inside it -- Route53 rejects a record that does not belong to the zone it is
# written to. The SNO therefore gets its own private zone, associated with the
# same VPC, mirroring how TNF's was created.
private_zone_for_sno() {
  local existing
  existing="$(aws route53 list-hosted-zones-by-name --dns-name "${SNO_DOMAIN}." \
    --query "HostedZones[?Name=='${SNO_DOMAIN}.' && Config.PrivateZone].Id" --output text | head -1)"
  if [ -n "${existing}" ] && [ "${existing}" != "None" ]; then
    echo "${existing##*/}"
    return
  fi
  aws route53 create-hosted-zone \
    --name "${SNO_DOMAIN}" \
    --vpc "VPCRegion=${REGION},VPCId=${VPC_ID}" \
    --hosted-zone-config "Comment=SNO ACM hub,PrivateZone=true" \
    --caller-reference "sno-${SNO_DOMAIN}-$(date +%s)" \
    --query 'HostedZone.Id' --output text | sed 's|.*/||'
}

upsert() {  # upsert <zone-id> <name> <ip>
  aws route53 change-resource-record-sets --hosted-zone-id "$1" --change-batch "$(
    jq -n --arg n "$2" --arg ip "$3" '{
      Changes: [{
        Action: "UPSERT",
        ResourceRecordSet: {
          Name: $n, Type: "A", TTL: 60,
          ResourceRecords: [{Value: $ip}]
        }
      }]
    }')" --query 'ChangeInfo.Status' --output text >/dev/null
  echo "  UPSERT $2 -> $3"
}

info "publishing DNS for ${SNO_DOMAIN}"
PRIVATE_ZONE_ID="$(private_zone_for_sno)"
info "private zone for ${SNO_DOMAIN}: ${PRIVATE_ZONE_ID}"

upsert "${PUBLIC_ZONE_ID}"  "api.${SNO_DOMAIN}"     "${PUBLIC_IP}"
upsert "${PUBLIC_ZONE_ID}"  "*.apps.${SNO_DOMAIN}"  "${PUBLIC_IP}"
upsert "${PRIVATE_ZONE_ID}" "api.${SNO_DOMAIN}"     "${PRIVATE_IP}"
upsert "${PRIVATE_ZONE_ID}" "*.apps.${SNO_DOMAIN}"  "${PRIVATE_IP}"

green "DNS published for ${SNO_DOMAIN}"
