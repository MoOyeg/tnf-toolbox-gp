#!/bin/bash
# DNS for a guest cluster whose nodes are virtual machines on the infra cluster.
#
# Usage: create-guest-dns.sh <guest-name> [<node-ip> ...]
#
# With no addresses it creates the zone and nothing in it. That is the first
# call, made before the nodes exist -- see "Why the zone comes first" below.
#
# With userManagedNetworking the installer builds no VIPs and creates no DNS, so
# the assisted installer's own validations fail before anything is installed:
#
#   Couldn't resolve domain name api.<guest>.<domain> on the host
#
# Its own zone rather than records in the infra cluster's, for the reason
# docs/architecture.md gives for the SNO: the infra zone is
# <cluster>.<base-domain>, api.<guest>.<base-domain> is not inside it, and
# Route53 rejects a record that does not belong to its zone.
#
# Private, and associated with the TNF VPC. The nodes resolve through the infra
# cluster's DNS, which forwards to the VPC resolver -- so a zone attached to
# that VPC is what they can see. Nothing outside needs these names: they answer
# with addresses on a user-defined network that only the infra cluster can route.
#
# Round-robin A records over every node, which is the ordinary shape for a
# cluster with no VIP: clients retry, and during bootstrap only one node answers
# anyway.
#
# Re-runnable. UPSERT with the same addresses is a no-op, and after a rebuild it
# corrects them rather than needing the zone torn down first.
#
# Why the zone comes first
#
# The nodes query these names the moment they register, and their addresses --
# which the records need -- are only known after that. A name looked up before
# its zone exists falls through to the account's public zone and comes back
# NXDOMAIN, and Route 53 Resolver caches that answer VPC-wide for the public
# zone's negative TTL: min(SOA TTL, SOA minimum) = min(900, 86400), fifteen
# minutes. Publishing the records does not clear it. Measured on vcp-1: records
# correct, a never-queried *.apps name resolving, api and api-int still
# NXDOMAIN from every node, and the install sitting 'insufficient' until the
# cache ran out.
#
# So the zone is created empty, before the nodes boot, and its own SOA carries a
# 60-second negative TTL. An early lookup then misses inside *this* zone and is
# forgotten within a minute of the records appearing.
NEGATIVE_TTL=60
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq
resolve_public_hosted_zone

GUEST="${1:?usage: $0 <guest-name> [<node-ip> ...]}"
shift

DOMAIN="${GUEST}.${BASE_DOMAIN}"
VPC="$(read_state vpc_id)"
[ -n "${VPC}" ] || die "no TNF VPC recorded; run 'make infra' first"

# The zone may already exist from an earlier run. list-hosted-zones-by-name is
# a prefix match, so the name is compared exactly rather than trusted.
ZONE_ID="$(aws route53 list-hosted-zones-by-name --dns-name "${DOMAIN}" \
  --query "HostedZones[?Name=='${DOMAIN}.'].Id" --output text 2>/dev/null | head -1)"
ZONE_ID="${ZONE_ID##*/}"

if [ -z "${ZONE_ID}" ]; then
  info "creating private zone ${DOMAIN} in ${VPC}"
  ZONE_ID="$(aws route53 create-hosted-zone \
    --name "${DOMAIN}" \
    --vpc "VPCRegion=${REGION},VPCId=${VPC}" \
    --hosted-zone-config "Comment=guest cluster ${GUEST},PrivateZone=true" \
    --caller-reference "${GUEST}-$(date +%s)" \
    --query 'HostedZone.Id' --output text)"
  ZONE_ID="${ZONE_ID##*/}"
else
  info "private zone ${DOMAIN} already exists (${ZONE_ID})"
  # Idempotent in the same fire-and-forget shape create-peering.sh uses: an
  # association that is already there is not an error worth stopping for.
  aws route53 associate-vpc-with-hosted-zone \
    --hosted-zone-id "${ZONE_ID}" \
    --vpc "VPCRegion=${REGION},VPCId=${VPC}" \
    --query 'ChangeInfo.Status' --output text >/dev/null 2>&1 || true
fi

# Both the SOA's own TTL and its minimum field, because a resolver caches a
# negative answer for whichever is smaller. Rewritten on every run: it costs one
# call, and a zone created by an older version of this script has the default
# fifteen minutes.
SOA="$(aws route53 list-resource-record-sets --hosted-zone-id "${ZONE_ID}" \
  --query "ResourceRecordSets[?Type=='SOA'] | [0].ResourceRecords[0].Value" --output text)"
SOA_SHORT="$(awk -v t="${NEGATIVE_TTL}" '{$NF = t; print}' <<<"${SOA}")"
if [ "${SOA}" != "${SOA_SHORT}" ]; then
  aws route53 change-resource-record-sets --hosted-zone-id "${ZONE_ID}" \
    --change-batch "$(jq -n --arg d "${DOMAIN}." --arg v "${SOA_SHORT}" --argjson t "${NEGATIVE_TTL}" \
      '{Changes: [{Action: "UPSERT", ResourceRecordSet:
         {Name: $d, Type: "SOA", TTL: $t, ResourceRecords: [{Value: $v}]}}]}')" \
    --query 'ChangeInfo.Status' --output text >/dev/null
fi
save_state "${GUEST}_hosted_zone_id" "${ZONE_ID}"

if [ "$#" -eq 0 ]; then
  green "${GUEST} DNS: zone ${DOMAIN} ready, negative answers cached ${NEGATIVE_TTL}s; records follow once the nodes have addresses"
  exit 0
fi

# api and api-int carry the same answers. They are separate names because
# OpenShift treats them as separate endpoints, not because they differ here:
# there is no external load balancer for one of them to point at.
CHANGES="$(jq -n --argjson ips "$(printf '%s\n' "$@" | jq -R . | jq -s .)" \
  --arg domain "${DOMAIN}" '
  ["api", "api-int", "*.apps"] as $names
  | {Changes: [ $names[] as $n | {
      Action: "UPSERT",
      ResourceRecordSet: {
        Name: ($n + "." + $domain),
        Type: "A",
        TTL: 60,
        ResourceRecords: [ $ips[] | {Value: .} ]
      }
    } ]}')"

aws route53 change-resource-record-sets --hosted-zone-id "${ZONE_ID}" \
  --change-batch "${CHANGES}" --query 'ChangeInfo.Status' --output text >/dev/null

green "${GUEST} DNS: api, api-int and *.apps.${DOMAIN} -> $*"
