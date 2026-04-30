#!/usr/bin/env bash
# Build and push the qontinui-canonical-coord image to ECR for an
# environment. Run before `terraform apply` or whenever the image
# changes.
#
# Usage:
#   AWS_REGION=us-east-1 IMAGE_TAG=staging bash scripts/push-coord-image.sh
#
# Outputs the full image URI on stdout — paste into terraform.tfvars.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
TAG="${IMAGE_TAG:-staging}"
REPO="${ECR_REPO:-qontinui-coord}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG"

echo "==> Ensuring ECR repository exists: $REPO"
aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$REPO" --region "$REGION" \
        --image-scanning-configuration scanOnPush=true >/dev/null

echo "==> Logging in to ECR"
aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

echo "==> Building qontinui-canonical-coord:$TAG"
docker buildx build \
    --platform linux/amd64 \
    -t "qontinui-canonical-coord:$TAG" \
    -t "$URI" \
    "$(dirname "$0")/../../../qontinui-coord"

echo "==> Pushing $URI"
docker push "$URI"

echo
echo "Image pushed. Add this to terraform.tfvars:"
echo "  coord_image_uri = \"$URI\""
