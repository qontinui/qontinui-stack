# ─── The AWS-side pager alarms (plan Phase 2; decisions D9, D10, D11, D16) ──
#
# Three pager alarms. Each pages on its own transition; none pages per child:
#   pager-coord-crash-loop  composite: abnormal coord task exits OR no healthy coord target (or its drill child)
#   pager-rds-free-storage  metric:    FreeStorageSpace below 5 GB for 15 minutes
#   pager-rds-saturation    composite: an acute database regime OR (IOPS saturated AND coord slow) (or its drill child)
#
# CHILD ALARMS CARRY NO ACTIONS. Only the two composites and pager-rds-free-storage have AlarmActions and
# OKActions on the pager topic, so one episode produces one page.
#
# Missing data renders UNKNOWN, never ALARM: every metric child uses treat_missing_data = "missing",
# so an unreadable metric is INSUFFICIENT_DATA, which a composite does not count as ALARM
# (verification-and-evidence `an-unknown-input-must-not-fire-a-detector`). The exception is a count
# of discrete events emitted only when one happens (abnormal exits): there, no data is zero.
#
# DRILLS NEVER TOUCH A COMPOSITE (D16). AWS: a composite set by SetAlarmState "is not guaranteed to return
# to its actual state". Each composite carries a `-drill` child that watches a metric nothing publishes
# (notBreaching, so its real state is OK); scripts/operator-pager-drill.sh sets THAT child to ALARM with
# reason `DRILL <id>`. Metric alarms return to their actual state within seconds and the composite
# follows. The relay recognises the drill from the triggering child.
#
# Thresholds are backtested (plan "Measured" B; this PR's body has the tables):
#   - acute: ~1.5x the 14-day maxima (ReadLatency max 16.7 ms, WriteLatency max 41.4 ms, DiskQueueDepth
#     avg max 48.4) -> 0 pages in 14 days;
#   - harm: IOPS >= 95% of provisioned AND coord ALB p90 > 2 s, 15 minutes -> 1 page in 14 days. A pure
#     IOPS rule would page 107 times: the volume is chronically saturated, and coord's p90 does not
#     degrade with it (r = -0.29), so chronic saturation stays agent work and is not paged;
#   - crash loop: >= 3 abnormal exits in a 3-hour period -> 3 pages in 30 days on the log-stream proxy
#     (09-22, 10-07, 10-10), replayed as CloudWatch evaluates it.

locals {
  crash_loop_name     = "${local.pager_prefix}coord-crash-loop"
  free_storage_name   = "${local.pager_prefix}rds-free-storage"
  saturation_name     = "${local.pager_prefix}rds-saturation"
  abnormal_exits_name = "${local.pager_prefix}coord-abnormal-exits"
  no_healthy_name     = "${local.pager_prefix}coord-no-healthy-hosts"
  read_latency_name   = "${local.pager_prefix}rds-read-latency"
  write_latency_name  = "${local.pager_prefix}rds-write-latency"
  queue_depth_name    = "${local.pager_prefix}rds-queue-depth"
  iops_saturated_name = "${local.pager_prefix}rds-iops-saturated"
  coord_slow_name     = "${local.pager_prefix}coord-latency-sustained"

  # The relay's view of the three pager alarms: what condition each pages, its title, and the automatic
  # remediation in force. "Agent dispatch follows from coord": an AWS page goes out while coord may be
  # down, so it names the automatic remediation; coord's update names the agent (plan D15).
  pager_alarm_entries = {
    (local.crash_loop_name) = {
      condition   = "leader_crash_loop"
      title       = "coord tasks are exiting abnormally, or no coord target is healthy"
      remediation = "ECS replaces exited tasks and the leader lease fails over; agent dispatch follows from coord"
      drill_child = "${local.crash_loop_name}-drill"
    }
    (local.free_storage_name) = {
      condition   = "storage_or_iops"
      title       = "Database free storage is below 5 GB"
      remediation = "RDS storage autoscaling, up to the maximum allocated storage; agent dispatch follows from coord"
      drill_child = ""
    }
    (local.saturation_name) = {
      condition   = "storage_or_iops"
      title       = "Database IOPS saturation is hurting: latency or queue depth past the 14-day maxima, or saturation while coord is slow"
      remediation = "nothing automatic; agent dispatch follows from coord"
      drill_child = "${local.saturation_name}-drill"
    }
  }
}

