# CloudWatch alarms for coord — verification mechanism, not manual
# watching. Uses always-on metrics only: ALB target metrics (5xx, latency,
# healthy-host count) and ECS SERVICE-level CPU/memory (the AWS/ECS namespace
# with ClusterName+ServiceName dims is always-on — it does NOT require
# Container Insights, which stays disabled on the cluster for cost). Plus one
# app-level custom metric coord emits to the QontinuiCoord namespace. All
# alarms notify the shared SNS topic.
#
# These three ECS/app alarms (cpu/mem/jetstream-err) were folded in from a
# hand-created, out-of-band alarm set (topic qontinui-coord-alarms-staging,
# created 2026-05-20) that had ZERO subscribers and therefore notified nobody.
# Bringing them into IaC + onto the subscribed topic is what makes them live.
# (Plan 2026-05-30-qontinui-stack-terraform-state-reconciliation follow-up.)

variable "environment" { type = string }
variable "alb_arn_suffix" { type = string }
variable "coord_tg_arn_suffix" { type = string }
variable "sns_topic_arn" { type = string }
variable "coord_cluster_name" {
  type        = string
  description = "ECS cluster name (AWS/ECS CPU/mem alarm dimension). From module.coord.cluster_name."
}
variable "coord_service_name" {
  type        = string
  description = "ECS service name (AWS/ECS CPU/mem alarm dimension). From module.coord.service_name."
}
variable "coord_log_group_name" {
  type        = string
  description = "coord CloudWatch log group (plan-ingest metric filter source). From module.coord.log_group_name."
}

# coord down: no healthy targets behind the ALB.
resource "aws_cloudwatch_metric_alarm" "coord_unhealthy" {
  alarm_name          = "qontinui-${var.environment}-coord-no-healthy-hosts"
  alarm_description   = "coord has zero healthy ALB targets (service down or failing health checks)"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.coord_tg_arn_suffix
  }

  alarm_actions = [var.sns_topic_arn]
  ok_actions    = [var.sns_topic_arn]
}

# coord erroring: sustained 5xx from the target.
resource "aws_cloudwatch_metric_alarm" "coord_5xx" {
  alarm_name          = "qontinui-${var.environment}-coord-5xx"
  alarm_description   = "coord returning 5xx (>=5 in 5 min)"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.coord_tg_arn_suffix
  }

  alarm_actions = [var.sns_topic_arn]
}

# coord dropping connections: gateway-level 5xx the target-5xx alarm CANNOT see.
# HTTPCode_Target_5XX_Count (coord_5xx above) counts only 5xx STATUS LINES the
# target actually sent. A handler that panics unwinds the tokio worker with no
# CatchPanic layer, so hyper closes the client connection WITHOUT a response
# line — the ALB records that as HTTPCode_ELB_5XX_Count (a 502), NOT a target
# 5xx. So a panic hot-loop is invisible to coord_5xx: the 2026-06-18->07-17
# agent_logs row.get(8) panic emitted ~28k ELB 5xx/hour (~25-30% of ALL coord
# requests) for a MONTH while coord_5xx sat in OK the whole time (target-5xx
# stayed at 2-3/hr). This closes that gap. NOTE: HTTPCode_ELB_5XX_Count has no
# per-target-group dimension and this ALB is shared with the web target group,
# so the alarm is LOAD-BALANCER-WIDE (coord + web) — acceptable, since a gateway
# 5xx storm from either service warrants a page. Threshold sits well above the
# handful of connection resets a rolling deploy produces but far below any real
# gateway-error storm; 2 periods requires it to be sustained.
resource "aws_cloudwatch_metric_alarm" "coord_elb_5xx" {
  alarm_name          = "qontinui-${var.environment}-coord-elb-5xx"
  alarm_description   = "ALB emitting gateway 5xx (>=100 in 5 min for 2 periods): a target is dropping/resetting connections mid-request (classic symptom: a handler panic loop) — INVISIBLE to the target-5xx alarm. Grep the coord log group for 'panicked at'. LB-wide (coord+web share this ALB)."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 100
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }

  alarm_actions = [var.sns_topic_arn]
  ok_actions    = [var.sns_topic_arn]
}

# coord slow: p90 latency sustained high.
resource "aws_cloudwatch_metric_alarm" "coord_latency" {
  alarm_name          = "qontinui-${var.environment}-coord-latency-p90"
  alarm_description   = "coord p90 target response time > 2s for 5 min"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p90"
  period              = 300
  evaluation_periods  = 1
  threshold           = 2
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.coord_tg_arn_suffix
  }

  alarm_actions = [var.sns_topic_arn]
}

