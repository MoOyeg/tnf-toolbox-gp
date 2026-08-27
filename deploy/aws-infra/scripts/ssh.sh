#!/bin/bash
# SSH to the bastion. Any arguments are run as a remote command.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
bastion_ssh "$@"
