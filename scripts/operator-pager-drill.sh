#!/usr/bin/env bash
# Drill the operator pager end to end, state-neutrally, and read the result back.
#
# Usage:
#   scripts/operator-pager-drill.sh --target relay|alarms|rules|all
#                                   [--env staging] [--region us-east-1]
#                                   [--drill-id <id>] [--wait-secs 300] [--dry-run]
#
# Plan: qontinui-dev-notes plans/2026-10-14-coord-pages-the-operator-for-five-
# conditions-and-names-the-agent-fixing-each.md, Phases 1 and 2 (D16).
#
# Targets:
#   relay   (Phase 1) publish one `drill` message per condition straight to
#           qontinui-<env>-operator-pager, then read the relay's records for the
#           drill id. Exercises topic -> relay -> adapter.
#   alarms  (Phase 2) set the two `-drill` CHILD alarms and pager-rds-free-storage
#           to ALARM with reason "DRILL <id>". NEVER a composite: SetAlarmState on
#           a composite "is not guaranteed to return to its actual state", and a
#           composite left in ALARM sends no action for a real incident (D16).
#           Then read the relay records and wait until every pager alarm,
#           composites included, is back in the state it had before the drill.
#   rules   (Phase 2) put `qontinui.drill` events matching the deploy-failed and
#           RDS-storage rules (relay records expected), and ONE task-exit event
#           matching the crash-loop rule (it must land in the task-exits log
#           group while the abnormal-exits metric does not move).
#   all     relay, then alarms, then rules.
# Every target ends with one `drill_summary` page.
#
# Exit codes: 0 every drilled signal read `accepted` (and, for alarms, every
# alarm is back in its pre-drill state); 1 a drill did not read back; 2 usage;
# 3 wrong AWS account.
#
# SAFETY. Each drill sends the operator email (five `[PAGE] DRILL` emails for
# `relay`), so announce it first through an `operator-notify` finding. The
# account check runs before any AWS call that writes (production-and-cost
# aws-account-is-per-tenant). --dry-run makes no AWS call at all and prints
# what would be sent.

set -euo pipefail

# Git Bash rewrites a leading-slash argument (a log group name) into a Windows
# path unless told not to.
export MSYS_NO_PATHCONV=1

EXPECTED_ACCOUNT="047719635665"
CONDITIONS=(train_stalled leader_crash_loop deploy_failed storage_or_iops github_budget_exhausted)

TARGET=""
ENVIRONMENT="staging"
REGION="us-east-1"
DRILL_ID=""
WAIT_SECS=300
DRY_RUN=0

usage() { sed -n '2,8p' "$0" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --env) ENVIRONMENT="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --drill-id) DRILL_ID="${2:-}"; shift 2 ;;
    --wait-secs) WAIT_SECS="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
case "$TARGET" in relay|alarms|rules|all) ;; *) usage ;; esac

PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "python3 is required to build and read JSON" >&2; exit 2; }

[ -n "$DRILL_ID" ] || DRILL_ID="d$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="qontinui-${ENVIRONMENT}"
PAGER="${PREFIX}-pager-"
RELAY_LOG="/aws/lambda/${PREFIX}-operator-pager-relay"
EXITS_LOG="/aws/events/${PREFIX}-coord-task-exits"
COMPOSITES=("${PAGER}coord-crash-loop" "${PAGER}rds-saturation")
DRILL_CHILDREN=("${PAGER}coord-crash-loop-drill" "${PAGER}rds-saturation-drill")
FREE_STORAGE="${PAGER}rds-free-storage"

aws_() { aws --region "$REGION" --output json "$@"; }

run() {
  if [ "$DRY_RUN" = 1 ]; then
    { printf 'DRY-RUN:'; printf ' %q' "$@"; printf '\n'; } >&2
  else
    "$@"
  fi
}

# ─── Account check FIRST ────────────────────────────────────────────────
ACCOUNT="$EXPECTED_ACCOUNT"
if [ "$DRY_RUN" = 0 ]; then
  ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
  if [ "$ACCOUNT" != "$EXPECTED_ACCOUNT" ]; then
    echo "REFUSED: caller is in account $ACCOUNT, not $EXPECTED_ACCOUNT (aws-account-is-per-tenant)" >&2
    exit 3
  fi
fi
TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT}:${PREFIX}-operator-pager"
CLUSTER_ARN="arn:aws:ecs:${REGION}:${ACCOUNT}:cluster/${PREFIX}"
COORD_SERVICE_ARN="arn:aws:ecs:${REGION}:${ACCOUNT}:service/${PREFIX}/coord"
START_MS="$(( $(date -u +%s) * 1000 ))"
echo "drill ${DRILL_ID}: target=${TARGET} env=${ENVIRONMENT} account=${ACCOUNT} region=${REGION}"

FAILED=0
DRILLED_CONDITIONS=()

# Relay records (one JSON object per line) since START_MS, optionally narrowed.
relay_records() {
  local pattern="$1"
  aws_ logs filter-log-events --log-group-name "$RELAY_LOG" \
    --start-time "$START_MS" --filter-pattern "$pattern" \
    --query 'events[].message' 2>/dev/null || echo '[]'
}