# coord task CPU saturated. AWS/ECS service-level metric (always-on, no
# Container Insights needed).
resource "aws_cloudwatch_metric_alarm" "coord_cpu" {
  alarm_name          = "qontinui-${var.environment}-coord-cpu"
  alarm_description   = "coord task CPU > 80% for 5 min"
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 1
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ClusterName = var.coord_cluster_name
    ServiceName = var.coord_service_name
  }

  alarm_actions = [var.sns_topic_arn]
}

# coord task memory saturated. AWS/ECS service-level metric (always-on).
resource "aws_cloudwatch_metric_alarm" "coord_mem" {
  alarm_name          = "qontinui-${var.environment}-coord-mem"
  alarm_description   = "coord task memory > 80% for 5 min"
  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 1
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ClusterName = var.coord_cluster_name
    ServiceName = var.coord_service_name
  }

  alarm_actions = [var.sns_topic_arn]
}

# coord can't publish to JetStream: sustained NATS/JetStream publish errors.
# App-level custom metric coord emits to the QontinuiCoord namespace (no
# dimensions). treat_missing_data=notBreaching so a quiet period (no errors
# emitted) doesn't page.
resource "aws_cloudwatch_metric_alarm" "coord_jetstream_err" {
  alarm_name          = "qontinui-${var.environment}-coord-jetstream-err"
  alarm_description   = "coord JetStream publish errors > 100 in 5 min"
  namespace           = "QontinuiCoord"
  metric_name         = "jetstream_publish_err"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 100
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.sns_topic_arn]
}

# coord plan-ingest worker inert: the worker runs (logs its 60s cycle line on
# the leader) but scans zero rows — the silent-failure mode that left
# plan-ingest a prod no-op (plan 2026-06-10-coord-plan-ingest-prod-noop-fix,
# Phase 3 open item). Count-style metric filter: the cycle line
# `plan_ingest_worker: cycle (scanned=347 transitions=0)` is plain text (not
# JSON / not cleanly space-delimited), so CloudWatch value extraction can't
# parse the scanned number; instead count lines that literally report
# `(scanned=0 `. default_value=0 keeps the metric publishing zeros while
# OTHER coord log traffic flows, so the alarm sits in OK (not
# INSUFFICIENT_DATA) during healthy operation.
resource "aws_cloudwatch_log_metric_filter" "coord_plan_ingest_scanned_zero" {
  name           = "qontinui-${var.environment}-coord-plan-ingest-scanned-zero"
  log_group_name = var.coord_log_group_name
  pattern        = "\"plan_ingest_worker: cycle\" \"(scanned=0 \""

  metric_transformation {
    name          = "plan_ingest_scanned_zero"
    namespace     = "QontinuiCoord"
    value         = "1"
    default_value = "0"
  }
}

# Leader logs ~1 cycle line/min; >=30 zero-scan cycles in an hour = inert for
# (at least) most of that hour while tolerating deploy/leader-turnover gaps.
# LIMITATION (deliberate): a fully-dead worker emits NO cycle lines at all =
# no matches (and, if coord stops logging entirely, missing data) = no alarm
# from this watch. That total-silence mode is covered by the service-level
# alarms above (no-healthy-hosts/5xx); this alarm targets the
# logging-but-scanning-nothing mode specifically. treat_missing_data
# notBreaching matches the repo's other app-level custom-metric alarm
# (jetstream_err).
resource "aws_cloudwatch_metric_alarm" "coord_plan_ingest_inert" {
  alarm_name          = "qontinui-${var.environment}-coord-plan-ingest-inert"
  alarm_description   = "coord plan-ingest worker inert: >=30 cycles with scanned=0 in 60 min (worker logging but scanning nothing)"
  namespace           = "QontinuiCoord"
  metric_name         = "plan_ingest_scanned_zero"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 30
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.sns_topic_arn]
  ok_actions    = [var.sns_topic_arn]
}

