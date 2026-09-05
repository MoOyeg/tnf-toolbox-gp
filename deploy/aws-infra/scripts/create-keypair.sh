#!/bin/bash
# Create the SSH key pair the environment is built around, from instance.env.
#
# Two things have to line up and are easy to get wrong separately:
#
#   SSH_KEY_NAME     an EC2 key pair, which CloudFormation stamps onto every
#                    instance as KeyName
#   SSH_PRIVATE_KEY  the local file Ansible and ssh.sh actually authenticate
#                    with
#
# If those are not two halves of the same key, everything builds cleanly and
# then nothing can be logged into -- the failure lands forty minutes later, on
# the first Ansible task, as a permission denied against a bastion that is
# otherwise healthy. Worse, a sandbox account handed back and reissued keeps the
# local key while losing the EC2 one, so the pair silently comes apart between
# runs.
#
# This creates whichever half is missing and refuses to continue if the two that
# exist do not match.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws ssh-keygen

: "${SSH_PRIVATE_KEY:?SSH_PRIVATE_KEY must be set in instance.env}"
: "${SSH_PUBLIC_KEY:?SSH_PUBLIC_KEY must be set in instance.env}"

FORCE="${FORCE:-false}"

# ------------------------------------------------------------- the local half
if [ -f "${SSH_PRIVATE_KEY}" ]; then
  echo "==> local key ${SSH_PRIVATE_KEY} already exists"
  # A private key without its .pub is recoverable: derive it rather than
  # regenerating and invalidating the EC2 side.
  if [ ! -f "${SSH_PUBLIC_KEY}" ]; then
    echo "    public half missing; deriving it from the private key"
    ssh-keygen -y -f "${SSH_PRIVATE_KEY}" > "${SSH_PUBLIC_KEY}"
    chmod 0644 "${SSH_PUBLIC_KEY}"
  fi
else
  echo "==> creating local key ${SSH_PRIVATE_KEY}"
  mkdir -p "$(dirname "${SSH_PRIVATE_KEY}")"
  # ed25519: shorter, and the fingerprint AWS reports for an imported one is the
  # same SHA256 ssh-keygen prints, which makes the check below possible.
  ssh-keygen -t ed25519 -N '' -C "${SSH_KEY_NAME}" -f "${SSH_PRIVATE_KEY}"
  chmod 0600 "${SSH_PRIVATE_KEY}"
fi

# Trailing '=' padding is stripped from both sides: AWS returns the base64
# fingerprint padded, ssh-keygen prints it unpadded, and comparing them raw
# reports every correctly matched ed25519 key as a mismatch.
local_fp="$(ssh-keygen -lf "${SSH_PUBLIC_KEY}" \
              | awk '{print $2}' | sed -e 's/^SHA256://' -e 's/=*$//')"

# --------------------------------------------------------------- the EC2 half
aws_fp_raw="$(aws ec2 describe-key-pairs \
                --key-names "${SSH_KEY_NAME}" \
                --query 'KeyPairs[0].KeyFingerprint' \
                --output text 2>/dev/null || true)"
aws_fp="$(printf '%s' "${aws_fp_raw}" | sed 's/=*$//')"

if [ -z "${aws_fp_raw}" ] || [ "${aws_fp_raw}" = "None" ]; then
  echo "==> importing ${SSH_KEY_NAME} into EC2 in ${REGION}"
  aws ec2 import-key-pair \
    --key-name "${SSH_KEY_NAME}" \
    --public-key-material "fileb://${SSH_PUBLIC_KEY}" \
    --query 'KeyName' --output text
  echo "==> key pair ${SSH_KEY_NAME} created"
  exit 0
fi

echo "==> EC2 key pair ${SSH_KEY_NAME} already exists"

# AWS reports SHA256 (base64) for an imported ed25519 key and MD5 (hex, colon
# separated) for RSA. Only the first can be compared against ssh-keygen's
# output, so an RSA key is reported rather than judged.
if [ "${aws_fp}" = "${local_fp}" ]; then
  echo "==> local key and EC2 key pair match"
  exit 0
fi

if [[ "${aws_fp_raw}" == *:* ]]; then
  cat >&2 <<MSG

  ${SSH_KEY_NAME} in EC2 has an MD5 fingerprint, so it is an RSA key and cannot
  be compared with the local ed25519 one directly:

    EC2:   ${aws_fp_raw}
    local: SHA256:${local_fp}  (${SSH_PUBLIC_KEY})

  If they are the same key this is fine. If they are not, SSH to the bastion
  will fail after the infrastructure is built. To be sure, delete the EC2 key
  pair and re-run so it is imported from the local one:

    aws ec2 delete-key-pair --key-name ${SSH_KEY_NAME} --region ${REGION}
    make keypair

MSG
  exit 0
fi

die "$(cat <<MSG
${SSH_KEY_NAME} exists in EC2 but is a different key from ${SSH_PRIVATE_KEY}.

    EC2:   ${aws_fp_raw}
    local: ${local_fp}

Instances would be built with a key you cannot log in with. Either point
SSH_PRIVATE_KEY at the matching private key, or replace the EC2 side:

    aws ec2 delete-key-pair --key-name ${SSH_KEY_NAME} --region ${REGION}
    make keypair
MSG
)"
