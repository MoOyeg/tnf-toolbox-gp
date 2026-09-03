#!/bin/bash
# SSH to the bastion. Any arguments are run as a remote command.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config

# --acm targets the ACM site's bastion instead of TNF's.
if [ "${1:-}" = "--acm" ]; then
  shift
  ip="$(read_acm_state public_address)"
  [ -n "${ip}" ] || die "no ACM bastion recorded; run 'make acm-infra' first"
  exec ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -i "${SSH_PRIVATE_KEY}" "ec2-user@${ip}" "$@"
fi

bastion_ssh "$@"
