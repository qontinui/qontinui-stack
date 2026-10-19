# The AWS-side pager alarms (plan Phase 2; claim r04-storage-and-iops-alarms-page).
#
# Three pager alarms carry actions, and only these three:
#   * pager-coord-crash-loop  — composite (plan D11)
#   * pager-rds-free-storage  — metric alarm (plan D10)
#   * pager-rds-saturation    — composite (plan D9)
# Every CHILD alarm carries NO actions, so one episode produces one page: the
# composite notifies, its children do not.
#
# Drills are state-neutral (plan D16). AWS SetAlarmState on a COMPOSITE "is not
# guaranteed to return to its actual state", and a composite stuck in ALARM sends
# no action for a real incident, so a composite is NEVER drilled directly. Each
# composite instead ORs in a dedicated `<composite>-drill` child: a metric alarm
# on a metric nothing ever publishes, `treat_missing_data = notBreaching`, so its
# real state is OK. A drill sets that child to ALARM ("DRILL <id>"); it returns
# to OK within seconds and the composite follows. The relay reads the triggering
# child from the composite's notification to tell a drill from a real page.
# pager-rds-free-storage is a metric alarm and is drilled directly.
#
# Missing data renders UNKNOWN, never ALARM (verification-and-evidence
# an-unknown-input-must-not-fire-a-detector): every metric child watching a
# real metric uses `missing`, except the abnormal-exits child, whose missing
# data MEANS "no abnormal exit was logged" (see there).

# Provisioned IOPS of the gp3 volume, read rather than restated so the
# saturation threshold follows a storage change without editing this module.
# Read here (data source) rather than as a postgres-module output so the
# postgres module is not edited (plan Phase 2).
data "aws_db_instance" "postgres" {
  db_instance_identifier = var.postgres_instance_identifier
}

locals {
  # RDS reports gp3 baseline IOPS (3,000 below 400 GiB) in `iops`. A null or
  # zero read would make the threshold 0 and alarm forever, so fall back to the
  # measured 3,000 (plan "Measured" B) rather than to zero.
  provisioned_iops         = coalesce(data.aws_db_instance.postgres.iops, 0) > 0 ? data.aws_db_instance.postgres.iops : 3000
  iops_saturated_threshold = floor(local.provisioned_iops * 0.95)

  drill_metric_namespace = "Qontinui/OperatorPager"
  drill_metric_name      = "DrillChildNeverReports"
}

# ─── pager-coord-crash-loop (plan D11) ──────────────────────────────────

# Child 1: abnormal coord task exits, counted from the log group the
# coord-task-exits rule writes (events.tf). Only `source = "aws.ecs"` events
# count; a `qontinui.drill` event reaches the log group but never this metric.
resource "aws_cloudwatch_log_metric_filter" "coord_abnormal_exits" {
  name           = "${local.pager_name_prefix}coord-abnormal-exits"
  log_group_name = aws_cloudwatch_log_group.coord_task_exits.name
  pattern        = "{ $.source = \"aws.ecs\" }"

  metric_transformation {
    name          = "CoordAbnormalTaskExits"
    namespace     = "Qontinui/OperatorPager"
    value         = "1"
    default_value = "0"
  }
}

# >= 3 abnormal exits in one 3-hour period. Proposal from plan D11 (on the
# log-stream proxy it gives 4 episodes in 30 days: 09-22, 10-07 x2, 10-10).
# The period is a TUMBLING 3 h window, which is how CloudWatch evaluates it, so
# the backtest that arms this threshold must replay it the same way, not as a
# sliding window (the two disagree at a period boundary). Re-run that backtest
# against the ECS stop codes before relying on the threshold; it is the verify
# step of Phase 2 and belongs in the PR body.
#
# notBreaching, unlike the RDS children: the log group only receives events
# when a task exits abnormally, so no datapoint here means "zero abnormal
# exits", which is a known value, not an unknown one.
resource "aws_cloudwatch_metric_alarm" "coord_abnormal_exits" {
  alarm_name          = "${local.pager_name_prefix}coord-abnormal-exits"
  alarm_description   = "Child of ${local.alarm_crash_loop} (no actions): >= 3 coord tasks stopped abnormally (EssentialContainerExited, TaskFailedToStart, or failed ELB/container health checks) within one 3-hour period."
  namespace           = "Qontinui/OperatorPager"
  metric_name         = "CoordAbnormalTaskExits"
  statistic           = "Sum"
  period              = 10800
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  depends_on          = [aws_cloudwatch_log_metric_filter.coord_abnormal_exits]
}

