#!/usr/bin/env bash
# Drill the operator pager path and report, per condition (or per alarm), whether the relay delivered it.
#
# Plan: 2026-10-14-coord-pages-the-operator-for-five-conditions-and-names-the-agent-fixing-each
#   Phase 1: --target relay.  Phase 2: --target alarms and --target rules.  (coord's own leg arrives later.)
#
# Usage:
#   scripts/operator-pager-drill.sh --target relay|alarms|rules [--conditions c1,c2] [--timeout SECONDS]
#                                   [--no-summary] [--allow-fallback] [--dry-run]
#
#   --target relay   one `kind: drill` message per condition, published straight to the pager topic. That
#                    exercises the topic, its subscription, the relay and the configured adapter.
#   --target alarms  each pager alarm through its real alarm action. The two composites are drilled
#                    through their `-drill` children, NEVER directly: a composite set by SetAlarmState is
#                    not guaranteed to return to its actual state (plan D16). pager-rds-free-storage is a
#                    metric alarm and is set directly. Every set carries the reason "DRILL <id>". Metric
#                    alarms return to their actual state within seconds and the composites follow;
#                    afterwards every alarm must read its pre-drill state again.
#   --target rules   each EventBridge rule through PutEvents with source qontinui.drill (PutEvents cannot
#                    forge aws.* sources). deploy-failed and rds-storage must reach the relay. ONE task-exit
#                    event must land in the crash-loop log group while CoordAbnormalTaskExits stays at 0:
#                    that proves the rule-to-log link without faking a crash loop.
#   --conditions     relay target only; default all five.
#   --no-summary     skip the one `drill_summary` message sent after the verdict.
#   --allow-fallback count a delivery by the email fallback as a pass (for a vendor-outage drill).
#   --dry-run        print what would be done; change nothing.
#
# Environment: ENVIRONMENT (default staging), AWS_REGION (default us-east-1).
#
# A drill emails (and, after the vendor phase, pages) the operator: announce it first with an
# `operator-notify` finding. It is state-neutral: messages are delivered and recorded, alarms return to
# their own state, and the drill event is excluded from the crash-loop count.
#
# Never pass --profile. The script refuses to run unless `aws sts get-caller-identity` reports account
# 047719635665, the account of the tenant this pager belongs to (production-and-cost
# `aws-account-is-per-tenant`).
#
# Exit codes: 0 every drilled path accepted (and state restored); 1 not accepted, or an alarm did not
#             return to its state, before the timeout; 2 bad usage; 3 the AWS account is not
#             047719635665 or cannot be read; 4 a pager alarm is already in ALARM (a real incident).
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
indent() { while IFS= read -r line; do printf '  %s\n' "$line"; done; }

while [ $# -gt 0 ]; do
  case "$1" in
    --target) target="${2:-}"; shift 2 ;;
    --conditions) conditions="${2:-}"; shift 2 ;;
    --timeout) timeout_s="${2:-}"; shift 2 ;;
    --no-summary) summary=0; shift ;;
    --allow-fallback) allow_fallback="--allow-fallback"; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) sed -n '2,42p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

case "$target" in relay|alarms|rules) ;; *) die "--target must be relay, alarms or rules" ;; esac
case "$timeout_s" in ''|*[!0-9]*) die "--timeout must be a whole number of seconds" ;; esac

account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
if [ "$account" != "$EXPECTED_ACCOUNT" ]; then
  echo "operator-pager-drill: AWS account is '${account:-unreadable}', not $EXPECTED_ACCOUNT; refusing" >&2
  exit 3
fi

prefix="qontinui-${ENVIRONMENT}-pager-"
topic_arn="arn:aws:sns:${REGION}:${account}:qontinui-${ENVIRONMENT}-operator-pager"
log_group="/aws/lambda/qontinui-${ENVIRONMENT}-pager-relay"
exits_log_group="/aws/events/${prefix}coord-task-exits"
cluster="qontinui-${ENVIRONMENT}"
cluster_arn="arn:aws:ecs:${REGION}:${account}:cluster/${cluster}"
coord_service_arn="arn:aws:ecs:${REGION}:${account}:service/${cluster}/coord"
db_instance="qontinui-${ENVIRONMENT}"
crash_loop="${prefix}coord-crash-loop"
saturation="${prefix}rds-saturation"
free_storage="${prefix}rds-free-storage"
drill_id="drill-$(date -u +%Y%m%dT%H%M%SZ)-$$"
start_epoch=$(( $(date +%s) - 5 ))
start_ms=$(( start_epoch * 1000 ))

