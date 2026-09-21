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

  # Pinned NodePorts for a guest's control plane, so the load balancer in front
  # of them can be built before the guest exists.
  GUEST_API_NODEPORT="${GUEST_API_NODEPORT:-30443}"
  GUEST_OAUTH_NODEPORT="${GUEST_OAUTH_NODEPORT:-30444}"
  export GUEST_API_NODEPORT GUEST_OAUTH_NODEPORT

  export AWS_DEFAULT_REGION="${REGION}"
  NETWORK_STACK="${CLUSTER_NAME}-network"
  SERVICES_STACK="${CLUSTER_NAME}-services"
  COMPUTE_STACK="${CLUSTER_NAME}-compute"
  BOOTSTRAP_STACK="${CLUSTER_NAME}-bootstrap"

  # The ACM site. Its own VPC, bastion and single bare-metal node, joined to the
  # TNF site by peering. The stacks reuse the same templates as TNF with
  # different parameters -- the two sites differ in addressing and size, not in
  # shape.
  ACM_CLUSTER_NAME="${ACM_CLUSTER_NAME:-acm}"
  ACM_NETWORK_STACK="${ACM_CLUSTER_NAME}-network"
  ACM_SERVICES_STACK="${ACM_CLUSTER_NAME}-services"
  ACM_SNO_STACK="${ACM_CLUSTER_NAME}-sno"
  PEERING_STACK="${ACM_CLUSTER_NAME}-to-${CLUSTER_NAME}-peering"
  ACM_STATE_DIR="${STATE_DIR}/${ACM_CLUSTER_NAME}"
  mkdir -p "${ACM_STATE_DIR}"
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

# cidr_contains <cidr> <ipv4-address> -- is the address inside the range?
#
# For the one question worth asking about ALLOWED_SSH_CIDR: does it still cover
# the machine running this? That address is written into both bastions'
# security groups, so an ISP or VPN change locks you out of the environment you
# are building. The stage that notices is 'make peering', whose check SSHes to a
# bastion and reports the failure as unreachable peering -- which sends you to
# the route tables, where nothing is wrong.
#
# Arithmetic rather than python or ipcalc: doctor is meant to run on a bare
# checkout with nothing installed beyond the tools it checks for.
ipv4_to_int() {
  local ip="$1" o1 o2 o3 o4 octet
  IFS=. read -r o1 o2 o3 o4 <<< "${ip}"
  for octet in "${o1:-}" "${o2:-}" "${o3:-}" "${o4:-}"; do
    [[ "${octet}" =~ ^[0-9]{1,3}$ ]] && [ "${octet}" -le 255 ] || return 1
  done
  echo $(( (o1 << 24) | (o2 << 16) | (o3 << 8) | o4 ))
}

