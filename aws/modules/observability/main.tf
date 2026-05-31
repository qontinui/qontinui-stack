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

output "alarm_names" {
  value = [
    aws_cloudwatch_metric_alarm.coord_unhealthy.alarm_name,
    aws_cloudwatch_metric_alarm.coord_5xx.alarm_name,
    aws_cloudwatch_metric_alarm.coord_latency.alarm_name,
    aws_cloudwatch_metric_alarm.coord_cpu.alarm_name,
    aws_cloudwatch_metric_alarm.coord_mem.alarm_name,
    aws_cloudwatch_metric_alarm.coord_jetstream_err.alarm_name,
  ]
}