# Every JSON document is built with json.dumps, never by string pasting.
doc() { # kind condition what
  python3 -I -c 'import json,sys
kind, cond, what, drill = sys.argv[1:5]
d = {"page_version": 1, "kind": kind, "source": "drill", "title": "Pager drill", "what": what, "drill_id": drill}
if cond:
    d["condition"] = cond
print(json.dumps(d))' "$1" "$2" "$3" "$drill_id"
}

run() { # command...  (printed instead of run under --dry-run)
  if [ "$dry_run" -eq 1 ]; then echo "would run: $*"; else "$@"; fi
}

publish() { # json
  run aws sns publish --region "$REGION" --topic-arn "$topic_arn" --message "$1" --query MessageId --output text >/dev/null
}

# One blocking wait per verdict: read the relay's records for this drill until every key is accepted or
# the timeout passes.
await() { # keys-csv key-field
  local deadline=$(( $(date +%s) + timeout_s ))
  local out rc report
  report="$(mktemp)"
  while :; do
    out="$(aws logs filter-log-events --region "$REGION" --log-group-name "$log_group" \
             --start-time "$start_ms" --filter-pattern "{ \$.drill_id = \"$drill_id\" }" \
             --output json 2>/dev/null || echo '{"events":[]}')"
    rc=0
    printf '%s' "$out" | python3 -I "$CHECK" --drill-id "$drill_id" --conditions "$1" --key "$2" $allow_fallback >"$report" 2>&1 || rc=$?
    if [ "$rc" -eq 0 ] || [ "$(date +%s)" -ge "$deadline" ]; then
      cat "$report"; rm -f "$report"
      return "$rc"
    fi
    sleep 15
  done
}

alarm_states() { # names... -> "name=STATE" lines, sorted
  aws cloudwatch describe-alarms --region "$REGION" --alarm-types MetricAlarm CompositeAlarm \
    --alarm-names "$@" --output json |
    python3 -I -c 'import json,sys
d = json.load(sys.stdin)
rows = [a["AlarmName"] + "=" + a["StateValue"] for a in d.get("MetricAlarms", []) + d.get("CompositeAlarms", [])]
print("\n".join(sorted(rows)))'
}

put_event() { # source detail-type detail-json [resource]
  local entries
  entries="$(python3 -I -c 'import json,sys
src, dtype, detail = sys.argv[1:4]
e = {"Source": src, "DetailType": dtype, "Detail": detail}
if len(sys.argv) > 4 and sys.argv[4]:
    e["Resources"] = [sys.argv[4]]
print(json.dumps([e]))' "$@")"
  if [ "$dry_run" -eq 1 ]; then echo "would put-events: $entries"; return 0; fi
  local failed
  failed="$(aws events put-events --region "$REGION" --entries "$entries" --query FailedEntryCount --output text)"
  [ "$failed" = "0" ] || { echo "operator-pager-drill: put-events failed for $2" >&2; return 1; }
}

