# CloudWatch alarms for coord — verification mechanism, not manual
# watching. Uses always-on ALB metrics (Container Insights is disabled
# on the cluster for cost). All alarms notify the shared SNS topic.

variable "environment" { type = string }
variable "alb_arn_suffix" { type = string }
variable "coord_tg_arn_suffix" { type = string }
variable "sns_topic_arn" { type = string }

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

output "alarm_names" {
  value = [
    aws_cloudwatch_metric_alarm.coord_unhealthy.alarm_name,
    aws_cloudwatch_metric_alarm.coord_5xx.alarm_name,
    aws_cloudwatch_metric_alarm.coord_latency.alarm_name,
  ]
}
