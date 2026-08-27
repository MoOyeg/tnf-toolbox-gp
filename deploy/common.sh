#!/bin/bash
# Shared helpers. Source, do not execute.
# shellcheck disable=SC2034  # several vars are consumed by sourcing scripts

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${REPO_ROOT}/config/instance.env"
STATE_DIR="${REPO_ROOT}/deploy/aws-infra/instance-data"
TEMPLATE_DIR="${REPO_ROOT}/deploy/aws-infra/templates"

red()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
info()  { printf '\033[0;36m==> %s\033[0m\n' "$*"; }
die()   { red "ERROR: $*"; exit 1; }

load_config() {
  [ -f "${CONFIG_FILE}" ] || die "missing ${CONFIG_FILE} (cp config/instance.env.template config/instance.env)"
  # shellcheck source=/dev/null
  source "${CONFIG_FILE}"

  : "${CLUSTER_NAME:?CLUSTER_NAME must be set in instance.env}"
  : "${REGION:?REGION must be set in instance.env}"
  : "${AVAILABILITY_ZONE:?AVAILABILITY_ZONE must be set in instance.env}"
  : "${SSH_KEY_NAME:?SSH_KEY_NAME must be set in instance.env}"

  # Sensible fallbacks so an older instance.env still works.
  PULL_SECRET_PATH="${PULL_SECRET_PATH:-${REPO_ROOT}/config/pull-secret.json}"
  export PULL_SECRET_PATH

  export AWS_DEFAULT_REGION="${REGION}"
  NETWORK_STACK="${CLUSTER_NAME}-network"
  SERVICES_STACK="${CLUSTER_NAME}-services"
  COMPUTE_STACK="${CLUSTER_NAME}-compute"
  BOOTSTRAP_STACK="${CLUSTER_NAME}-bootstrap"
  mkdir -p "${STATE_DIR}"
}

require_tools() {
  local missing=()
  for tool in "$@"; do
    command -v "${tool}" >/dev/null 2>&1 || missing+=("${tool}")
  done
  [ ${#missing[@]} -eq 0 ] || die "missing required tools: ${missing[*]}"
}

stack_exists() {
  aws cloudformation describe-stacks --stack-name "$1" >/dev/null 2>&1
}

stack_status() {
  aws cloudformation describe-stacks --stack-name "$1" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST"
}

# stack_output <stack> <output-key>
stack_output() {
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
}

# create_or_update_stack <stack> <template> [ParameterKey=..,ParameterValue=.. ...]
create_or_update_stack() {
  local stack="$1" template="$2"
  shift 2

  local args=(
    --stack-name "${stack}"
    --template-body "file://${template}"
    --capabilities CAPABILITY_IAM
    --tags "Key=tnf-cluster,Value=${CLUSTER_NAME}"
  )
  [ $# -gt 0 ] && args+=(--parameters "$@")

  if stack_exists "${stack}"; then
    info "updating stack ${stack}"
    # A no-op update is reported as a failure by the CLI; that is not an error.
    if ! aws cloudformation update-stack "${args[@]}" 2>&1 | tee /dev/stderr \
         | grep -q 'No updates are to be performed'; then
      aws cloudformation wait stack-update-complete --stack-name "${stack}"
    fi
  else
    info "creating stack ${stack}"
    aws cloudformation create-stack "${args[@]}" >/dev/null
    aws cloudformation wait stack-create-complete --stack-name "${stack}" || {
      red "stack ${stack} failed; most recent failure events:"
      aws cloudformation describe-stack-events --stack-name "${stack}" \
        --query 'StackEvents[?contains(ResourceStatus, `FAILED`)].[LogicalResourceId,ResourceStatusReason]' \
        --output table >&2
      die "stack ${stack} did not create"
    }
  fi
  green "stack ${stack}: $(stack_status "${stack}")"
}

delete_stack() {
  local stack="$1"
  stack_exists "${stack}" || { info "stack ${stack} does not exist; nothing to delete"; return 0; }
  info "deleting stack ${stack}"
  aws cloudformation delete-stack --stack-name "${stack}"
  aws cloudformation wait stack-delete-complete --stack-name "${stack}"
  green "stack ${stack} deleted"
}

save_state() {
  printf '%s' "$2" > "${STATE_DIR}/$1"
}

read_state() {
  cat "${STATE_DIR}/$1" 2>/dev/null
}

# The AMI is resolved from the installer's own CoreOS stream metadata rather
# than an SSM alias, so the boot image always matches the release being
# installed.
rhcos_ami() {
  local installer="${1:-openshift-install}"
  "${installer}" coreos print-stream-json \
    | jq -r ".architectures.x86_64.images.aws.regions[\"${REGION}\"].image"
}

bastion_ssh() {
  local ip
  ip="$(read_state public_address)"
  [ -n "${ip}" ] || die "no bastion address recorded; run 'make infra' first"
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -i "${SSH_PRIVATE_KEY}" "ec2-user@${ip}" "$@"
}
