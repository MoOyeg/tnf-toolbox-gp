#!/bin/bash
# Read-only preflight. Everything it checks is something that otherwise fails
# partway through a deploy, after money has been spent.
set -uo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

PROBLEMS=0
ok()   { printf '  \033[0;32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[0;31mFAIL\033[0m  %s\n' "$*"; PROBLEMS=$((PROBLEMS + 1)); }
warn() { printf '  \033[0;33mWARN\033[0m  %s\n' "$*"; }

echo
echo "TOOLS"
for tool in aws jq ansible-playbook ssh; do
  if command -v "${tool}" >/dev/null 2>&1; then ok "${tool}"; else bad "${tool} not on PATH"; fi
done

echo
echo "CONFIG"
if [ -f "${CONFIG_FILE}" ]; then
  ok "config/instance.env exists"
  load_config
else
  bad "config/instance.env missing (cp config/instance.env.template config/instance.env)"
  echo; echo "${PROBLEMS} problem(s) found."; exit 1
fi

[ -f "${PULL_SECRET_PATH}" ] && ok "pull secret at ${PULL_SECRET_PATH}" \
  || bad "pull secret missing at ${PULL_SECRET_PATH}"
# A warning rather than a failure: 'make keypair' creates this, and it runs
# first in 'make all'. Failing here would mean doctor -- the thing the README
# tells you to run before anything else -- rejects every fresh checkout.
[ -f "${SSH_PRIVATE_KEY}" ] && ok "ssh private key ${SSH_PRIVATE_KEY}" \
  || warn "no ssh private key at ${SSH_PRIVATE_KEY} yet; 'make keypair' creates it"
[ "${BMC_PASSWORD}" = "CHANGE-ME" ] && bad "BMC_PASSWORD is still the placeholder" \
  || ok "BMC_PASSWORD set"
[ "${ALLOWED_SSH_CIDR}" = "0.0.0.0/0" ] && warn "ALLOWED_SSH_CIDR is 0.0.0.0/0" \
  || ok "ALLOWED_SSH_CIDR is ${ALLOWED_SSH_CIDR}"

echo
echo "AWS"
if CALLER="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)"; then
  ok "authenticated as ${CALLER}"
else
  bad "aws sts get-caller-identity failed (check AWS_PROFILE=${AWS_PROFILE:-unset})"
fi

# The cluster domain, its DNS records and its certificates all derive from this.
#
# Probed in a subshell first because resolve_public_hosted_zone calls die() on
# failure, which would take doctor down with it -- and doctor's whole job is to
# report every problem, not stop at the first. On success it is called again in
# this shell, where its exports actually survive.
if ZONE_OUT="$( resolve_public_hosted_zone >/dev/null 2>&1 && echo ok )" \
   && [ "${ZONE_OUT}" = "ok" ]; then
  resolve_public_hosted_zone >/dev/null 2>&1
  ok "public Route53 zone ${BASE_DOMAIN} (${PUBLIC_ZONE_ID})"
  ok "cluster will be at api.${CLUSTER_NAME}.${BASE_DOMAIN}, publicly resolvable"
else
  bad "no usable public Route53 zone: $( resolve_public_hosted_zone 2>&1 | tail -3 )"
fi

[ "${ALLOWED_API_CIDR:-0.0.0.0/0}" = "0.0.0.0/0" ] \
  && warn "ALLOWED_API_CIDR is 0.0.0.0/0 (API and ingress open to the internet)" \
  || ok "ALLOWED_API_CIDR is ${ALLOWED_API_CIDR}"

if aws ec2 describe-key-pairs --key-names "${SSH_KEY_NAME}" >/dev/null 2>&1; then
  ok "EC2 key pair ${SSH_KEY_NAME} exists"
else
  bad "EC2 key pair ${SSH_KEY_NAME} not found in ${REGION}"
fi

# The check most likely to save a wasted deploy: g4dn.metal is not offered in
# every AZ, and the failure otherwise surfaces only at instance launch.
OFFERED="$(aws ec2 describe-instance-type-offerings \
  --location-type availability-zone \
  --filters "Name=instance-type,Values=${MASTER_INSTANCE_TYPE}" \
  --query 'InstanceTypeOfferings[].Location' --output text 2>/dev/null)"
if [ -z "${OFFERED}" ]; then
  bad "${MASTER_INSTANCE_TYPE} is not offered anywhere in ${REGION}"
elif grep -qw "${AVAILABILITY_ZONE}" <<< "${OFFERED}"; then
  ok "${MASTER_INSTANCE_TYPE} offered in ${AVAILABILITY_ZONE}"
else
  bad "${MASTER_INSTANCE_TYPE} not offered in ${AVAILABILITY_ZONE} (try: ${OFFERED})"
fi

# Two bare metal instances is a large ask against a default quota. The relevant
# quota is vCPU-based: g4dn.metal is 96 vCPU, so two need 192.
QUOTA="$(aws service-quotas get-service-quota --service-code ec2 \
  --quota-code L-DB2E81BA --query 'Quota.Value' --output text 2>/dev/null)"
if [ -n "${QUOTA}" ] && [ "${QUOTA}" != "None" ]; then
  if awk -v q="${QUOTA}" 'BEGIN{exit !(q >= 192)}'; then
    ok "G/VT instance vCPU quota is ${QUOTA} (need 192)"
  else
    bad "G/VT instance vCPU quota is ${QUOTA}; two g4dn.metal need 192"
  fi
else
  warn "could not read the G/VT vCPU quota; confirm it is at least 192"
fi

echo
if [ "${PROBLEMS}" -eq 0 ]; then
  green "no problems found"
else
  red "${PROBLEMS} problem(s) found"
fi
exit "${PROBLEMS}"
