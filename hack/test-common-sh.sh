#!/bin/bash
# Unit tests for the shell helpers in deploy/common.sh that are easy to get
# wrong and expensive to get wrong. No AWS calls.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../deploy/common.sh
source "${REPO_ROOT}/deploy/common.sh"

FAILURES=0
check() {
  if [ "$2" = "true" ]; then
    printf '  PASS  %s\n' "$1"
  else
    printf '  FAIL  %s%s\n' "$1" "${3:+: $3}"
    FAILURES=$((FAILURES + 1))
  fi
}

echo
echo "build_stack_parameters"

tmp="$(mktemp)"

# The bug this exists for: the CLI's shorthand --parameters syntax cannot carry
# a value containing '=', ',' or a quote, so an ignition config could not be
# passed at all. Parameters are built as JSON now, and this proves it survives.
IGNITION='{"ignition":{"version":"3.4.0","config":{"merge":[{"source":"http://10.0.0.5:8080/master.ign"}]}},"storage":{"files":[{"path":"/etc/hostname","mode":420,"overwrite":true,"contents":{"source":"data:,master-0%0A"}}]}}'

build_stack_parameters "${tmp}" \
  "ClusterName=tnf-gp" \
  "Master0UserData=${IGNITION}" \
  "BmcPassword=pa\"ss,w=ord\$x" \
  "Empty="

jq -e . "${tmp}" >/dev/null 2>&1
check "produces valid JSON" "$([ $? -eq 0 ] && echo true || echo false)"

count="$(jq 'length' "${tmp}")"
check "one entry per parameter" "$([ "${count}" = "4" ] && echo true || echo false)" "got ${count}"

round_trip="$(jq -r '.[] | select(.ParameterKey=="Master0UserData") | .ParameterValue' "${tmp}")"
check "an ignition config survives verbatim" \
  "$([ "${round_trip}" = "${IGNITION}" ] && echo true || echo false)"

jq -e '.[] | select(.ParameterKey=="Master0UserData") | .ParameterValue | fromjson' "${tmp}" >/dev/null 2>&1
check "the round-tripped ignition still parses as JSON" \
  "$([ $? -eq 0 ] && echo true || echo false)"

# Values may contain '=' -- splitting on the last one would corrupt them.
password="$(jq -r '.[] | select(.ParameterKey=="BmcPassword") | .ParameterValue' "${tmp}")"
check "quotes, commas and equals in a password survive" \
  "$([ "${password}" = 'pa"ss,w=ord$x' ] && echo true || echo false)" "got ${password}"

empty="$(jq -r '.[] | select(.ParameterKey=="Empty") | .ParameterValue' "${tmp}")"
check "an empty value stays empty rather than vanishing" \
  "$([ -z "${empty}" ] && echo true || echo false)"

check "no stray entries file is left behind" \
  "$([ ! -f "${tmp}.entries" ] && echo true || echo false)"

rm -f "${tmp}"

echo
if [ "${FAILURES}" -eq 0 ]; then
  echo "All common.sh checks passed."
else
  echo "${FAILURES} check(s) failed."
fi
exit "${FAILURES}"
