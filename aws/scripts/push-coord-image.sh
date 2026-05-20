#!/usr/bin/env bash
# Build, push, and deploy the qontinui-canonical-coord image to ECR.
#
# The terraform.tfvars `coord_image_uri` says `:staging` (a convenience
# default for fresh `terraform apply`), but every actual deploy
# SHA-pins the task definition for rollback traceability. This script
# automates the SHA-pin flow:
#
#   1. Compute commit-SHA tag from the qontinui-coord checkout
#   2. Build linux/amd64 image
#   3. Push BOTH `:<sha>` and `:staging` to ECR (the SHA tag is the
#      authoritative one; `:staging` is a moving alias)
#   4. Register a new ECS task definition revision pinned at `:<sha>`
#   5. Update the ECS service to use the new revision + force redeploy
#   6. Wait for rollout completion
#
# Usage:
#   AWS_REGION=us-east-1 ENVIRONMENT=staging bash scripts/push-coord-image.sh
#
# Optional overrides:
#   COORD_SOURCE_DIR    Path to qontinui-coord checkout. Default:
#                       sibling at `<script-dir>/../../../qontinui-coord`.
#   ECR_REPO            ECR repository name. Default: qontinui-coord.
#   SKIP_DEPLOY=1       Push image + register revision but do NOT update
#                       the service. Useful for dry runs.
#   SHA_TAG             Explicit SHA tag to use (overrides git-derived).
#                       Use when the checkout is dirty or detached.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-staging}"
REPO="${ECR_REPO:-qontinui-coord}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COORD_SOURCE_DIR="${COORD_SOURCE_DIR:-$SCRIPT_DIR/../../../qontinui-coord}"
CLUSTER="qontinui-$ENVIRONMENT"
SERVICE="coord"
TASK_FAMILY="qontinui-$ENVIRONMENT-coord"

# ──────────────────────────────────────────────────────────────────────────
# Compute SHA tag from the qontinui-coord checkout. Use the short commit
# SHA so the tag is stable across rebuilds of the same source state.
# Fail-fast if the checkout is dirty (uncommitted changes) or detached
# from a tracked branch — we don't want to ship local-modified code.
# ──────────────────────────────────────────────────────────────────────────

if [[ -n "${SHA_TAG:-}" ]]; then
    TAG="$SHA_TAG"
    echo "==> Using operator-supplied SHA tag: $TAG"
else
    if [[ ! -d "$COORD_SOURCE_DIR/.git" ]]; then
        echo "ERROR: $COORD_SOURCE_DIR is not a git checkout" >&2
        exit 1
    fi
    cd "$COORD_SOURCE_DIR"
    if [[ -n "$(git status --porcelain)" ]]; then
        echo "ERROR: $COORD_SOURCE_DIR has uncommitted changes." >&2
        echo "  Either commit them, or set SHA_TAG=<explicit> to override." >&2
        exit 1
    fi
    BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "DETACHED")
    if [[ "$BRANCH" == "DETACHED" ]]; then
        echo "WARNING: $COORD_SOURCE_DIR is in detached HEAD state."
        echo "  Building from $(git rev-parse --short HEAD) — verify this is intentional."
    elif [[ "$BRANCH" != "main" ]]; then
        echo "WARNING: $COORD_SOURCE_DIR is on branch '$BRANCH', not main."
        echo "  Building from $(git rev-parse --short HEAD) — verify this is intentional."
    fi
    TAG=$(git rev-parse --short=7 HEAD)
    cd - >/dev/null
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO"
SHA_URI="$ECR_BASE:$TAG"
STAGING_URI="$ECR_BASE:staging"

echo "==> Account: $ACCOUNT_ID"
echo "==> SHA tag: $TAG"
echo "==> Cluster: $CLUSTER, service: $SERVICE, family: $TASK_FAMILY"

# ──────────────────────────────────────────────────────────────────────────
# ECR repo + login
# ──────────────────────────────────────────────────────────────────────────

echo "==> Ensuring ECR repository exists: $REPO"
aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$REPO" --region "$REGION" \
        --image-scanning-configuration scanOnPush=true >/dev/null

echo "==> Logging in to ECR"
aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

