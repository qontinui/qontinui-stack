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

output "alarm_names" {
  value = [
    aws_cloudwatch_metric_alarm.coord_unhealthy.alarm_name,
    aws_cloudwatch_metric_alarm.coord_5xx.alarm_name,
    aws_cloudwatch_metric_alarm.coord_latency.alarm_name,
    aws_cloudwatch_metric_alarm.coord_cpu.alarm_name,
    aws_cloudwatch_metric_alarm.coord_mem.alarm_name,
    aws_cloudwatch_metric_alarm.coord_jetstream_err.alarm_name,
    aws_cloudwatch_metric_alarm.coord_plan_ingest_inert.alarm_name,
  ]
}