data "aws_db_instance" "postgres" {
  db_instance_identifier = var.postgres_instance_identifier
}

# ─── Drill children (one per composite; see the header) ─────────────────

resource "aws_cloudwatch_metric_alarm" "drill" {
  for_each = toset([local.crash_loop_name, local.saturation_name])

  alarm_name          = "${each.key}-drill"
  alarm_description   = "Drill child of ${each.key}: watches a metric nothing publishes, so its real state is OK. scripts/operator-pager-drill.sh sets it to ALARM with reason 'DRILL <id>'; never set the composite itself."
  namespace           = "Qontinui/OperatorPager"
  metric_name         = "DrillChildNeverReports"
  dimensions          = { Alarm = each.key }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}

# ─── coord crash loop ───────────────────────────────────────────────────

# Counts aws.ecs-sourced abnormal exits only (events.tf: rule -> log group -> metric filter). A 3-hour
# period with Sum >= 3 replays to 3 pages in 30 days (tumbling periods; 4 if evaluation slides), and
# reproduces the 2026-09-22, 10-07 and 10-10 episodes. A fast loop crosses 3 within minutes of its
# third exit, because the period's sum is evaluated as data arrives.
resource "aws_cloudwatch_metric_alarm" "abnormal_exits" {
  alarm_name          = local.abnormal_exits_name
  alarm_description   = "Child of ${local.crash_loop_name}: at least 3 coord tasks stopped abnormally (EssentialContainerExited, TaskFailedToStart, failed health checks) within a 3-hour period. No actions: the composite pages."
  namespace           = "Qontinui/OperatorPager"
  metric_name         = aws_cloudwatch_log_metric_filter.coord_abnormal_exits.metric_transformation[0].name
  statistic           = "Sum"
  period              = 10800
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching" # no exit event = no exits
}

# Not `breaching` like the email alarm (observability/main.tf): no datapoint is UNKNOWN, and an
# unknown input must not fire a page. A coord with no registered target stays covered: tasks that never
# become healthy stop with TaskFailedToStart or EssentialContainerExited, which the exits child counts.
resource "aws_cloudwatch_metric_alarm" "no_healthy_hosts" {
  alarm_name          = local.no_healthy_name
  alarm_description   = "Child of ${local.crash_loop_name}: no healthy coord target for 3 minutes. No actions: the composite pages."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"
  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.coord_tg_arn_suffix
  }
}

resource "aws_cloudwatch_composite_alarm" "crash_loop" {
  alarm_name        = local.crash_loop_name
  alarm_description = "PAGER (leader_crash_loop): coord tasks are exiting abnormally, or no coord target is healthy. Pages qontinui-${var.environment}-operator-pager; the relay renders it."
  alarm_rule = join(" OR ", [
    "ALARM(\"${aws_cloudwatch_metric_alarm.abnormal_exits.alarm_name}\")",
    "ALARM(\"${aws_cloudwatch_metric_alarm.no_healthy_hosts.alarm_name}\")",
    "ALARM(\"${aws_cloudwatch_metric_alarm.drill[local.crash_loop_name].alarm_name}\")",
  ])
  alarm_actions = [aws_sns_topic_policy.pager.arn]
  ok_actions    = [aws_sns_topic_policy.pager.arn]
}

# ─── Database free storage ──────────────────────────────────────────────

