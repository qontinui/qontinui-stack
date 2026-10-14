# ─── The AWS-side pager rules (plan Phase 2; decisions D10, D11, D12, D16) ──
#
#   pager-coord-task-exits  ECS "Task State Change" for the coord service, abnormal stops only
#                           -> log group -> metric filter -> the crash-loop composite's exits child
#   pager-deploy-failed     ECS "Deployment State Change" SERVICE_DEPLOYMENT_FAILED for coord or web -> topic
#   pager-rds-storage       RDS storage events for the instance -> topic
#
# Every pattern also matches `source: qontinui.drill`, so scripts/operator-pager-drill.sh exercises the
# REAL rule (PutEvents cannot forge an aws.* source). A drill page is recognised by that source.
#
# The input transformers pass every event field as a JSON VALUE, never pasted inside a JSON string, so
# any text an event carries leaves the document valid. The relay builds the page's words from them; an
# RDS event's own text is imperative ("Increase the maximum storage threshold."), so the relay uses
# its own description of each event id instead, and a page never asks.

locals {
  coord_cluster_arn = "arn:${local.partition}:ecs:${local.region}:${local.account}:cluster/${var.coord_cluster_name}"
  service_arns = [
    for svc in [var.coord_service_name, var.web_service_name] :
    "arn:${local.partition}:ecs:${local.region}:${local.account}:service/${var.coord_cluster_name}/${svc}"
  ]
  drill_source = "qontinui.drill"
  rds_storage_event_ids = [
    "RDS-EVENT-0225", # allocated storage approaching the autoscaling maximum (80% of it)
    "RDS-EVENT-0224", # autoscaling will reach or exceed the maximum
    "RDS-EVENT-0223", # autoscaling unable to scale
    "RDS-EVENT-0007", # allocated storage exhausted
    "RDS-EVENT-0089", # free storage below 10% of provisioned
    "RDS-EVENT-0222", # free storage low; shutdown follows below the storage-full threshold
    "RDS-EVENT-0221", # storage-full: the database has been shut down
  ]
}

# ─── coord task exits -> log group -> metric ───────────────────────────

resource "aws_cloudwatch_event_rule" "coord_task_exits" {
  name        = "${local.pager_prefix}coord-task-exits"
  description = "Abnormal coord task stops (feeds the crash-loop pager alarm through a log group). Plan 2026-10-14 operator pager, D11."
  event_pattern = jsonencode({
    source        = ["aws.ecs", local.drill_source]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [local.coord_cluster_arn]
      group      = ["service:${var.coord_service_name}"]
      lastStatus = ["STOPPED"]
      "$or" = [
        { stopCode = ["EssentialContainerExited", "TaskFailedToStart"] },
        { stoppedReason = [{ prefix = "Task failed ELB health checks" }, { prefix = "Task failed container health checks" }] },
      ]
    }
  })
}

resource "aws_cloudwatch_log_group" "coord_task_exits" {
  # EventBridge can target a log group only under /aws/events/.
  name              = "/aws/events/${local.pager_prefix}coord-task-exits"
  retention_in_days = var.relay_log_retention_days
}

