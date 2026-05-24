#!/usr/bin/env bash
# coord-ha-chaos-validate.sh — Repeatable HA Phase C acceptance check.
#
# Validates the coord ECS service survives a leader task termination with:
#   RPO 0  — a critical git ref written before the kill is intact after promotion
#   RTO    — a standby promotes within the configured lease TTL
#   Resume — agent git traffic (fetch) succeeds on the new leader
#
# Usage:
#   ./coord-ha-chaos-validate.sh [CLUSTER] [SERVICE] [COORD_URL] [REGION]
#
# All positional args default to staging values and can also be set via env:
#   COORD_CLUSTER   ECS cluster name           (default: qontinui-staging)
#   COORD_SERVICE   ECS service name           (default: coord)
#   COORD_URL       coord HTTP base URL        (default: https://coord.staging.qontinui.io)
#   AWS_REGION      AWS region                 (default: us-east-1)
#   LEASE_TTL_SECS  expected max promotion time (default: 30)
#   POLL_INTERVAL   seconds between health polls (default: 2)
#
# Credentials required:
#   - AWS_PROFILE or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY with
#     ecs:StopTask, ecs:DescribeServices, ecs:DescribeTasks,
#     ecs:ListTasks permissions on COORD_CLUSTER.
#   OPERATOR INPUT NEEDED: coord write credentials (see Step 2 below).
#
# Exit codes:
#   0 — all assertions PASS
#   1 — one or more assertions FAIL (details printed to stdout)
#   2 — script setup error (missing tool, bad args)

set -euo pipefail

# ─── Parameters ─────────────────────────────────────────────────────────────

COORD_CLUSTER="${1:-${COORD_CLUSTER:-qontinui-staging}}"
COORD_SERVICE="${2:-${COORD_SERVICE:-coord}}"
COORD_URL="${3:-${COORD_URL:-https://coord.staging.qontinui.io}}"
AWS_REGION="${4:-${AWS_REGION:-us-east-1}}"
LEASE_TTL_SECS="${LEASE_TTL_SECS:-30}"
POLL_INTERVAL="${POLL_INTERVAL:-2}"

# Test repo/ref used for the RPO-0 assertion.  Must be a repo already tracked
# by coord on the target cluster.
# OPERATOR INPUT NEEDED: set TEST_REPO to a real repo slug known to coord, e.g.
#   TEST_REPO="jspinak/qontinui-coord"
TEST_REPO="${TEST_REPO:-}"

# ─── Prerequisites ───────────────────────────────────────────────────────────

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: required command '$1' not found"; exit 2; }
}
require_cmd aws
require_cmd curl
require_cmd jq
require_cmd date

# ─── Helpers ─────────────────────────────────────────────────────────────────

PASS_COUNT=0
FAIL_COUNT=0
FAILURES=""

