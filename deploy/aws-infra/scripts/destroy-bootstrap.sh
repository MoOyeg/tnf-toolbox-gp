#!/bin/bash
# Remove the bootstrap node once bootstrap-complete has returned.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws
delete_stack "${BOOTSTRAP_STACK}"
rm -f "${STATE_DIR}/bootstrap_instance_id"
