#!/usr/bin/env bash
# One-time bootstrap for the Terraform remote-state S3 bucket + DynamoDB
# lock table. Run this once per AWS account before the first
# `terraform init` of any environment.
#
# After this runs, the staging/ and prod/ Terraform configs can use the
# `s3` backend block in their `backend.tf`.
#
# Idempotent: errors on already-existing resources are tolerated.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
BUCKET="${TF_STATE_BUCKET:-qontinui-tfstate}"
TABLE="${TF_LOCK_TABLE:-qontinui-tfstate-lock}"

echo "==> Bootstrapping Terraform backend in $REGION..."
echo "    bucket=$BUCKET table=$TABLE"

# 1. Bucket
if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
    echo "    bucket already exists — skipping create"
else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
        $([ "$REGION" != "us-east-1" ] && echo "--create-bucket-configuration LocationConstraint=$REGION")
    aws s3api put-bucket-versioning --bucket "$BUCKET" \
        --versioning-configuration Status=Enabled
    aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
    aws s3api put-public-access-block --bucket "$BUCKET" \
        --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    echo "    bucket created + versioned + encrypted + private"
fi

# 2. Lock table
if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
    echo "    lock table already exists — skipping create"
else
    aws dynamodb create-table \
        --table-name "$TABLE" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region "$REGION"
    echo "    lock table created"
fi

echo "==> Done. You can now 'terraform init' from staging/ or prod/."