# Child 2: the pager copy of no-healthy-hosts. `missing`, NOT the email alarm's
# `breaching` (observability/main.tf): with no target registered the ALB may
# publish no datapoint at all, so this child stays INSUFFICIENT_DATA, and
# child 1 covers that case — tasks that never become healthy stop with
# TaskFailedToStart or EssentialContainerExited.
resource "aws_cloudwatch_metric_alarm" "coord_no_healthy_hosts" {
  alarm_name          = "${local.pager_name_prefix}coord-no-healthy-hosts"
  alarm_description   = "Child of ${local.alarm_crash_loop} (no actions): coord has zero healthy ALB targets for 3 minutes."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"
  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.coord_tg_arn_suffix
  }
}

# Child 3: the drill child (plan D16).
resource "aws_cloudwatch_metric_alarm" "coord_crash_loop_drill" {
  alarm_name          = local.alarm_crash_loop_drill
  alarm_description   = "Drill child of ${local.alarm_crash_loop} (no actions). Watches a metric nothing publishes, so its real state is OK. scripts/operator-pager-drill.sh sets it to ALARM with reason 'DRILL <id>'; never set-alarm-state the composite itself."
  namespace           = local.drill_metric_namespace
  metric_name         = local.drill_metric_name
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_composite_alarm" "coord_crash_loop" {
  alarm_name        = local.alarm_crash_loop
  alarm_description = "PAGER (leader_crash_loop): coord is crash-looping — abnormal task exits, or no healthy ALB target. ECS replaces stopped tasks and the leader lease fails over; agent dispatch follows from coord."
  alarm_rule = join(" OR ", [
    "ALARM(\"${aws_cloudwatch_metric_alarm.coord_abnormal_exits.alarm_name}\")",
    "ALARM(\"${aws_cloudwatch_metric_alarm.coord_no_healthy_hosts.alarm_name}\")",
    "ALARM(\"${aws_cloudwatch_metric_alarm.coord_crash_loop_drill.alarm_name}\")",
  ])
  alarm_actions = [aws_sns_topic_policy.pager.arn]
  ok_actions    = [aws_sns_topic_policy.pager.arn]
}

# ─── pager-rds-free-storage (plan D10) ──────────────────────────────────
# The pager copy of observability's rds-free-storage-low: < 5 GB for 3 x 5 min.
# Never below 5 GB in 30 days; at ~1.56 GB/day, 5 GB is ~3 days of runway.

resource "aws_cloudwatch_metric_alarm" "rds_free_storage" {
  alarm_name          = local.alarm_free_storage
  alarm_description   = "PAGER (storage_or_iops): coord RDS FreeStorageSpace < 5 GB (Minimum) for 15 min — storage autoscaling did not rescue it. RDS storage autoscaling grows the volume up to its maximum; agent dispatch follows from coord."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 5000000000
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"
  dimensions          = { DBInstanceIdentifier = var.postgres_instance_identifier }
  alarm_actions       = [aws_sns_topic_policy.pager.arn]
  ok_actions          = [aws_sns_topic_policy.pager.arn]
}

# ─── pager-rds-saturation (plan D9) ─────────────────────────────────────
# Pages on HARM, not on IOPS: the instance sits at its IOPS ceiling most of the
# day without hurting coord (ALB p90 median 0.167 s during saturation), and a
# pure IOPS rule would have paged 107 times in 14 days. Two branches, OR-ed:
#   acute — ReadLatency >= 25 ms, or WriteLatency >= 50 ms, or DiskQueueDepth
#           >= 64, each for 3 of 3 five-minute periods (~1.5x the 14-day maxima);
#   harm  — total IOPS >= 95% of provisioned AND coord ALB p90 > 2 s, each for
#           3 of 3 periods.
# Backtest (plan D9): 0 acute pages and 1 harm page in 14 days.

resource "aws_cloudwatch_metric_alarm" "rds_read_latency_acute" {
  alarm_name          = "${local.pager_name_prefix}rds-read-latency-acute"
  alarm_description   = "Child of ${local.alarm_saturation} (no actions): RDS ReadLatency >= 25 ms for 15 min."
  namespace           = "AWS/RDS"
  metric_name         = "ReadLatency"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 0.025 # seconds
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "missing"
  dimensions          = { DBInstanceIdentifier = var.postgres_instance_identifier }
}