# wait_for_records <filter-pattern> <python predicate over list `recs`> <label>
wait_for_records() {
  local pattern="$1" check="$2" label="$3" deadline=$(( $(date -u +%s) + WAIT_SECS ))
  if [ "$DRY_RUN" = 1 ]; then echo "DRY-RUN: would read $RELAY_LOG for: $label"; return 0; fi
  while :; do
    if relay_records "$pattern" | "$PY" -c '
import json, sys
msgs = json.load(sys.stdin) or []
recs = []
for m in msgs:
    try:
        recs.append(json.loads(m))
    except ValueError:
        pass
ok = '"$check"'
for r in recs:
    print("  record:", r.get("signal"), r.get("condition"), r.get("kind"), r.get("outcome"), file=sys.stderr)
sys.exit(0 if ok else 1)
'; then
      echo "OK: $label"; return 0
    fi
    if [ "$(date -u +%s)" -ge "$deadline" ]; then
      echo "FAIL: $label (no matching accepted record within ${WAIT_SECS}s)" >&2; return 1
    fi
    sleep 15
  done
}

# key=value pairs -> a page_version 1 JSON object (page_version as the integer
# the relay requires; every other value a string).
json_msg() {
  "$PY" -c 'import json,sys; d=dict(a.split("=",1) for a in sys.argv[1:]); d["page_version"]=int(d.get("page_version","1")); print(json.dumps(d))' "$@"
}

# ─── relay ──────────────────────────────────────────────────────────────
drill_relay() {
  echo "== relay: one drill message per condition to $TOPIC_ARN"
  for c in "${CONDITIONS[@]}"; do
    run aws_ sns publish --topic-arn "$TOPIC_ARN" --message "$(json_msg \
      page_version=1 kind=drill origin=drill-script source=qontinui.drill \
      signal=drill-script:relay condition="$c" drill_id="$DRILL_ID" \
      what="Relay drill $DRILL_ID for $c.")" >/dev/null
  done
  local want
  want="$(printf '"%s",' "${CONDITIONS[@]}")"
  if wait_for_records "{ \$.drill_id = \"$DRILL_ID\" && \$.signal = \"drill-script:relay\" }" \
    "set(r[\"condition\"] for r in recs if r.get(\"outcome\") == \"accepted\") >= {${want%,}}" \
    "relay accepted all five conditions"; then
    DRILLED_CONDITIONS+=("${CONDITIONS[@]}")
  else
    FAILED=1
  fi
}

# ─── alarms ─────────────────────────────────────────────────────────────
alarm_states() {
  aws_ cloudwatch describe-alarms --alarm-name-prefix "$PAGER" --alarm-types CompositeAlarm MetricAlarm \
    --query '[MetricAlarms[].[AlarmName,StateValue], CompositeAlarms[].[AlarmName,StateValue]][]' --output text
}

drill_alarms() {
  echo "== alarms: drill children + ${FREE_STORAGE} (never a composite)"
  local before=""
  if [ "$DRY_RUN" = 0 ]; then before="$(alarm_states | sort)"; echo "pre-drill states:"; echo "$before"; fi
  for a in "${DRILL_CHILDREN[@]}" "$FREE_STORAGE"; do
    run aws_ cloudwatch set-alarm-state --alarm-name "$a" --state-value ALARM --state-reason "DRILL $DRILL_ID"
  done
  local names
  names="$(printf '"%s",' "${COMPOSITES[@]}" "$FREE_STORAGE")"
  if wait_for_records "{ \$.drill IS TRUE && \$.kind = \"drill\" }" \
    "{r[\"signal\"] for r in recs if r.get(\"outcome\") == \"accepted\"} >= {${names%,}}" \
    "relay accepted a drill page from both composites and the free-storage alarm"; then
    DRILLED_CONDITIONS+=(leader_crash_loop storage_or_iops)
  else
    FAILED=1
  fi
  # State-neutral read-back: every pager alarm, composites included, back to
  # its pre-drill state (production-and-cost exercising-a-live-surface-is-state-neutral).
  if [ "$DRY_RUN" = 1 ]; then echo "DRY-RUN: would wait for every ${PAGER}* alarm to return to its pre-drill state"; return; fi
  local deadline=$(( $(date -u +%s) + WAIT_SECS )) after
  while :; do
    after="$(alarm_states | sort)"
    if [ "$after" = "$before" ]; then echo "OK: every pager alarm is back in its pre-drill state"; break; fi
    if [ "$(date -u +%s)" -ge "$deadline" ]; then
      echo "FAIL: pager alarms not back in their pre-drill state after ${WAIT_SECS}s:" >&2
      diff <(echo "$before") <(echo "$after") >&2 || true
      FAILED=1; break
    fi
    sleep 15
  done
  echo "alarm history since the drill (ALARM -> OK transitions):"
  for a in "${DRILL_CHILDREN[@]}" "$FREE_STORAGE" "${COMPOSITES[@]}"; do
    aws_ cloudwatch describe-alarm-history --alarm-name "$a" --history-item-type StateUpdate \
      --start-date "$(date -u -d "@$(( START_MS / 1000 ))" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT00:00:00Z)" \
      --query 'AlarmHistoryItems[].[AlarmName,Timestamp,HistorySummary]' --output text || true
  done
}

