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

# The two instance stacks together: the SNO is bare metal and takes about twenty
# minutes to terminate, and the bootstrap has no relationship to it.
delete_stacks_parallel "${ACM_CLUSTER_NAME}-bootstrap" "${ACM_SNO_STACK}"

# Serial from here, and necessarily so: the bastion sits in the security groups
# the services stack owns, and everything sits in the VPC the network stack owns.
delete_stack "${ACM_SERVICES_STACK}"
delete_stack "${ACM_NETWORK_STACK}"

rm -rf "${ACM_STATE_DIR}"
green "ACM site destroyed"