cidr_contains() {
  local cidr="$1" ip="$2" network bits mask net_int ip_int
  network="${cidr%%/*}"
  case "${cidr}" in
    */*) bits="${cidr##*/}" ;;
    *)   bits=32 ;;
  esac
  [[ "${bits}" =~ ^[0-9]{1,2}$ ]] && [ "${bits}" -le 32 ] || return 1
  net_int="$(ipv4_to_int "${network}")" || return 1
  ip_int="$(ipv4_to_int "${ip}")" || return 1
  if [ "${bits}" -eq 0 ]; then
    mask=0
  else
    mask=$(( (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
  fi
  [ $(( net_int & mask )) -eq $(( ip_int & mask )) ]
}

# create_or_update_stack <stack> <template> [Key=Value ...]
#
# Parameters are passed as plain Key=Value and converted to a JSON file here.
# The CLI's shorthand syntax (ParameterKey=..,ParameterValue=..) cannot carry a
# value containing '=', ',' or a quote, which means it cannot carry an ignition
# config at all -- it fails with "Expected: '=', received: '\"'". Building the
# JSON with jq also removes every quoting question about the BMC password.
build_stack_parameters() {
  local out="$1"
  shift
  local pair key value
  : > "${out}.entries"
  for pair in "$@"; do
    key="${pair%%=*}"
    # Split on the first '=' only; values may legitimately contain more.
    value="${pair#*=}"
    jq -n --arg k "${key}" --arg v "${value}" \
      '{ParameterKey: $k, ParameterValue: $v}' >> "${out}.entries"
  done
  jq -s '.' < "${out}.entries" > "${out}"
  rm -f "${out}.entries"
}

create_or_update_stack() {
  local stack="$1" template="$2"
  shift 2

  local args=(
    --stack-name "${stack}"
    --template-body "file://${template}"
    --capabilities CAPABILITY_IAM
    --tags "Key=tnf-cluster,Value=${CLUSTER_NAME}"
  )

  local params_file=""
  if [ $# -gt 0 ]; then
    params_file="$(mktemp -t "${stack}-params-XXXXXX.json")"
    build_stack_parameters "${params_file}" "$@"
    args+=(--parameters "file://${params_file}")
  fi

  local current_status
  current_status="$(stack_status "${stack}")"

  # Settle any operation already in flight. This polls rather than chaining
  # `aws cloudformation wait` calls: waiting for stack-delete-complete on a
  # stack that is merely rolled back blocks for an hour on a deletion nobody
  # asked for. Rolling back a pair of g4dn.metal takes 10-20 minutes on its
  # own, so the ceiling is generous.
  local waited=0 announced=false
  while [[ "${current_status}" == *_IN_PROGRESS ]] \
        && [ "${current_status}" != "REVIEW_IN_PROGRESS" ] \
        && [ "${waited}" -lt "${STACK_SETTLE_TIMEOUT:-3600}" ]; do
    if [ "${announced}" = false ]; then
      info "stack ${stack} is ${current_status}; waiting for it to settle"
      announced=true
    fi
    sleep 20
    waited=$((waited + 20))
    current_status="$(stack_status "${stack}")"
  done
  [ "${announced}" = true ] && info "stack ${stack} settled at ${current_status}"

  # A stack that rolled back during creation never existed as far as AWS is
  # concerned: it cannot be updated, only deleted and recreated. Without this a
  # retry after a failed create fails again with an unhelpful
  # "is in ROLLBACK_COMPLETE state and can not be updated".
  case "${current_status}" in
    ROLLBACK_COMPLETE|ROLLBACK_FAILED|CREATE_FAILED|REVIEW_IN_PROGRESS)
      info "stack ${stack} is ${current_status}; deleting it so it can be recreated"
      delete_stack "${stack}"
      ;;
  esac

  if stack_exists "${stack}"; then
    info "updating stack ${stack}"
    # The CLI reports a no-op update as a failure, so the exit code and the
    # message have to be inspected separately. Piping update-stack into grep
    # does not work here: under `set -o pipefail` the pipeline inherits the
    # CLI's non-zero exit even when the grep matched, and the caller then waits
    # forever for an update that is never going to start.
    local update_output update_rc
    set +e
    update_output="$(aws cloudformation update-stack "${args[@]}" 2>&1)"
    update_rc=$?
    set -e

    if [ "${update_rc}" -eq 0 ]; then
      aws cloudformation wait stack-update-complete --stack-name "${stack}" || {
        red "stack ${stack} failed to update; most recent failure events:"
        aws cloudformation describe-stack-events --stack-name "${stack}" \
          --query 'StackEvents[?contains(ResourceStatus, `FAILED`)].[LogicalResourceId,ResourceStatusReason]' \
          --output table >&2
        [ -n "${params_file}" ] && rm -f "${params_file}"
        die "stack ${stack} did not update"
      }
    elif grep -q 'No updates are to be performed' <<< "${update_output}"; then
      info "stack ${stack} is already up to date"
    else
      red "${update_output}"
      [ -n "${params_file}" ] && rm -f "${params_file}"
      die "stack ${stack} update failed"
    fi
  else
    # Bare metal launches fail transiently often enough to matter, and one
    # unlucky instance rolls the whole stack back -- taking a healthy,
    # already-running g4dn.metal with it. Retrying is worth real money here, so
    # callers that launch metal set STACK_CREATE_ATTEMPTS above 1.
    local attempt=1
    local max_attempts="${STACK_CREATE_ATTEMPTS:-1}"
    while :; do
      info "creating stack ${stack} (attempt ${attempt}/${max_attempts})"
      aws cloudformation create-stack "${args[@]}" >/dev/null

      if aws cloudformation wait stack-create-complete --stack-name "${stack}"; then
        break
      fi

      local reasons
      reasons="$(aws cloudformation describe-stack-events --stack-name "${stack}" \
        --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
        --output text 2>/dev/null)"
      red "stack ${stack} failed to create:"
      printf '%s\n' "${reasons}" >&2

      # Retry only what AWS might do differently next time. A bad template or a
      # rejected parameter fails identically forever, and retrying it just
      # burns fifteen minutes per attempt.
      if [ "${attempt}" -lt "${max_attempts}" ] && grep -qE \
           'Internal error on launch|did not stabilize|InsufficientInstanceCapacity|Server\.InternalError|Unavailable' \
           <<< "${reasons}"; then
        info "that is a transient AWS-side failure; deleting and retrying"
        delete_stack "${stack}"
        attempt=$((attempt + 1))
        continue
      fi

      [ -n "${params_file}" ] && rm -f "${params_file}"
      die "stack ${stack} did not create"
    done
  fi

  # The parameter file holds the BMC password and the ignition pointers.
  [ -n "${params_file}" ] && rm -f "${params_file}"
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

# Delete several stacks at once, and report which of them failed.
#
# CloudFormation is not the slow part of a teardown; waiting is. Nearly all of
# it is inside `wait stack-delete-complete` while EC2 releases a bare-metal
# instance, which takes about twenty minutes -- so two metal stacks deleted one
# after the other cost forty minutes for work that has no ordering between it.
#
# Stacks passed here are deleted together. The caller keeps the ordering that
# does matter: instances before the security groups they sit in, the peering
# before either VPC.
delete_stacks_parallel() {
  local stack rc=0 i
  local -a pids=() names=()
  for stack in "$@"; do
    delete_stack "${stack}" &
    pids+=("$!")
    names+=("${stack}")
  done
  for i in "${!pids[@]}"; do
    if ! wait "${pids[$i]}"; then
      red "stack ${names[$i]} failed to delete"
      rc=1
    fi
  done
  return "${rc}"
}

save_state() {
  printf '%s' "$2" > "${STATE_DIR}/$1"
}

# Both readers succeed on a missing file. Callers use them in assignments, and
# under `set -e` a failing command substitution in an assignment kills the
# script -- which is how a not-yet-created ACM site took down inventory.sh.
# "no value recorded" is a normal state here, not an error.
read_state() {
  cat "${STATE_DIR}/$1" 2>/dev/null || true
}

# The ACM site keeps its own state alongside TNF's rather than mixing the two,
# so that "which bastion" is never ambiguous.
save_acm_state() {
  printf '%s' "$2" > "${ACM_STATE_DIR}/$1"
}

read_acm_state() {
  cat "${ACM_STATE_DIR}/$1" 2>/dev/null || true
}

# Resolve the public Route53 zone the cluster's DNS records go into.
#
# Deliberately discovered rather than configured. The zone name is issued per
# account -- sandbox807.opentlc.com in one, something else in the next -- so
# hard-coding it means editing config on every new account, which is exactly the
# kind of per-environment constant that has bitten this toolbox before.
#
# BASE_DOMAIN in instance.env still wins when set; discovery only fills a blank.
resolve_public_hosted_zone() {
  local zones count
  zones="$(aws route53 list-hosted-zones \
    --query 'HostedZones[?Config.PrivateZone==`false`].[Name,Id]' --output text)"

  if [ -z "${zones}" ]; then
    die "no public Route53 hosted zone in this account; set BASE_DOMAIN and create one"
  fi

  if [ -n "${BASE_DOMAIN:-}" ]; then
    # Trailing dots are how Route53 returns names; strip for comparison.
    PUBLIC_ZONE_ID="$(awk -v d="${BASE_DOMAIN}." '$1 == d {print $2}' <<< "${zones}" | head -1)"
    [ -n "${PUBLIC_ZONE_ID}" ] || die "no public hosted zone matches BASE_DOMAIN=${BASE_DOMAIN}
available: $(awk '{printf "%s ", $1}' <<< "${zones}")"
  else
    count="$(wc -l <<< "${zones}")"
    [ "${count}" -eq 1 ] || die "found ${count} public hosted zones; set BASE_DOMAIN to pick one:
$(awk '{printf "  %s\n", $1}' <<< "${zones}")"
    BASE_DOMAIN="$(awk '{print $1}' <<< "${zones}" | sed 's/\.$//')"
    PUBLIC_ZONE_ID="$(awk '{print $2}' <<< "${zones}")"
  fi

  # Route53 returns /hostedzone/ZXXXX; the API wants the bare id.
  PUBLIC_ZONE_ID="${PUBLIC_ZONE_ID##*/}"
  export BASE_DOMAIN PUBLIC_ZONE_ID
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
