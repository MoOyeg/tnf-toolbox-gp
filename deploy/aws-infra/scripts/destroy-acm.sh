#!/bin/bash
# Tear down the ACM site and the peering that joins it to TNF.
#
# Peering first: the connection references both VPCs, so neither network stack
# can be deleted while it exists.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws

delete_stack "${PEERING_STACK}"
delete_stack "${ACM_CLUSTER_NAME}-bootstrap"
delete_stack "${ACM_SNO_STACK}"
delete_stack "${ACM_SERVICES_STACK}"
delete_stack "${ACM_NETWORK_STACK}"

rm -rf "${ACM_STATE_DIR}"
green "ACM site destroyed"