# ──────────────────────────────────────────────────────────────────────────
# Build + push (both SHA and staging tags)
# ──────────────────────────────────────────────────────────────────────────

echo "==> Building qontinui-canonical-coord:$TAG (source: $COORD_SOURCE_DIR)"
docker buildx build \
    --platform linux/amd64 \
    -t "qontinui-canonical-coord:$TAG" \
    -t "$SHA_URI" \
    -t "$STAGING_URI" \
    "$COORD_SOURCE_DIR"

echo "==> Pushing $SHA_URI"
docker push "$SHA_URI"

echo "==> Pushing $STAGING_URI"
docker push "$STAGING_URI"

# ──────────────────────────────────────────────────────────────────────────
# Register new ECS task definition revision pinning the SHA tag
# ──────────────────────────────────────────────────────────────────────────

echo "==> Fetching current task definition: $TASK_FAMILY"
TMP_TASK_DEF=$(mktemp)
trap 'rm -f "$TMP_TASK_DEF" "$TMP_TASK_DEF.new"' EXIT
aws ecs describe-task-definition \
    --task-definition "$TASK_FAMILY" \
    --region "$REGION" \
    --query 'taskDefinition' \
    > "$TMP_TASK_DEF"

echo "==> Rewriting image to $SHA_URI"
python - "$TMP_TASK_DEF" "$TMP_TASK_DEF.new" "$SHA_URI" <<'PYEOF'
import json, sys
src, dst, new_image = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(src))
old_image = d['containerDefinitions'][0]['image']
d['containerDefinitions'][0]['image'] = new_image
# Strip fields that register-task-definition rejects on input
for k in ['taskDefinitionArn', 'revision', 'status', 'requiresAttributes',
          'compatibilities', 'registeredAt', 'registeredBy']:
    d.pop(k, None)
json.dump(d, open(dst, 'w'))
print(f"  old: {old_image}")
print(f"  new: {new_image}")
PYEOF

echo "==> Registering new task definition revision"
NEW_REVISION_ARN=$(aws ecs register-task-definition \
    --cli-input-json "file://$TMP_TASK_DEF.new" \
    --region "$REGION" \
    --query 'taskDefinition.taskDefinitionArn' \
    --output text)
NEW_REVISION="${NEW_REVISION_ARN##*/}"
echo "  registered: $NEW_REVISION"

if [[ "${SKIP_DEPLOY:-0}" == "1" ]]; then
    echo
    echo "SKIP_DEPLOY=1 — stopping after register-task-definition."
    echo "  To deploy:  aws ecs update-service --cluster $CLUSTER --service $SERVICE --task-definition $NEW_REVISION --force-new-deployment --region $REGION"
    exit 0
fi

# ──────────────────────────────────────────────────────────────────────────
# Update service + wait for rollout
# ──────────────────────────────────────────────────────────────────────────

echo "==> Updating service $SERVICE to $NEW_REVISION (force-new-deployment)"
aws ecs update-service \
    --cluster "$CLUSTER" \
    --service "$SERVICE" \
    --task-definition "$NEW_REVISION" \
    --force-new-deployment \
    --region "$REGION" \
    --query 'service.deployments[?status==`PRIMARY`].{rolloutState:rolloutState,taskDef:taskDefinition}' \
    --output table

echo "==> Waiting for rollout completion (polling every 30s)"
while true; do
    STATE=$(aws ecs describe-services \
        --cluster "$CLUSTER" \
        --services "$SERVICE" \
        --region "$REGION" \
        --query 'services[0].deployments[?status==`PRIMARY`].rolloutState' \
        --output text)
    if [[ "$STATE" == "COMPLETED" ]]; then
        echo "  rolloutState: COMPLETED"
        break
    fi
    if [[ "$STATE" == "FAILED" ]]; then
        echo "  rolloutState: FAILED — aborting"
        exit 1
    fi
    echo "  rolloutState: $STATE — waiting..."
    sleep 30
done

echo
echo "==> Deploy complete"
echo "    Image:     $SHA_URI"
echo "    Revision:  $NEW_REVISION"
echo "    Service:   $CLUSTER/$SERVICE"