# ─── rules ──────────────────────────────────────────────────────────────
drill_rules() {
  echo "== rules: qontinui.drill events through the real rules"
  local entries
  entries="$("$PY" - "$DRILL_ID" "$COORD_SERVICE_ARN" "$CLUSTER_ARN" "$PREFIX" <<'PYEOF'
import json, sys
drill, service_arn, cluster_arn, prefix = sys.argv[1:5]
def entry(detail_type, detail, resources=()):
    return {"Source": "qontinui.drill", "DetailType": detail_type,
            "Resources": list(resources), "Detail": json.dumps(detail)}
print(json.dumps([
    entry("ECS Deployment State Change",
          {"eventType": "ERROR", "eventName": "SERVICE_DEPLOYMENT_FAILED",
           "deploymentId": f"drill-{drill}", "reason": f"DRILL {drill} deploy-failed rule drill"},
          [service_arn]),
    entry("RDS DB Instance Event",
          {"EventID": "RDS-EVENT-0225", "SourceIdentifier": prefix, "SourceType": "DB_INSTANCE",
           "Message": f"DRILL {drill} rds-storage rule drill"}),
    entry("ECS Task State Change",
          {"clusterArn": cluster_arn, "group": "service:coord", "lastStatus": "STOPPED",
           "stopCode": "EssentialContainerExited", "stoppedReason": f"DRILL {drill} task-exit rule drill"}),
]))
PYEOF
)"
  run aws_ events put-events --entries "$entries" >/dev/null
  if wait_for_records "{ \$.drill_id = \"$DRILL_ID\" && \$.origin = \"eventbridge\" }" \
    "{r[\"condition\"] for r in recs if r.get(\"outcome\") == \"accepted\"} >= {\"deploy_failed\", \"storage_or_iops\"}" \
    "relay accepted the deploy-failed and rds-storage rule drills"; then
    DRILLED_CONDITIONS+=(deploy_failed storage_or_iops)
  else
    FAILED=1
  fi
  if [ "$DRY_RUN" = 1 ]; then echo "DRY-RUN: would read $EXITS_LOG for the task-exit drill and the CoordAbnormalTaskExits metric"; return; fi
  local deadline=$(( $(date -u +%s) + WAIT_SECS )) n
  while :; do
    n="$(aws_ logs filter-log-events --log-group-name "$EXITS_LOG" --start-time "$START_MS" \
      --filter-pattern "\"DRILL $DRILL_ID\"" --query 'length(events)' --output text 2>/dev/null || echo 0)"
    if [ "${n:-0}" != "0" ] && [ "${n:-0}" != "None" ]; then echo "OK: task-exit drill landed in $EXITS_LOG"; break; fi
    if [ "$(date -u +%s)" -ge "$deadline" ]; then echo "FAIL: task-exit drill not in $EXITS_LOG after ${WAIT_SECS}s" >&2; FAILED=1; break; fi
    sleep 15
  done
  local sum
  sum="$(aws_ cloudwatch get-metric-statistics --namespace Qontinui/OperatorPager --metric-name CoordAbnormalTaskExits \
    --start-time "$(date -u -d "@$(( START_MS / 1000 - 300 ))" +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --period 60 --statistics Sum --query 'sum(Datapoints[].Sum)' --output text 2>/dev/null || echo UNKNOWN)"
  case "$sum" in
    0|0.0|None) echo "OK: CoordAbnormalTaskExits did not move (drill events are excluded)" ;;
    *) echo "FAIL: CoordAbnormalTaskExits read '$sum' during the drill window (a drill must never count; or a real exit coincided)" >&2; FAILED=1 ;;
  esac
}

case "$TARGET" in
  relay) drill_relay ;;
  alarms) drill_alarms ;;
  rules) drill_rules ;;
  all) drill_relay; drill_alarms; drill_rules ;;
esac

# ─── summary ────────────────────────────────────────────────────────────
if [ "${#DRILLED_CONDITIONS[@]}" -gt 0 ]; then
  SUMMARY="$("$PY" - "$DRILL_ID" "$TARGET" "$FAILED" "${DRILLED_CONDITIONS[@]}" <<'PYEOF'
import json, sys
drill, target, failed, *conds = sys.argv[1:]
uniq = sorted(set(conds))
print(json.dumps({"page_version": 1, "kind": "drill_summary", "origin": "drill-script",
                  "source": "qontinui.drill", "signal": "drill-script:summary", "drill_id": drill,
                  "conditions": uniq,
                  "what": f"Target {target}: {len(uniq)} conditions read back accepted; "
                          + ("one or more drills did not read back." if failed == "1" else "every drill read back.")}))
PYEOF
)"
  run aws_ sns publish --topic-arn "$TOPIC_ARN" --message "$SUMMARY" >/dev/null
fi

if [ "$FAILED" = 0 ]; then echo "drill ${DRILL_ID}: PASS"; else echo "drill ${DRILL_ID}: FAIL" >&2; fi
exit "$FAILED"
