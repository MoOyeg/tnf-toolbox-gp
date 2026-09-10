#!/bin/bash
# S3 for the ACM hub's Thanos, and a user that can reach only that bucket.
#
# MultiClusterObservability keeps long-term metrics in S3-compatible object
# storage. The usual answer is ODF/Noobaa, but the hub here is a single node
# already carrying ACM, MCE, OpenShift Virtualization and every guest cluster's
# control plane -- and Noobaa would want local disk on that same node to serve
# object storage back to it. On AWS there is a real S3 a few milliseconds away,
# so the hub gets that instead and keeps its CPU for the fleet.
#
# The credentials that reach the cluster belong to a user created here whose
# only policy is this one bucket. The workstation's own credentials are
# administrator-wide and have no business being copied into a Secret.
set -euo pipefail
# shellcheck source=../../common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../common.sh"

load_config
require_tools aws jq

BUCKET="${OBSERVABILITY_BUCKET:-${ACM_CLUSTER_NAME}-mco-$(aws sts get-caller-identity --query Account --output text)}"
USER_NAME="${ACM_CLUSTER_NAME}-mco"
POLICY_NAME="${ACM_CLUSTER_NAME}-mco-bucket"

# Bucket. Already-owned is success: this script is re-runnable, and losing the
# bucket would lose the history it exists to keep.
if aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  info "bucket ${BUCKET} already exists"
else
  info "creating bucket ${BUCKET}"
  aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
    --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  aws s3api put-public-access-block --bucket "${BUCKET}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
  aws s3api put-bucket-encryption --bucket "${BUCKET}" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
fi

# A user scoped to this bucket and nothing else.
aws iam get-user --user-name "${USER_NAME}" >/dev/null 2>&1 \
  || { info "creating IAM user ${USER_NAME}"; aws iam create-user --user-name "${USER_NAME}" >/dev/null; }

POLICY="$(mktemp)"; trap 'rm -f "${POLICY}"' EXIT
cat > "${POLICY}" <<POL
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::${BUCKET}" },
    { "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/*" }
  ]
}
POL
aws iam put-user-policy --user-name "${USER_NAME}" \
  --policy-name "${POLICY_NAME}" --policy-document "file://${POLICY}"

# One access key at a time. IAM allows two, and leaving orphans behind is how a
# rerun eventually fails with LimitExceeded on a cluster nobody is watching.
for key in $(aws iam list-access-keys --user-name "${USER_NAME}" \
               --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null); do
  info "removing previous access key ${key}"
  aws iam delete-access-key --user-name "${USER_NAME}" --access-key-id "${key}"
done
CREDS="$(aws iam create-access-key --user-name "${USER_NAME}")"

save_acm_state observability_bucket    "${BUCKET}"
save_acm_state observability_endpoint  "s3.${REGION}.amazonaws.com"
save_acm_state observability_key_id    "$(jq -r .AccessKey.AccessKeyId <<< "${CREDS}")"
save_acm_state observability_key_secret "$(jq -r .AccessKey.SecretAccessKey <<< "${CREDS}")"
chmod 600 "${ACM_STATE_DIR}/observability_key_secret"

green "observability bucket ${BUCKET} ready, scoped user ${USER_NAME}"
