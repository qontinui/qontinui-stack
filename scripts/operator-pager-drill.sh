#!/usr/bin/env bash
# Drill the operator pager path and report, per condition, whether the relay delivered it.
#
# Plan: 2026-10-14-coord-pages-the-operator-for-five-conditions-and-names-the-agent-fixing-each
# Phase 1 ships `--target relay`. Later phases add the alarm, rule and coord targets.
#
# Usage:
#   scripts/operator-pager-drill.sh --target relay [--conditions c1,c2] [--timeout SECONDS]
#                                   [--no-summary] [--allow-fallback] [--dry-run]
#
#   --target relay   publish one `kind: drill` message per condition straight to the pager topic.
#                    That exercises the topic, its subscription, the relay and the configured adapter.
#   --conditions     default: all five (train_stalled, leader_crash_loop, deploy_failed,
#                    storage_or_iops, github_budget_exhausted).
#   --no-summary     skip the one `drill_summary` message sent after the per-condition verdict.
#   --allow-fallback count a delivery by the email fallback as a pass (for a vendor-outage drill).
#   --dry-run        print the messages that would be published; publish nothing.
#
# Environment: ENVIRONMENT (default staging), AWS_REGION (default us-east-1).
#
# A drill emails (and, after the vendor phase, pages) the operator: announce it first with an
# `operator-notify` finding. It changes no state: the messages are delivered and recorded, nothing
# else moves.
#
# Never pass --profile. The script refuses to run unless `aws sts get-caller-identity` reports
# account 047719635665, the account of the tenant this pager belongs to
# (production-and-cost `aws-account-is-per-tenant`).
#
# Exit codes: 0 every condition (and the summary) accepted; 1 not accepted before the timeout;
#             2 bad usage; 3 the AWS account is not 047719635665 or cannot be read.
set -euo pipefail

EXPECTED_ACCOUNT="047719635665"
ENVIRONMENT="${ENVIRONMENT:-staging}"
REGION="${AWS_REGION:-us-east-1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="$HERE/operator_pager_drill_check.py"
ALL_CONDITIONS="train_stalled,leader_crash_loop,deploy_failed,storage_or_iops,github_budget_exhausted"

target=""
conditions="$ALL_CONDITIONS"
timeout_s=300
summary=1
allow_fallback=""
dry_run=0

die() { echo "operator-pager-drill: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --target) target="${2:-}"; shift 2 ;;
    --conditions) conditions="${2:-}"; shift 2 ;;
    --timeout) timeout_s="${2:-}"; shift 2 ;;
    --no-summary) summary=0; shift ;;
    --allow-fallback) allow_fallback="--allow-fallback"; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ "$target" = "relay" ] || die "--target relay is the only target in this build"
case "$timeout_s" in ''|*[!0-9]*) die "--timeout must be a whole number of seconds" ;; esac

account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
if [ "$account" != "$EXPECTED_ACCOUNT" ]; then
  echo "operator-pager-drill: AWS account is '${account:-unreadable}', not $EXPECTED_ACCOUNT; refusing" >&2
  exit 3
fi

topic_arn="arn:aws:sns:${REGION}:${account}:qontinui-${ENVIRONMENT}-operator-pager"
log_group="/aws/lambda/qontinui-${ENVIRONMENT}-pager-relay"
drill_id="drill-$(date -u +%Y%m%dT%H%M%SZ)-$$"
start_ms=$(( ($(date +%s) - 5) * 1000 ))

# Build each message with json.dumps, never by string pasting.
message() { # kind condition what
  python3 -I -c 'import json,sys
kind, cond, what, drill = sys.argv[1:5]
doc = {"page_version": 1, "kind": kind, "source": "drill", "title": "Pager drill", "what": what, "drill_id": drill}
if cond:
    doc["condition"] = cond
print(json.dumps(doc))' "$1" "$2" "$3" "$drill_id"
}

publish() { # json
  if [ "$dry_run" -eq 1 ]; then
    echo "would publish to $topic_arn: $1"
  else
    aws sns publish --region "$REGION" --topic-arn "$topic_arn" --message "$1" --query MessageId --output text >/dev/null
  fi
}

echo "drill $drill_id -> $topic_arn (conditions: $conditions)"
IFS=',' read -r -a conds <<<"$conditions"
for c in "${conds[@]}"; do
  publish "$(message drill "$c" "DRILL $drill_id: a test message published by scripts/operator-pager-drill.sh to check that a $c page reaches the operator.")"
done
[ "$dry_run" -eq 1 ] && exit 0

# One blocking wait per verdict: read the relay's records for this drill until all conditions are
# accepted or the timeout passes.
await() { # conditions-csv
  local deadline=$(( $(date +%s) + timeout_s ))
  local out rc report
  report="$(mktemp)"
  while :; do
    out="$(aws logs filter-log-events --region "$REGION" --log-group-name "$log_group" \
             --start-time "$start_ms" --filter-pattern "{ \$.drill_id = \"$drill_id\" }" \
             --output json 2>/dev/null || echo '{"events":[]}')"
    rc=0
    printf '%s' "$out" | python3 -I "$CHECK" --drill-id "$drill_id" --conditions "$1" $allow_fallback >"$report" 2>&1 || rc=$?
    if [ "$rc" -eq 0 ] || [ "$(date +%s)" -ge "$deadline" ]; then
      cat "$report"; rm -f "$report"
      return "$rc"
    fi
    sleep 15
  done
}

rc=0
await "$conditions" || rc=$?
if [ "$summary" -eq 1 ]; then
  verdict_word="accepted"; [ "$rc" -eq 0 ] || verdict_word="NOT all accepted"
  publish "$(message drill_summary "" "Drill $drill_id finished: ${#conds[@]} condition(s), $verdict_word. Per-condition outcomes are in the relay's log group $log_group.")"
  src=0
  await "summary" || src=$?
  [ "$src" -eq 0 ] || rc=1
fi
exit "$rc"