# EventBridge writes to a log group only when the group's resource policy admits it.
data "aws_iam_policy_document" "coord_task_exits_log" {
  statement {
    sid     = "EventBridgeWritesCoordTaskExits"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
    resources = ["${aws_cloudwatch_log_group.coord_task_exits.arn}:*"]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.coord_task_exits.arn]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "coord_task_exits" {
  policy_name     = "${local.pager_prefix}coord-task-exits"
  policy_document = data.aws_iam_policy_document.coord_task_exits_log.json
}

resource "aws_cloudwatch_event_target" "coord_task_exits" {
  rule      = aws_cloudwatch_event_rule.coord_task_exits.name
  target_id = "coord-task-exits-log"
  arn       = aws_cloudwatch_log_group.coord_task_exits.arn

  depends_on = [aws_cloudwatch_log_resource_policy.coord_task_exits]
}

# Counts aws.ecs events only. A drill event reaches the log group (the rule admits qontinui.drill) and
# emits the default 0, so a drill proves the rule-to-log link without counting toward a crash loop.
resource "aws_cloudwatch_log_metric_filter" "coord_abnormal_exits" {
  name           = "${local.pager_prefix}coord-abnormal-exits"
  log_group_name = aws_cloudwatch_log_group.coord_task_exits.name
  pattern        = "{ $.source = \"aws.ecs\" }"

  metric_transformation {
    name          = "CoordAbnormalTaskExits"
    namespace     = "Qontinui/OperatorPager"
    value         = "1"
    default_value = "0"
  }
}

# ─── ECS deployment failed (coord, web) -> topic ───────────────────────

resource "aws_cloudwatch_event_rule" "deploy_failed" {
  name        = "${local.pager_prefix}deploy-failed"
  description = "ECS deployment circuit breaker marked a coord or web deployment FAILED (it rolls back). Pages deploy_failed. Plan 2026-10-14 operator pager, D12."
  event_pattern = jsonencode({
    source        = ["aws.ecs", local.drill_source]
    "detail-type" = ["ECS Deployment State Change"]
    resources     = local.service_arns
    detail = {
      eventName = ["SERVICE_DEPLOYMENT_FAILED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "deploy_failed" {
  rule      = aws_cloudwatch_event_rule.deploy_failed.name
  target_id = "operator-pager"
  arn       = aws_sns_topic_policy.pager.arn

  input_transformer {
    input_paths = {
      event_source = "$.source"
      event_id     = "$.id"
      event_time   = "$.time"
      service      = "$.resources[0]"
      deployment   = "$.detail.deploymentId"
      event        = "$.detail.eventName"
      reason       = "$.detail.reason"
    }
    input_template = <<-EOT
      {"page_version": 1, "kind": "page", "condition": "deploy_failed", "source": "eventbridge", "event_source": <event_source>, "event_id": <event_id>, "event_time": <event_time>, "aws": {"service": <service>, "deployment": <deployment>, "event": <event>, "reason": <reason>}, "title": "ECS deployment failed", "remediation": {"name": "ECS deployment circuit breaker", "state": "rolls back to the last good task set (rollback = true); agent dispatch follows from coord"}}
    EOT
  }
}

# ─── RDS storage events -> topic ────────────────────────────────────────

resource "aws_cloudwatch_event_rule" "rds_storage" {
  name        = "${local.pager_prefix}rds-storage"
  description = "RDS storage events for ${var.postgres_instance_identifier} (approaching the autoscaling maximum, autoscaling failures, storage low or full). Pages storage_or_iops. Plan 2026-10-14 operator pager, D10."
  event_pattern = jsonencode({
    source        = ["aws.rds", local.drill_source]
    "detail-type" = ["RDS DB Instance Event"]
    detail = {
      SourceIdentifier = [var.postgres_instance_identifier]
      EventID          = local.rds_storage_event_ids
    }
  })
}

resource "aws_cloudwatch_event_target" "rds_storage" {
  rule      = aws_cloudwatch_event_rule.rds_storage.name
  target_id = "operator-pager"
  arn       = aws_sns_topic_policy.pager.arn

  input_transformer {
    input_paths = {
      event_source = "$.source"
      event_id     = "$.id"
      event_time   = "$.time"
      rds_event_id = "$.detail.EventID"
      instance     = "$.detail.SourceIdentifier"
      message      = "$.detail.Message"
    }
    input_template = <<-EOT
      {"page_version": 1, "kind": "page", "condition": "storage_or_iops", "source": "eventbridge", "event_source": <event_source>, "event_id": <event_id>, "event_time": <event_time>, "aws": {"rds_event_id": <rds_event_id>, "instance": <instance>, "message": <message>}, "title": "RDS storage event", "remediation": {"name": "RDS storage autoscaling", "state": "grows storage up to the maximum allocated storage; agent dispatch follows from coord"}}
    EOT
  }
}
