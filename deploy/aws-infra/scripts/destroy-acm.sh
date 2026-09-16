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

# Any guest control-plane load balancers, before the network stack. They are
# created per guest by create-guest-lb.sh and live in the ACM VPC, so one left
# behind holds a reference to the subnet and the network stack delete fails --
# after the SNO has already gone, which is a bad place to discover it.
#
# Discovered rather than derived from the guest names: the count and prefix can
# change between runs, and a stack this teardown does not know about is exactly
# the one that blocks it.
#
# Two suffixes, because the two profiles publish differently: a hosted cluster's
# entry point is <guest>-cp-lb, an all-VM cluster's is <cluster>-pub-lb. Matching
# only one leaves the other holding an ENI in the subnet, and the network stack
# will not delete under it.
for lb in $(aws cloudformation list-stacks \
      --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE ROLLBACK_COMPLETE \
      --query 'StackSummaries[?ends_with(StackName, `-cp-lb`) || ends_with(StackName, `-pub-lb`)].StackName' \
      --output text 2>/dev/null); do
  delete_stack "${lb}"
done

# Serial from here, and necessarily so: the bastion sits in the security groups
# the services stack owns, and everything sits in the VPC the network stack owns.
delete_stack "${ACM_SERVICES_STACK}"
delete_stack "${ACM_NETWORK_STACK}"

rm -rf "${ACM_STATE_DIR}"
green "ACM site destroyed"