resource "aws_cloudwatch_metric_alarm" "rds_write_latency_acute" {
  alarm_name          = "${local.pager_name_prefix}rds-write-latency-acute"
  alarm_description   = "Child of ${local.alarm_saturation} (no actions): RDS WriteLatency >= 50 ms for 15 min."
  namespace           = "AWS/RDS"
  metric_name         = "WriteLatency"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 0.05 # seconds
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "missing"
  dimensions          = { DBInstanceIdentifier = var.postgres_instance_identifier }
}

resource "aws_cloudwatch_metric_alarm" "rds_queue_depth_acute" {
  alarm_name          = "${local.pager_name_prefix}rds-queue-depth-acute"
  alarm_description   = "Child of ${local.alarm_saturation} (no actions): RDS DiskQueueDepth >= 64 for 15 min."
  namespace           = "AWS/RDS"
  metric_name         = "DiskQueueDepth"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 64
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "missing"
  dimensions          = { DBInstanceIdentifier = var.postgres_instance_identifier }
}

resource "aws_cloudwatch_metric_alarm" "rds_iops_saturated" {
  alarm_name          = "${local.pager_name_prefix}rds-iops-saturated"
  alarm_description   = "Child of ${local.alarm_saturation} (no actions; AND-ed with coord latency): ReadIOPS + WriteIOPS >= 95% of the ${local.provisioned_iops} provisioned IOPS for 15 min. Alone this is chronic saturation, which is agent work, not a page."
  evaluation_periods  = 3
  threshold           = local.iops_saturated_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "missing"

  metric_query {
    id          = "total"
    expression  = "read + write"
    label       = "ReadIOPS + WriteIOPS"
    return_data = true
  }

  metric_query {
    id = "read"
    metric {
      namespace   = "AWS/RDS"
      metric_name = "ReadIOPS"
      stat        = "Average"
      period      = 300
      dimensions  = { DBInstanceIdentifier = var.postgres_instance_identifier }
    }
  }

  metric_query {
    id = "write"
    metric {
      namespace   = "AWS/RDS"
      metric_name = "WriteIOPS"
      stat        = "Average"
      period      = 300
      dimensions  = { DBInstanceIdentifier = var.postgres_instance_identifier }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "coord_latency_sustained" {
  alarm_name          = "${local.pager_name_prefix}coord-latency-sustained"
  alarm_description   = "Child of ${local.alarm_saturation} (no actions; AND-ed with IOPS saturation): coord ALB p90 TargetResponseTime > 2 s for 15 min."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p90"
  period              = 300
  evaluation_periods  = 3
  threshold           = 2
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.coord_tg_arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_saturation_drill" {
  alarm_name          = local.alarm_saturation_drill
  alarm_description   = "Drill child of ${local.alarm_saturation} (no actions). Watches a metric nothing publishes, so its real state is OK. scripts/operator-pager-drill.sh sets it to ALARM with reason 'DRILL <id>'; never set-alarm-state the composite itself."
  namespace           = local.drill_metric_namespace
  metric_name         = local.drill_metric_name
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_composite_alarm" "rds_saturation" {
  alarm_name        = local.alarm_saturation
  alarm_description = "PAGER (storage_or_iops): coord's database is saturated AND it is hurting — acute RDS latency/queue depth, or IOPS at the provisioned ceiling while coord p90 > 2 s. No automatic remediation; agent dispatch follows from coord."
  alarm_rule = join(" OR ", [
    "ALARM(\"${aws_cloudwatch_metric_alarm.rds_read_latency_acute.alarm_name}\")",
    "ALARM(\"${aws_cloudwatch_metric_alarm.rds_write_latency_acute.alarm_name}\")",
    "ALARM(\"${aws_cloudwatch_metric_alarm.rds_queue_depth_acute.alarm_name}\")",
    "(ALARM(\"${aws_cloudwatch_metric_alarm.rds_iops_saturated.alarm_name}\") AND ALARM(\"${aws_cloudwatch_metric_alarm.coord_latency_sustained.alarm_name}\"))",
    "ALARM(\"${aws_cloudwatch_metric_alarm.rds_saturation_drill.alarm_name}\")",
  ])
  alarm_actions = [aws_sns_topic_policy.pager.arn]
  ok_actions    = [aws_sns_topic_policy.pager.arn]
}
