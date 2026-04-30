#!/usr/bin/env bash
# Stop billable resources without destroying state. After this:
# - RDS goes to "stopped" (storage retained; auto-restart after 7 days)
# - ECS service scales to 0 tasks
# ElastiCache cannot be stopped without deletion; it stays running.

set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"

echo "==> Stopping RDS instance qontinui-staging..."
aws rds stop-db-instance --db-instance-identifier qontinui-staging --region "$REGION" \
    || echo "    RDS stop failed (may already be stopped)"

echo "==> Scaling ECS service to 0..."
aws ecs update-service \
    --cluster qontinui-staging \
    --service coord \
    --desired-count 0 \
    --region "$REGION" >/dev/null

echo "==> Done. ALB + ElastiCache + storage continue billing (~$30/mo idle)."
