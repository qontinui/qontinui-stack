# EventBridge sources of the operator pager (plan Phase 2; D10, D11, D12).
#
# Every rule is named `<prefix>-pager-*` (the topic policy's ArnLike admits
# exactly that), and every pattern also matches `source: ["qontinui.drill"]`, so
# a drill exercises the REAL rule: PutEvents cannot forge an `aws.*` source. A
# drill event carries `DRILL <id>` in the field the template forwards (`reason`
# for ECS, `Message` for RDS), and the relay reads the drill id from there.
#
# Input templates use UNQUOTED placeholders (`"reason": <reason>`), so
# EventBridge inserts each value as an escaped JSON value rather than splicing
# raw text into a string literal; the output is the relay's `page_version: 1`
# shape.

# ─── Abnormal coord task exits → log group (crash-loop child, D11) ──────

resource "aws_cloudwatch_log_group" "coord_task_exits" {
  # EventBridge can only target log groups under /aws/events/.
  name              = "/aws/events/${local.prefix}-coord-task-exits"
  retention_in_days = 90
}

# A CloudWatch Logs RESOURCE policy (not an IAM role) is how EventBridge gets
# write access to a log-group target. Scoped to this one group and to rules on
# the pager prefix.
data "aws_iam_policy_document" "coord_task_exits_logs" {
  statement {
    sid       = "EventBridgePagerRulesWriteTaskExits"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.coord_task_exits.arn}:*"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.part}:events:${local.region}:${local.account}:rule/${local.pager_name_prefix}*"]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "coord_task_exits" {
  policy_name     = "${local.prefix}-pager-coord-task-exits"
  policy_document = data.aws_iam_policy_document.coord_task_exits_logs.json
}

# STOPPED coord tasks whose stop was abnormal. A routine deploy or scale-in
# stops tasks with stopCode ServiceSchedulerInitiated, which this does not
# match; the four arms below are the crash shapes (plan D11).
resource "aws_cloudwatch_event_rule" "coord_task_exits" {
  name        = "${local.pager_name_prefix}coord-task-exits"
  description = "Abnormal coord ECS task exits -> ${aws_cloudwatch_log_group.coord_task_exits.name} (counted by the pager crash-loop child; drill events land but are not counted)."
  event_pattern = jsonencode({
    source        = ["aws.ecs", "qontinui.drill"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [var.coord_cluster_arn]
      group      = ["service:${var.coord_service_name}"]
      lastStatus = ["STOPPED"]
      "$or" = [
        { stopCode = ["EssentialContainerExited", "TaskFailedToStart"] },
        { stoppedReason = [
          { prefix = "Task failed ELB health checks" },
          { prefix = "Task failed container health checks" },
        ] },
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "coord_task_exits" {
  rule       = aws_cloudwatch_event_rule.coord_task_exits.name
  target_id  = "coord-task-exits-log"
  arn        = aws_cloudwatch_log_group.coord_task_exits.arn
  depends_on = [aws_cloudwatch_log_resource_policy.coord_task_exits]
}

# ─── ECS deployment failed → pager topic (D12) ──────────────────────────
# ECS's own verdict, for coord and web. Both services run the deployment
# circuit breaker with rollback = true, which is the automatic remediation the
# page names. CI's smoke-failure rollback never emits this event; that case is
# paged from deploy-coord.yml (Phase 3).

locals {
  ecs_service_arn_prefix = "arn:${local.part}:ecs:${local.region}:${local.account}:service/${var.coord_cluster_name}"
}

resource "aws_cloudwatch_event_rule" "deploy_failed" {
  name        = "${local.pager_name_prefix}deploy-failed"
  description = "ECS SERVICE_DEPLOYMENT_FAILED for coord or web -> operator pager (deploy_failed)."
  event_pattern = jsonencode({
    source        = ["aws.ecs", "qontinui.drill"]
    "detail-type" = ["ECS Deployment State Change"]
    resources = [
      "${local.ecs_service_arn_prefix}/${var.coord_service_name}",
      "${local.ecs_service_arn_prefix}/${var.web_service_name}",
    ]
    detail = { eventName = ["SERVICE_DEPLOYMENT_FAILED"] }
  })
}

resource "aws_cloudwatch_event_target" "deploy_failed" {
  rule      = aws_cloudwatch_event_rule.deploy_failed.name
  target_id = "operator-pager"
  arn       = aws_sns_topic_policy.pager.arn

  input_transformer {
    input_paths = {
      source        = "$.source"
      time          = "$.time"
      resources     = "$.resources"
      event_name    = "$.detail.eventName"
      reason        = "$.detail.reason"
      deployment_id = "$.detail.deploymentId"
    }
    input_template = <<-EOT
      {"page_version": 1, "kind": "page", "origin": "eventbridge", "condition": "deploy_failed", "signal": "${aws_cloudwatch_event_rule.deploy_failed.name}", "source": <source>, "time": <time>, "resources": <resources>, "event_name": <event_name>, "reason": <reason>, "deployment_id": <deployment_id>, "remediation": "the ECS deployment circuit breaker rolls the service back to its last steady deployment (rollback = true)", "remediation_state": "started by ECS with this event"}
    EOT
  }
}

# ─── RDS storage events → pager topic (D10) ─────────────────────────────
# 0225 (approaching max, at 80%: weeks of notice), 0224 (autoscaling will reach
# the max), 0223 (autoscaling unable to scale), 0007 (allocated storage
# exhausted), 0089 (> 90% consumed), 0222 (last warning before RDS shuts the
# database down), 0221 (storage-full, shut down). Routine autoscaling emits
# none of these, so it does not page.

resource "aws_cloudwatch_event_rule" "rds_storage" {
  name        = "${local.pager_name_prefix}rds-storage-events"
  description = "RDS storage events for ${var.postgres_instance_identifier} -> operator pager (storage_or_iops)."
  event_pattern = jsonencode({
    source        = ["aws.rds", "qontinui.drill"]
    "detail-type" = ["RDS DB Instance Event"]
    detail = {
      SourceIdentifier = [var.postgres_instance_identifier]
      EventID = [
        "RDS-EVENT-0225",
        "RDS-EVENT-0224",
        "RDS-EVENT-0223",
        "RDS-EVENT-0007",
        "RDS-EVENT-0089",
        "RDS-EVENT-0222",
        "RDS-EVENT-0221",
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "rds_storage" {
  rule      = aws_cloudwatch_event_rule.rds_storage.name
  target_id = "operator-pager"
  arn       = aws_sns_topic_policy.pager.arn

  input_transformer {
    input_paths = {
      source            = "$.source"
      time              = "$.time"
      event_id          = "$.detail.EventID"
      message           = "$.detail.Message"
      source_identifier = "$.detail.SourceIdentifier"
    }
    input_template = <<-EOT
      {"page_version": 1, "kind": "page", "origin": "eventbridge", "condition": "storage_or_iops", "signal": "${aws_cloudwatch_event_rule.rds_storage.name}", "source": <source>, "time": <time>, "event_id": <event_id>, "message": <message>, "source_identifier": <source_identifier>, "remediation": "RDS storage autoscaling grows the volume up to its maximum allocated storage", "remediation_state": "in force unless the event says the maximum is reached"}
    EOT
  }
}
