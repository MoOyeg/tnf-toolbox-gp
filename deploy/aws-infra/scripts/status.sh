#!/bin/bash
# One screen covering stacks, instance power state, fencing, and the cluster.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws

echo
info "Cluster ${CLUSTER_NAME} (${REGION} / ${AVAILABILITY_ZONE})"

echo
echo "STACKS"
for stack in "${NETWORK_STACK}" "${SERVICES_STACK}" "${COMPUTE_STACK}" "${BOOTSTRAP_STACK}"; do
  printf '  %-28s %s\n' "${stack}" "$(stack_status "${stack}")"
done

echo
echo "INSTANCES"
aws ec2 describe-instances \
  --filters "Name=tag:tnf-cluster,Values=${CLUSTER_NAME}" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,InstanceId,InstanceType,State.Name,PrivateIpAddress]' \
  --output table 2>/dev/null || echo "  (none)"

BASTION_IP="$(read_state public_address)"
if [ -n "${BASTION_IP}" ]; then
  echo
  echo "BASTION SERVICES  (${BASTION_IP})"
  # These run over SSH rather than from here: haproxy and the shim bind private
  # addresses, and the API is only resolvable inside the VPC.
  bastion_ssh bash -s <<'REMOTE' 2>/dev/null || echo "  bastion unreachable"
    printf '  %-22s %s\n' "redfish-ec2" \
      "$(curl -sk --max-time 5 https://localhost:8000/healthz -o /dev/null -w '%{http_code}' || echo unreachable)"
    printf '  %-22s %s\n' "haproxy" \
      "$(systemctl is-active haproxy 2>/dev/null || echo inactive)"
    printf '  %-22s %s\n' "ignition server" \
      "$(systemctl is-active tnf-ignition 2>/dev/null || echo inactive)"
    kubeconfig=$(find ~/clusters -maxdepth 3 -name kubeconfig -path '*/auth/*' 2>/dev/null | head -1)
    if [ -n "$kubeconfig" ]; then
      export KUBECONFIG="$kubeconfig"
      printf '  %-22s %s\n' "nodes ready" \
        "$(oc get nodes --no-headers 2>/dev/null | grep -c ' Ready' || echo 0)/2"
      printf '  %-22s %s\n' "degraded operators" \
        "$(oc get co --no-headers 2>/dev/null | awk '$5=="True"' | wc -l)"
    fi
REMOTE
fi
echo