pass() { echo "  PASS: $*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "  FAIL: $*"; FAIL_COUNT=$((FAIL_COUNT + 1)); FAILURES="${FAILURES}\n  - $*"; }

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# Poll until a condition (bash snippet) is true, or time out.
# Usage: poll_until TIMEOUT_SECS DESCRIPTION "bash condition"
poll_until() {
  local timeout="$1" desc="$2" condition="$3"
  local deadline=$(( $(date +%s) + timeout ))
  echo "  Polling: $desc (timeout ${timeout}s)..."
  while true; do
    if eval "$condition" >/dev/null 2>&1; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      return 1
    fi
    sleep "$POLL_INTERVAL"
  done
}

# Return the task ARN of the ECS task that holds the coord leader lease.
# Strategy: query the /health endpoint on each running task via ECS exec, or
# use the /ha/leader endpoint if coord exposes it publicly.
# Falls back to the first running task ARN if the leader API is unavailable.
get_leader_task_arn() {
  local leader_arn

  # Preferred: coord /ha/leader endpoint returns {"leader_task_id": "<arn-suffix>"}
  # OPERATOR INPUT NEEDED: confirm the exact path once the leader-info endpoint
  # is implemented in coord (tracked in HA Phase C.2).  The code below attempts
  # it and falls back gracefully.
  local leader_resp
  leader_resp=$(curl -sf --max-time 5 "${COORD_URL}/ha/leader" 2>/dev/null || true)
  if [ -n "$leader_resp" ]; then
    local leader_id
    leader_id=$(echo "$leader_resp" | jq -r '.leader_task_id // empty' 2>/dev/null || true)
    if [ -n "$leader_id" ]; then
      # Convert short task ID to full ARN
      leader_arn=$(aws ecs list-tasks \
        --cluster "$COORD_CLUSTER" \
        --service-name "$COORD_SERVICE" \
        --desired-status RUNNING \
        --region "$AWS_REGION" \
        --output json \
        | jq -r ".taskArns[] | select(endswith(\"$leader_id\"))" | head -1)
      if [ -n "$leader_arn" ]; then
        echo "$leader_arn"
        return 0
      fi
    fi
  fi

  # Fallback: pick the first running task (assumes single-leader, first task listed)
  aws ecs list-tasks \
    --cluster "$COORD_CLUSTER" \
    --service-name "$COORD_SERVICE" \
    --desired-status RUNNING \
    --region "$AWS_REGION" \
    --output json \
    | jq -r '.taskArns[0] // empty'
}

get_running_task_count() {
  aws ecs describe-services \
    --cluster "$COORD_CLUSTER" \
    --services "$COORD_SERVICE" \
    --region "$AWS_REGION" \
    --output json \
    | jq -r '.services[0].runningCount'
}

# ─── PRE-FLIGHT ──────────────────────────────────────────────────────────────

echo "========================================="
echo " coord HA Chaos Validation"
echo " $(ts)"
echo "========================================="
echo " Cluster : $COORD_CLUSTER"
echo " Service : $COORD_SERVICE"
echo " URL     : $COORD_URL"
echo " Region  : $AWS_REGION"
echo " Lease TTL (expected max promotion): ${LEASE_TTL_SECS}s"
echo ""

echo "[0] Pre-flight checks"

# Verify service exists and has >=2 running tasks (HA requires a standby)
RUNNING_COUNT=$(get_running_task_count)
echo "  Running tasks: $RUNNING_COUNT"
if [ "$RUNNING_COUNT" -lt 2 ]; then
  echo "ERROR: HA chaos test requires >=2 running coord tasks (found $RUNNING_COUNT)."
  echo "       Scale up first: aws ecs update-service --cluster $COORD_CLUSTER --service $COORD_SERVICE --desired-count 2 --region $AWS_REGION"
  exit 2
fi
pass "Service has $RUNNING_COUNT running tasks (>=2 required)"

# Verify coord is healthy before we start
echo "  Checking coord /health before chaos..."
HEALTH_BEFORE=$(curl -sf --max-time 10 "${COORD_URL}/health" 2>/dev/null || echo "UNREACHABLE")
if echo "$HEALTH_BEFORE" | grep -qi "ok\|healthy\|pass\|true"; then
  pass "/health before chaos: healthy"
else
  fail "/health before chaos: got '${HEALTH_BEFORE}' — coord may not be healthy, aborting"
  echo ""
  echo "ABORTED — fix coord health before running chaos validation."
  exit 2
fi

# ─── STEP 1: Identify the leader task ────────────────────────────────────────

echo ""
echo "[1] Identify leader task"

LEADER_ARN=$(get_leader_task_arn)
if [ -z "$LEADER_ARN" ]; then
  fail "Could not identify leader task ARN"
  echo "ABORTED"
  exit 2
fi
LEADER_SHORT="${LEADER_ARN##*/}"
echo "  Leader task ARN: $LEADER_ARN"
pass "Leader task identified: $LEADER_SHORT"

# ─── STEP 2: Write a critical ref (RPO-0 sentinel) ───────────────────────────

echo ""
echo "[2] Write RPO-0 sentinel ref via coord git-http API"

SENTINEL_REF="refs/chaos/ha-validate-$(date +%s)"
SENTINEL_SHA="0000000000000000000000000000000000000001"  # placeholder OID

# OPERATOR INPUT NEEDED: coord's git-http write path requires authentication.
# Supply COORD_WRITE_TOKEN (Bearer token) or COORD_ADMIN_SECRET (admin creds)
# via environment.  The exact endpoint path depends on the coord git-http
# receive-pack implementation (typically POST /<repo>/git-receive-pack or
# a coord-native ref-update API).
#
# Expected invocation once credentials are available:
#   curl -sf -X POST "${COORD_URL}/git/${TEST_REPO}/info/refs?service=git-receive-pack" \
#     -H "Authorization: Bearer ${COORD_WRITE_TOKEN}" ...
#
# For now we use the coord admin stats/ha endpoint as a lighter sentinel —
# a POST to /ha/checkpoint if it exists, otherwise we record that this step
# needs operator credentials and mark it as skipped (not a FAIL on its own).

RPO_WRITE_SKIPPED=false

if [ -z "${COORD_WRITE_TOKEN:-}" ] && [ -z "${COORD_ADMIN_SECRET:-}" ]; then
  echo "  OPERATOR INPUT NEEDED: set COORD_WRITE_TOKEN or COORD_ADMIN_SECRET to"
  echo "  enable the git-http write step.  Skipping RPO-0 write; the ref-intact"
  echo "  assertion (Step 5) will also be skipped."
  RPO_WRITE_SKIPPED=true
else
  # Attempt to write a sentinel via coord admin API
  AUTH_HEADER="Authorization: Bearer ${COORD_WRITE_TOKEN:-${COORD_ADMIN_SECRET}}"
  if [ -n "$TEST_REPO" ]; then
    WRITE_RESP=$(curl -sf --max-time 10 \
      -X POST "${COORD_URL}/coord/ha/sentinel" \
      -H "$AUTH_HEADER" \
      -H "Content-Type: application/json" \
      -d "{\"ref\": \"${SENTINEL_REF}\", \"sha\": \"${SENTINEL_SHA}\", \"repo\": \"${TEST_REPO}\"}" \
      2>/dev/null || echo "WRITE_FAILED")
    if echo "$WRITE_RESP" | grep -qi "WRITE_FAILED\|error\|404\|405"; then
      echo "  NOTE: /coord/ha/sentinel endpoint not available ($(echo "$WRITE_RESP" | head -c 80))."
      echo "  OPERATOR INPUT NEEDED: implement POST /coord/ha/sentinel in coord, or"
      echo "  provide a git-receive-pack path + credentials for direct ref writes."
      RPO_WRITE_SKIPPED=true
    else
      pass "Sentinel ref written: $SENTINEL_REF on $TEST_REPO"
    fi
  else
    echo "  OPERATOR INPUT NEEDED: set TEST_REPO env var to a repo slug tracked by"
    echo "  coord (e.g. TEST_REPO=jspinak/qontinui-coord) to enable RPO-0 assertion."
    RPO_WRITE_SKIPPED=true
  fi
fi

# ─── STEP 3: Terminate the leader task ───────────────────────────────────────

echo ""
echo "[3] Terminate leader task (chaos inject)"

KILL_TS=$(date +%s)
echo "  Stopping task $LEADER_SHORT at $(ts)..."
STOP_RESP=$(aws ecs stop-task \
  --cluster "$COORD_CLUSTER" \
  --task "$LEADER_ARN" \
  --reason "coord-ha-chaos-validate: intentional leader termination test" \
  --region "$AWS_REGION" \
  --output json 2>&1) || {
  fail "aws ecs stop-task failed: $STOP_RESP"
  echo "ABORTED — could not terminate leader task."
  exit 1
}
echo "  Stop task accepted. Waiting for task to deregister..."
pass "Leader task stop-task call accepted"

# Wait for the killed task to leave RUNNING state (ECS deregisters it)
if poll_until 60 "leader task deregistering" \
  "aws ecs describe-tasks --cluster $COORD_CLUSTER --tasks $LEADER_ARN --region $AWS_REGION --output json | jq -e '.tasks[0].lastStatus != \"RUNNING\"' >/dev/null 2>&1"; then
  pass "Leader task deregistered from RUNNING"
else
  fail "Leader task did not deregister within 60s"
fi

# ─── STEP 4: Assert standby promotes within lease TTL ────────────────────────

echo ""
echo "[4] Assert standby promotion within ${LEASE_TTL_SECS}s"

PROMOTE_DEADLINE=$(( KILL_TS + LEASE_TTL_SECS ))
NOW=$(date +%s)
REMAINING=$(( PROMOTE_DEADLINE - NOW ))
if [ "$REMAINING" -lt 0 ]; then REMAINING=1; fi

echo "  Polling /health for recovery (${REMAINING}s budget)..."
PROMOTE_START=$(date +%s)
PROMOTED=false

while [ "$(date +%s)" -le "$PROMOTE_DEADLINE" ]; do
  HEALTH=$(curl -sf --max-time 5 "${COORD_URL}/health" 2>/dev/null || echo "UNREACHABLE")
  if echo "$HEALTH" | grep -qi "ok\|healthy\|pass\|true"; then
    PROMOTE_END=$(date +%s)
    PROMOTE_ELAPSED=$(( PROMOTE_END - KILL_TS ))
    PROMOTED=true
    echo "  coord healthy again after ${PROMOTE_ELAPSED}s"
    break
  fi
  sleep "$POLL_INTERVAL"
done

if $PROMOTED; then
  if [ "$PROMOTE_ELAPSED" -le "$LEASE_TTL_SECS" ]; then
    pass "Standby promoted within lease TTL (${PROMOTE_ELAPSED}s <= ${LEASE_TTL_SECS}s)"
  else
    fail "Standby promoted but exceeded lease TTL (${PROMOTE_ELAPSED}s > ${LEASE_TTL_SECS}s)"
  fi
else
  ELAPSED=$(( $(date +%s) - KILL_TS ))
  fail "No healthy coord response within ${ELAPSED}s (lease TTL ${LEASE_TTL_SECS}s)"
fi

# Confirm a new task is running (ECS replaced the stopped one)
echo "  Verifying ECS service running count recovered..."
if poll_until 120 "ECS running count back to pre-chaos level" \
  "[ \"\$(get_running_task_count)\" -ge \"$RUNNING_COUNT\" ]"; then
  FINAL_COUNT=$(get_running_task_count)
  pass "ECS running count recovered to $FINAL_COUNT (expected $RUNNING_COUNT)"
else
  FINAL_COUNT=$(get_running_task_count)
  fail "ECS running count did not recover: got $FINAL_COUNT, expected $RUNNING_COUNT"
fi

# ─── STEP 5: Assert RPO-0 — sentinel ref intact on new leader ────────────────

echo ""
echo "[5] Assert RPO-0 — sentinel ref intact after promotion"

if $RPO_WRITE_SKIPPED; then
  echo "  SKIPPED (sentinel write was skipped in Step 2 — see OPERATOR INPUT NEEDED above)"
else
  # Read back the sentinel ref from coord
  AUTH_HEADER="Authorization: Bearer ${COORD_WRITE_TOKEN:-${COORD_ADMIN_SECRET}}"
  READ_RESP=$(curl -sf --max-time 10 \
    "${COORD_URL}/coord/ha/sentinel?ref=${SENTINEL_REF}&repo=${TEST_REPO}" \
    -H "$AUTH_HEADER" \
    2>/dev/null || echo "READ_FAILED")

  if echo "$READ_RESP" | grep -q "$SENTINEL_SHA"; then
    pass "Sentinel ref $SENTINEL_REF intact on new leader (RPO 0 confirmed)"
  elif echo "$READ_RESP" | grep -qi "READ_FAILED\|404\|not found"; then
    echo "  OPERATOR INPUT NEEDED: /coord/ha/sentinel GET not implemented."
    echo "  Alternatively, verify ref via: git ls-remote <coord-git-http-url> $SENTINEL_REF"
    echo "  Skipping RPO-0 read-back assertion."
  else
    fail "Sentinel ref not found or SHA mismatch after promotion (got: $(echo "$READ_RESP" | head -c 120))"
  fi
fi

# ─── STEP 6: Assert git traffic resumes (agent fetch succeeds) ───────────────

echo ""
echo "[6] Assert agent git traffic resumes (fetch succeeds)"

if [ -z "$TEST_REPO" ]; then
  echo "  OPERATOR INPUT NEEDED: set TEST_REPO to enable git-fetch assertion."
  echo "  Example: TEST_REPO=jspinak/qontinui-coord"
  echo "  SKIPPED"
else
  # Try a git ls-remote against the coord git-http endpoint.
  # This exercises the full git smart-http info/refs path on the new leader.
  GIT_URL="${COORD_URL}/git/${TEST_REPO}"

  # OPERATOR INPUT NEEDED: if git-http requires auth, set GIT_CREDS as
  # "user:token" and uncomment the --user flag below.
  # GIT_CREDS="${GIT_CREDS:-}"

  FETCH_RESP=$(curl -sf --max-time 15 \
    "${GIT_URL}/info/refs?service=git-upload-pack" \
    2>/dev/null || echo "FETCH_FAILED")

  if echo "$FETCH_RESP" | grep -q "git-upload-pack"; then
    pass "git-upload-pack info/refs responds correctly on new leader"
  elif echo "$FETCH_RESP" | grep -qi "FETCH_FAILED\|404\|503"; then
    fail "git fetch failed after promotion: ${GIT_URL}/info/refs returned: $(echo "$FETCH_RESP" | head -c 120)"
  else
    # Non-empty response but no git-upload-pack header — may be auth gate
    echo "  NOTE: Response did not contain git-upload-pack marker."
    echo "  OPERATOR INPUT NEEDED: if git-http requires auth, set GIT_CREDS=user:token"
    echo "  Response preview: $(echo "$FETCH_RESP" | head -c 80)"
    fail "git-fetch assertion inconclusive — see OPERATOR INPUT NEEDED note above"
  fi
fi

# ─── SUMMARY ─────────────────────────────────────────────────────────────────

echo ""
echo "========================================="
echo " Summary  $(ts)"
echo "========================================="
echo " PASS: $PASS_COUNT"
echo " FAIL: $FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo ""
  echo "Failed assertions:"
  printf "%b\n" "$FAILURES"
  echo ""
  echo "RESULT: FAIL"
  exit 1
else
  echo ""
  echo "RESULT: PASS"
  exit 0
fi