# coord PR-merge GitHub-App hydration down: the reconciler has been running
# without an App client past the alert threshold (coord main 5cc2a8d). While
# hydration is down, drift detection + the mergestate heal are dead and
# DIRTY/UNKNOWN-cached PRs can silently freeze (incident coord#880) — a
# service-level-healthy failure mode none of the ALB/ECS alarms above can see.
# Coord's designed alert hook is a one-shot `tracing::error!` line containing
# `pr_merge hydration has been DISABLED (no GitHub App client)` fired once per
# outage after COORD_PR_HYDRATION_DOWN_ALERT_TICKS x 60s. (The companion
# Prometheus gauge `pr_merge_hydration_down_seconds` on /metrics has no
# scraper in this stack — /metrics is token-gated behind the ALB and nothing
# collects it — so the log line is the pageable signal.) Term-match the
# stable literal prefix; default_value=0 keeps the metric publishing zeros
# while other coord log traffic flows, so the alarm sits in OK (not
# INSUFFICIENT_DATA) during healthy operation.
resource "aws_cloudwatch_log_metric_filter" "coord_pr_hydration_down" {
  name           = "qontinui-${var.environment}-coord-pr-hydration-down"
  log_group_name = var.coord_log_group_name
  pattern        = "\"pr_merge hydration has been DISABLED\""

  metric_transformation {
    name          = "pr_merge_hydration_down"
    namespace     = "QontinuiCoord"
    value         = "1"
    default_value = "0"
  }
}

# One matching line in any 5-min window = page. LIMITATION (deliberate): the
# app fires the line ONCE per outage, so the alarm returns to OK one period
# later even if hydration is still down — the OK transition does NOT mean
# recovered (that's why there are no ok_actions here, unlike
# plan_ingest_inert). Treat the page as "go check GET /pr-merge/health
# `hydration_enabled` / GITHUB_APP_* credentials"; recovery resets the app's
# once-per-outage latch so a NEW outage pages again. treat_missing_data
# notBreaching matches the repo's other log-filter alarm.
resource "aws_cloudwatch_metric_alarm" "coord_pr_hydration_down" {
  alarm_name          = "qontinui-${var.environment}-coord-pr-hydration-down"
  alarm_description   = "coord PR-merge GitHub-App hydration DISABLED past threshold: drift detection + mergestate heal dead, PRs can silently freeze. Check GET /pr-merge/health hydration_enabled + GITHUB_APP_* creds. One-shot log line — OK transition is NOT recovery."
  namespace           = "QontinuiCoord"
  metric_name         = "pr_merge_hydration_down"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.sns_topic_arn]
}

# coord worker panicking: a tokio task unwinding on the request path. The
# incident this alarm exists for (agent_logs.rs row.get(8) index drift,
# 2026-06-18->07-17) panicked EVERY POST /agents/:id/log ~8x/second — ~14.5k
# panics/30min, the single loudest signal in this log group — yet nothing
# paged for a MONTH: the panic resets the connection (see coord_elb_5xx above,
# a gateway 5xx not a target 5xx) and the runner producer swallowed the failure
# with no log line and a bounded in-memory queue. A panicking worker is never
# healthy in prod, whatever the endpoint, so term-match the stable Rust panic
# prefix. default_value=0 keeps the metric publishing zeros while other coord
# log traffic flows, so the alarm sits in OK (not INSUFFICIENT_DATA) when
# healthy. Threshold 10/5min ignores an isolated one-off but trips within one
# period on any hot-loop (which runs into the thousands per period).
resource "aws_cloudwatch_log_metric_filter" "coord_worker_panic" {
  name           = "qontinui-${var.environment}-coord-worker-panic"
  log_group_name = var.coord_log_group_name
  pattern        = "\"panicked at\""

  metric_transformation {
    name          = "worker_panic"
    namespace     = "QontinuiCoord"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "coord_worker_panic" {
  alarm_name          = "qontinui-${var.environment}-coord-worker-panic"
  alarm_description   = "coord tokio worker(s) panicking (>=10 panic lines in 5 min): a task is unwinding on the request path — often a panic HOT-LOOP (thousands/period) that resets connections and is invisible to the target-5xx alarm. Grep the coord log group for 'panicked at' to find the file:line."
  namespace           = "QontinuiCoord"
  metric_name         = "worker_panic"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.sns_topic_arn]
  ok_actions    = [var.sns_topic_arn]
}

output "alarm_names" {
  value = [
    aws_cloudwatch_metric_alarm.coord_unhealthy.alarm_name,
    aws_cloudwatch_metric_alarm.coord_5xx.alarm_name,
    aws_cloudwatch_metric_alarm.coord_elb_5xx.alarm_name,
    aws_cloudwatch_metric_alarm.coord_latency.alarm_name,
    aws_cloudwatch_metric_alarm.coord_cpu.alarm_name,
    aws_cloudwatch_metric_alarm.coord_mem.alarm_name,
    aws_cloudwatch_metric_alarm.coord_jetstream_err.alarm_name,
    aws_cloudwatch_metric_alarm.coord_plan_ingest_inert.alarm_name,
    aws_cloudwatch_metric_alarm.coord_pr_hydration_down.alarm_name,
    aws_cloudwatch_metric_alarm.coord_worker_panic.alarm_name,
  ]
}
