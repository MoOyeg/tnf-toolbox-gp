#!/bin/bash
# Remove the ACM site's bootstrap machine once bootstrap-complete has returned.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws
delete_stack "${ACM_CLUSTER_NAME}-bootstrap"
