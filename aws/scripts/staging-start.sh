#!/usr/bin/env bash
# Re-start what staging-stop.sh stopped.
#
# HA Phase C.6: default replica count raised to 2 (multi-AZ baseline).
# Override via COORD_DESIRED_COUNT env var if you need a different count
# (e.g. COORD_DESIRED_COUNT=1 for cost-constrained debugging sessions).

set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
COORD_DESIRED_COUNT="${COORD_DESIRED_COUNT:-2}"

echo "==> Starting RDS instance qontinui-staging..."
aws rds start-db-instance --db-instance-identifier qontinui-staging --region "$REGION" \
    || echo "    RDS start failed (may already be running)"

echo "==> Waiting for RDS to be available (~3-5 min)..."
aws rds wait db-instance-available --db-instance-identifier qontinui-staging --region "$REGION"

echo "==> Scaling ECS service to ${COORD_DESIRED_COUNT} replica(s)..."
aws ecs update-service \
    --cluster qontinui-staging \
    --service coord \
    --desired-count "${COORD_DESIRED_COUNT}" \
    --region "$REGION" >/dev/null

echo "==> Done. Coord tasks will appear within ~30s; check ALB health probes."