# A metric alarm, so it is drilled directly: its own state reason carries the DRILL marker.
resource "aws_cloudwatch_metric_alarm" "free_storage" {
  alarm_name          = local.free_storage_name
  alarm_description   = "PAGER (storage_or_iops): RDS ${var.postgres_instance_identifier} has under 5 GB free for 15 minutes, about 3 days of runway at the measured 1.56 GB/day. Autoscaling keeps routine lows near 10 GB, so this fires when autoscaling cannot keep up or is at its maximum."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 5000000000
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"
  dimensions = {
    DBInstanceIdentifier = var.postgres_instance_identifier
  }
  alarm_actions = [aws_sns_topic_policy.pager.arn]
  ok_actions    = [aws_sns_topic_policy.pager.arn]
}

# ─── Database saturation that hurts ─────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "rds_acute" {
  for_each = {
    (local.read_latency_name)  = { metric = "ReadLatency", threshold = 0.025, what = "read latency at least 25 ms" }
    (local.write_latency_name) = { metric = "WriteLatency", threshold = 0.05, what = "write latency at least 50 ms" }
    (local.queue_depth_name)   = { metric = "DiskQueueDepth", threshold = 64, what = "disk queue depth at least 64" }
  }

  alarm_name          = each.key
  alarm_description   = "Child of ${local.saturation_name}: ${each.value.what} for 15 minutes, past the 14-day maximum. No actions: the composite pages."
  namespace           = "AWS/RDS"
  metric_name         = each.value.metric
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "missing"
  dimensions = {
    DBInstanceIdentifier = var.postgres_instance_identifier
  }
}

resource "aws_cloudwatch_metric_alarm" "iops_saturated" {
  alarm_name          = local.iops_saturated_name
  alarm_description   = "Child of ${local.saturation_name}: read plus write IOPS at least 95% of the ${data.aws_db_instance.postgres.iops} provisioned, for 15 minutes. Pages only together with coord being slow. No actions."
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 0.95
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "missing"

  metric_query {
    id          = "saturation"
    expression  = "(read + write) / ${data.aws_db_instance.postgres.iops}"
    label       = "IOPS share of provisioned"
    return_data = true
  }
  metric_query {
    id = "read"
    metric {
      namespace   = "AWS/RDS"
      metric_name = "ReadIOPS"
      period      = 300
      stat        = "Average"
      dimensions  = { DBInstanceIdentifier = var.postgres_instance_identifier }
    }
  }
  metric_query {
    id = "write"
    metric {
      namespace   = "AWS/RDS"
      metric_name = "WriteIOPS"
      period      = 300
      stat        = "Average"
      dimensions  = { DBInstanceIdentifier = var.postgres_instance_identifier }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "coord_slow" {
  alarm_name          = local.coord_slow_name
  alarm_description   = "Child of ${local.saturation_name}: coord's ALB p90 response time above 2 s for 15 minutes. Pages only together with IOPS saturation. No actions."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p90"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 2
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.coord_tg_arn_suffix
  }
}

resource "aws_cloudwatch_composite_alarm" "saturation" {
  alarm_name        = local.saturation_name
  alarm_description = "PAGER (storage_or_iops): database IOPS saturation is hurting. Pages qontinui-${var.environment}-operator-pager; the relay renders it."
  alarm_rule = join(" OR ", concat(
    [for name in sort(keys(aws_cloudwatch_metric_alarm.rds_acute)) : "ALARM(\"${aws_cloudwatch_metric_alarm.rds_acute[name].alarm_name}\")"],
    [
      "(ALARM(\"${aws_cloudwatch_metric_alarm.iops_saturated.alarm_name}\") AND ALARM(\"${aws_cloudwatch_metric_alarm.coord_slow.alarm_name}\"))",
      "ALARM(\"${aws_cloudwatch_metric_alarm.drill[local.saturation_name].alarm_name}\")",
    ],
  ))
  alarm_actions = [aws_sns_topic_policy.pager.arn]
  ok_actions    = [aws_sns_topic_policy.pager.arn]
}