rc=0
case "$target" in
  relay)
    echo "drill $drill_id -> $topic_arn (relay; conditions: $conditions)"
    IFS=',' read -r -a conds <<<"$conditions"
    for c in "${conds[@]}"; do
      publish "$(doc drill "$c" "DRILL $drill_id: a test message published by scripts/operator-pager-drill.sh to check that a $c page reaches the operator.")"
    done
    [ "$dry_run" -eq 1 ] && exit 0
    await "$conditions" condition || rc=$?
    ;;

  alarms)
    names=("$crash_loop" "$saturation" "$free_storage" "${crash_loop}-drill" "${saturation}-drill")
    if [ "$dry_run" -eq 0 ]; then
      before="$(alarm_states "${names[@]}")"
      echo "pre-drill states:"; indent <<<"$before"
      if echo "$before" | grep -q "=ALARM$"; then
        echo "operator-pager-drill: a pager alarm is already in ALARM (a real incident?); not drilling" >&2
        exit 4
      fi
    fi
    echo "drill $drill_id -> alarms (composites via their -drill children)"
    for a in "${crash_loop}-drill" "${saturation}-drill" "$free_storage"; do
      run aws cloudwatch set-alarm-state --region "$REGION" --alarm-name "$a" --state-value ALARM --state-reason "DRILL $drill_id"
    done
    [ "$dry_run" -eq 1 ] && exit 0
    await "$crash_loop,$free_storage,$saturation" alarm || rc=$?
    # State-neutral: every alarm, composites included, must read its pre-drill state again.
    deadline=$(( $(date +%s) + timeout_s ))
    while :; do
      after="$(alarm_states "${names[@]}")"
      [ "$after" = "$before" ] && break
      if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "operator-pager-drill: alarms did not return to their pre-drill states:" >&2
        indent <<<"$after" >&2
        rc=1; break
      fi
      sleep 15
    done
    echo "post-drill states:"; alarm_states "${names[@]}" | indent
    ;;

  rules)
    echo "drill $drill_id -> rules (source qontinui.drill)"
    put_event qontinui.drill "ECS Deployment State Change" \
      "$(python3 -I -c 'import json,sys; print(json.dumps({"eventName": "SERVICE_DEPLOYMENT_FAILED", "deploymentId": sys.argv[1], "reason": "DRILL " + sys.argv[1]}))' "$drill_id")" \
      "$coord_service_arn"
    put_event qontinui.drill "RDS DB Instance Event" \
      "$(python3 -I -c 'import json,sys; print(json.dumps({"SourceIdentifier": sys.argv[2], "EventID": "RDS-EVENT-0225", "Message": "DRILL " + sys.argv[1]}))' "$drill_id" "$db_instance")"
    put_event qontinui.drill "ECS Task State Change" \
      "$(python3 -I -c 'import json,sys; print(json.dumps({"clusterArn": sys.argv[2], "group": "service:coord", "lastStatus": "STOPPED", "stopCode": "EssentialContainerExited", "stoppedReason": "DRILL " + sys.argv[1]}))' "$drill_id" "$cluster_arn")"
    [ "$dry_run" -eq 1 ] && exit 0
    await "deploy_failed,storage_or_iops" condition || rc=$?
    # The crash-loop rule targets a log group, not the topic: the drill event must land there, and
    # the abnormal-exit metric must not move (the metric filter counts aws.ecs events only).
    deadline=$(( $(date +%s) + timeout_s ))
    landed=0
    while [ "$(date +%s)" -lt "$deadline" ]; do
      n="$(aws logs filter-log-events --region "$REGION" --log-group-name "$exits_log_group" \
             --start-time "$start_ms" --filter-pattern "\"DRILL $drill_id\"" --query 'length(events)' --output text 2>/dev/null || echo 0)"
      if [ "${n:-0}" != "0" ] && [ "${n:-0}" != "None" ]; then landed=1; break; fi
      sleep 15
    done
    exits_sum="$(aws cloudwatch get-metric-statistics --region "$REGION" --namespace Qontinui/OperatorPager \
                   --metric-name CoordAbnormalTaskExits --start-time "$start_epoch" --end-time "$(( $(date +%s) + 60 ))" \
                   --period 60 --statistics Sum --query 'sum(Datapoints[].Sum)' --output text 2>/dev/null || echo "unreadable")"
    echo "crash-loop rule: drill event in $exits_log_group: $([ "$landed" -eq 1 ] && echo yes || echo NO); CoordAbnormalTaskExits sum since start: ${exits_sum}"
    [ "$landed" -eq 1 ] || rc=1
    case "$exits_sum" in 0|0.0|None|null) ;; *) echo "operator-pager-drill: the abnormal-exit metric moved or could not be read (${exits_sum})" >&2; rc=1 ;; esac
    ;;
esac

if [ "$summary" -eq 1 ] && [ "$dry_run" -eq 0 ]; then
  verdict_word="accepted"; [ "$rc" -eq 0 ] || verdict_word="NOT all accepted"
  publish "$(doc drill_summary "" "Drill $drill_id ($target) finished: $verdict_word. Per-message outcomes are in the relay's log group $log_group.")"
  src=0
  await "summary" condition || src=$?
  [ "$src" -eq 0 ] || rc=1
fi
exit "$rc"
