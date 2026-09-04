#!/bin/bash
# Run a playbook with config/instance.env and the recorded stack state folded
# in as extra vars.
#
# This exists so the shell config and the Ansible config cannot drift: there is
# one source of truth (config/instance.env plus instance-data/), and playbooks
# never re-derive a value that a script already decided.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config

# BASE_DOMAIN is normally blank in instance.env and discovered from the account's
# public Route53 zone. The install-config needs the resolved value, so resolve it
# here rather than letting an empty string reach the template.
resolve_public_hosted_zone

[ $# -ge 1 ] || die "usage: $0 <playbook.yml> [ansible args...]"
PLAYBOOK="$1"
shift

PLAYBOOK_DIR="${REPO_ROOT}/deploy/openshift-clusters"
[ -f "${PLAYBOOK_DIR}/${PLAYBOOK}" ] || die "no such playbook: ${PLAYBOOK}"
[ -f "${PLAYBOOK_DIR}/inventory.ini" ] || die "no inventory; run 'make infra' or 'make inventory'"

[ -f "${PULL_SECRET_PATH}" ] || die "pull secret not found at ${PULL_SECRET_PATH}"
[ -f "${SSH_PUBLIC_KEY}" ] || die "ssh public key not found at ${SSH_PUBLIC_KEY}"

VARS_FILE="${STATE_DIR}/ansible-vars.json"

# jq builds this rather than a heredoc so that values containing quotes (a BMC
# password, most likely) cannot break out of the JSON.
jq -n \
  --arg repo_root            "${REPO_ROOT}" \
  --arg cluster_name         "${CLUSTER_NAME}" \
  --arg base_domain          "${BASE_DOMAIN}" \
  --arg cluster_domain       "$(read_state cluster_domain)" \
  --arg ocp_version          "${OCP_VERSION}" \
  --arg feature_set          "${FEATURE_SET:-}" \
  --arg region               "${REGION}" \
  --arg availability_zone    "${AVAILABILITY_ZONE}" \
  --arg master_instance_type "${MASTER_INSTANCE_TYPE}" \
  --arg subnet_cidr          "${SUBNET_CIDR}" \
  --arg vpc_cidr             "${VPC_CIDR}" \
  --arg bastion_private_ip   "${BASTION_PRIVATE_IP}" \
  --arg bastion_public_ip    "$(read_state public_address)" \
  --arg bootstrap_private_ip "${BOOTSTRAP_PRIVATE_IP}" \
  --arg master0_private_ip   "${MASTER0_PRIVATE_IP}" \
  --arg master1_private_ip   "${MASTER1_PRIVATE_IP}" \
  --arg bmc_username         "${BMC_USERNAME}" \
  --arg bmc_password         "${BMC_PASSWORD}" \
  --arg pull_secret          "$(jq -c . "${PULL_SECRET_PATH}")" \
  --arg ssh_public_key       "$(cat "${SSH_PUBLIC_KEY}")" \
  --arg fencing_base_url     "$(read_state fencing_base_url)" \
  --arg ignition_base_url    "$(read_state ignition_base_url)" \
  --arg master0_data_volume  "$(read_state master0_data_volume)" \
  --arg master1_data_volume  "$(read_state master1_data_volume)" \
  --arg acm_cluster_name     "${ACM_CLUSTER_NAME}" \
  --arg acm_subnet_cidr      "${ACM_SUBNET_CIDR}" \
  --arg acm_vpc_cidr         "${ACM_VPC_CIDR}" \
  --arg acm_sno_private_ip   "${ACM_SNO_PRIVATE_IP}" \
  --arg acm_bootstrap_private_ip "${ACM_BOOTSTRAP_PRIVATE_IP:-10.1.0.9}" \
  --arg acm_bastion_private_ip   "${ACM_BASTION_PRIVATE_IP}" \
  --arg acm_bastion_public_ip    "$(read_acm_state public_address)" \
  --arg acm_ignition_base_url    "$(read_acm_state ignition_base_url)" \
  --arg acm_sno_data_volume      "$(read_acm_state sno_data_volume)" \
  --arg guest_cluster_prefix "${GUEST_CLUSTER_PREFIX}" \
  --argjson guest_cluster_count     "${GUEST_CLUSTER_COUNT}" \
  --argjson guest_nodepool_replicas "${GUEST_NODEPOOL_REPLICAS}" \
  --argjson guest_gpus_per_node     "${GUEST_GPUS_PER_NODE}" \
  --argjson guest_vm_cores          "${GUEST_VM_CORES}" \
  --arg guest_vm_memory      "${GUEST_VM_MEMORY}" \
  --arg guest_vm_root_disk   "${GUEST_VM_ROOT_DISK}" \
  '$ARGS.named' > "${VARS_FILE}"
chmod 600 "${VARS_FILE}"

cd "${PLAYBOOK_DIR}"
mkdir -p "${REPO_ROOT}/logs"
exec ansible-playbook -i inventory.ini -e "@${VARS_FILE}" "${PLAYBOOK}" "$@"
