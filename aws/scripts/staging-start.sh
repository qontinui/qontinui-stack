#!/usr/bin/env bash
# Re-start what staging-stop.sh stopped.

set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"

echo "==> Starting RDS instance qontinui-staging..."
aws rds start-db-instance --db-instance-identifier qontinui-staging --region "$REGION" \
    || echo "    RDS start failed (may already be running)"

echo "==> Waiting for RDS to be available (~3-5 min)..."
aws rds wait db-instance-available --db-instance-identifier qontinui-staging --region "$REGION"

echo "==> Scaling ECS service back to 1..."
aws ecs update-service \
    --cluster qontinui-staging \
    --service coord \
    --desired-count 1 \
    --region "$REGION" >/dev/null

echo "==> Done. Coord task will appear within ~30s; check ALB health probes."
