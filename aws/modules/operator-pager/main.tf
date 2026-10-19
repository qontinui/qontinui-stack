# Operator pager: one SNS topic every pager source publishes to, and one relay
# Lambda that is its only subscriber and the only holder of a vendor credential.
#
# Plan: qontinui-dev-notes plans/2026-10-14-coord-pages-the-operator-for-five-
# conditions-and-names-the-agent-fixing-each.md, Phases 1 (this file) and 2
# (alarms.tf, events.tf, iam_coord.tf).
#
#   CloudWatch composites ─┐
#   EventBridge rules ─────┤                           ┌─ adapter (sns_forward
#   coord task role ───────┼─→ qontinui-<env>-operator ─→ relay ─┤  today; vendor in
#   coord deploy role ─────┘        -pager                        │  Phase 6)
#                                                                 └─ fallback: email topic
#
# Why a SEPARATE topic rather than the email topic (plan D2): the email topic
# delivered 1,279 notifications in 30 days, so a real page cannot be told apart
# there. The pager admits at most five conditions (plan D7) and that cap is
# enforced twice — by the publishers (coord's closed enum; the alarm/rule names
# the topic policy admits) and by the relay, which drops anything else.
#
# Unencrypted on purpose: pages carry no secrets, and a KMS key would need key
# grants for CloudWatch and EventBridge. The vendor key never transits this
# topic; the relay reads it from SSM (eu-central-1) at run time (plan D4).
#
# Off-switch: var.enabled = false (root tfvar operator_pager_enabled) removes
# the relay's subscription.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  prefix  = "qontinui-${var.environment}"
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.name
  part    = data.aws_partition.current.partition

  # Every pager alarm and rule is named `<prefix>-pager-*`; the topic policy
  # admits exactly that prefix (plan Phase 2 "Names").
  pager_name_prefix = "${local.prefix}-pager-"

  relay_name = "${local.prefix}-operator-pager-relay"

  # Alarm names, as locals so the relay's map does not depend on the alarm
  # resources (alarms.tf references these same names).
  alarm_crash_loop         = "${local.pager_name_prefix}coord-crash-loop"
  alarm_crash_loop_drill   = "${local.pager_name_prefix}coord-crash-loop-drill"
  alarm_free_storage       = "${local.pager_name_prefix}rds-free-storage"
  alarm_free_storage_drill = "${local.pager_name_prefix}rds-free-storage-drill"
  alarm_saturation         = "${local.pager_name_prefix}rds-saturation"
  alarm_saturation_drill   = "${local.pager_name_prefix}rds-saturation-drill"

  # The three drill children: the only alarms a drill ever sets, and the only
  # ones the relay may read history from.
  drill_children = [local.alarm_crash_loop_drill, local.alarm_free_storage_drill, local.alarm_saturation_drill]

  # The relay's alarm → condition map (handler.py PAGER_ALARM_MAP). Only the
  # alarms that carry ACTIONS are here, and all three are composites (child
  # alarms carry no actions, so they never notify). An alarm missing from this
  # map is DROPPED by the relay. `drill_child` names each composite's dedicated
  # drill child (plan D16).
  pager_alarm_map = {
    (local.alarm_crash_loop) = {
      condition   = "leader_crash_loop"
      remediation = "ECS replaces stopped coord tasks and the coord leader lease fails over"
      drill_child = local.alarm_crash_loop_drill
    }
    (local.alarm_free_storage) = {
      condition   = "storage_or_iops"
      remediation = "RDS storage autoscaling grows the volume up to its maximum allocated storage"
      drill_child = local.alarm_free_storage_drill
    }
    (local.alarm_saturation) = {
      condition   = "storage_or_iops"
      remediation = "none automatic: RDS keeps serving at its provisioned IOPS ceiling"
      drill_child = local.alarm_saturation_drill
    }
  }
}

# ─── The pager topic ─────────────────────────────────────────────────────

resource "aws_sns_topic" "pager" {
  name = "${local.prefix}-operator-pager"
}

# An explicit topic policy REPLACES SNS's default, so every publisher is named
# here or its publish is silently denied (the cost-control module's lesson).
#
# The two service statements carry BOTH aws:SourceAccount and an ArnLike
# aws:SourceArn on the `-pager-*` name prefix. Without the SourceArn condition
# any alarm or rule in the account that names this topic could publish to the
# pager; the relay would still drop an unmapped alarm, but the cap should not
# rest on one layer when a condition costs nothing (plan Phase 1, vet addition).
data "aws_iam_policy_document" "pager_topic" {
  statement {
    sid       = "AllowPagerAlarmsPublish"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.pager.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.part}:cloudwatch:${local.region}:${local.account}:alarm:${local.pager_name_prefix}*"]
    }
  }

  statement {
    sid       = "AllowPagerRulesPublish"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.pager.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.part}:events:${local.region}:${local.account}:rule/${local.pager_name_prefix}*"]
    }
  }

  # coord's operator leg (Phase 3) and coord's CI rollback step (Phase 3, via
  # the deploy role). Each role also needs its own identity-side sns:Publish:
  # the task role's is in iam_coord.tf; the deploy role's lives in qontinui-coord
  # deploy/iam/qontinui-coord-deploy.policy.json (plan D12).
  statement {
    sid       = "AllowCoordPublish"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.pager.arn]
    principals {
      type = "AWS"
      identifiers = [
        var.coord_task_role_arn,
        "arn:${local.part}:iam::${local.account}:role/${var.coord_deploy_role_name}",
      ]
    }
  }
}

resource "aws_sns_topic_policy" "pager" {
  arn    = aws_sns_topic.pager.arn
  policy = data.aws_iam_policy_document.pager_topic.json
}

# ─── Dead-letter queue ───────────────────────────────────────────────────
# Two failure paths land here: the relay raising after Lambda's async retries
# (dead_letter_config below), and SNS failing to invoke the relay at all
# (the subscription's redrive policy). Depth >= 1 alarms on the email topic.

resource "aws_sqs_queue" "dlq" {
  name                      = "${local.prefix}-operator-pager-dlq"
  message_retention_seconds = 1209600 # 14 days, the SQS maximum
  sqs_managed_sse_enabled   = true
}

data "aws_iam_policy_document" "dlq" {
  statement {
    sid       = "AllowPagerTopicRedrive"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dlq.arn]
    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_sns_topic.pager.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy    = data.aws_iam_policy_document.dlq.json
}

# ─── The relay ───────────────────────────────────────────────────────────
# Same packaging as modules/cross-idp-linking: zip handler.py alone; boto3
# ships in the Python runtime.

data "archive_file" "relay" {
  type        = "zip"
  source_file = "${path.module}/../../lambdas/operator_pager_relay/handler.py"
  output_path = "${path.module}/.build/operator_pager_relay.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "relay" {
  name               = local.relay_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "relay_logs" {
  role       = aws_iam_role.relay.name
  policy_arn = "arn:${local.part}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Least privilege, one statement per need:
#   * publish to the EMAIL topic (sns_forward + the vendor fallback). The email
#     topic's own policy must also name this role: cost-control's
#     AllowOperatorPagerRelayPublish statement;
#   * read the operator-staged vendor parameter, by path, in eu-central-1
#     (first used in Phase 6). No kms:Decrypt: the operator keeps the Console's
#     default alias/aws/ssm key, whose AWS-managed key policy admits account
#     principals through SSM (plan D4). A customer-managed key would need a
#     Decrypt grant conditioned on kms:EncryptionContext:PARAMETER_ARN;
#   * write to the dead-letter queue.
#   * read the state history of the three DRILL CHILDREN only
#     (cloudwatch:DescribeAlarmHistory). A composite's notification names the
#     child that moved it, but the drill id lives only in that child's own state
#     reason ("DRILL <id>"). The same read decides drill-or-real when a
#     notification names no child, and it is what lets the relay suppress the
#     composite's return to OK at the end of a drill.
data "aws_iam_policy_document" "relay" {
  statement {
    sid       = "ForwardToEmailTopic"
    actions   = ["sns:Publish"]
    resources = [var.email_topic_arn]
  }

  statement {
    sid       = "ReadOperatorStagedVendorKey"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${local.part}:ssm:${var.operator_pager_ssm_region}:${local.account}:parameter/qontinui/ops/operator-pager/*"]
  }

  statement {
    sid       = "ReadDrillChildHistory"
    actions   = ["cloudwatch:DescribeAlarmHistory"]
    resources = [for n in local.drill_children : "arn:${local.part}:cloudwatch:${local.region}:${local.account}:alarm:${n}"]
  }

  statement {
    sid       = "DeadLetter"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dlq.arn]
  }
}

resource "aws_iam_role_policy" "relay" {
  name   = local.relay_name
  role   = aws_iam_role.relay.id
  policy = data.aws_iam_policy_document.relay.json
}

# 90 days: Phase 5's ingest reads the last hour on start, and its adapter gauge
# is withheld once the newest record is > 35 days old, so the records must
# outlive that window.
resource "aws_cloudwatch_log_group" "relay" {
  name              = "/aws/lambda/${local.relay_name}"
  retention_in_days = 90
}

resource "aws_lambda_function" "relay" {
  function_name = local.relay_name
  role          = aws_iam_role.relay.arn
  runtime       = "python3.12"
  handler       = "handler.handler"
  # Worst case, every AWS call at its 3 s connect + 7 s read timeout: one
  # drill-history read, the adapter call, its one retry after 5 s, and the
  # fallback = 45 s (handler.py boto_config). 90 s leaves room for a cold start
  # and is pinned by test_lambda_timeout_covers_the_worst_case_delivery.
  timeout     = 90
  memory_size = 128

  filename         = data.archive_file.relay.output_path
  source_code_hash = data.archive_file.relay.output_base64sha256

  dead_letter_config {
    target_arn = aws_sqs_queue.dlq.arn
  }

  environment {
    variables = {
      ENVIRONMENT     = var.environment
      EMAIL_TOPIC_ARN = var.email_topic_arn
      PAGER_ADAPTER   = var.adapter
      PAGER_ALARM_MAP = jsonencode(local.pager_alarm_map)
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.relay_logs,
    aws_iam_role_policy.relay,
    aws_cloudwatch_log_group.relay,
  ]
}

# SNS invokes the relay ASYNCHRONOUSLY, and Lambda retries a failed async
# invocation twice by default. The relay has its own retry rule (4xx never, 5xx
# once), so Lambda's would multiply it: a 4xx sent 3 times, a 5xx tried 6, and
# three records for one message. Off: a failed invocation goes straight to the
# dead-letter queue (dead_letter_config above) and counts as a Lambda error.
resource "aws_lambda_function_event_invoke_config" "relay" {
  function_name          = aws_lambda_function.relay.function_name
  maximum_retry_attempts = 0
}

resource "aws_lambda_permission" "sns_invoke" {
  statement_id  = "AllowOperatorPagerTopicInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.relay.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.pager.arn
}

resource "aws_sns_topic_subscription" "relay" {
  count     = var.enabled ? 1 : 0
  topic_arn = aws_sns_topic.pager.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.relay.arn

  redrive_policy = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dlq.arn })

  depends_on = [aws_lambda_permission.sns_invoke, aws_sqs_queue_policy.dlq]
}

# ─── Delivery health (on the EMAIL topic, deliberately not on the pager) ─
# A pager that cannot deliver must be reported on a channel that does not
# depend on it. Names are `<prefix>-operator-pager-*`, NOT `-pager-*`, so the
# pager topic's policy would refuse them even if one were mis-pointed there.
# Quiet periods emit no datapoints, so missing data is not breaching.

resource "aws_cloudwatch_metric_alarm" "relay_errors" {
  alarm_name          = "${local.prefix}-operator-pager-relay-errors"
  alarm_description   = "Operator pager relay Lambda errored (>= 1 in 5 min). A page may not have been delivered: Lambda's async retries are off (maximum_retry_attempts = 0), so the message is already in the operator-pager DLQ. Read the relay log group for the record."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.relay.function_name }
  alarm_actions       = [var.email_topic_arn]
  ok_actions          = [var.email_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "${local.prefix}-operator-pager-dlq-depth"
  alarm_description   = "Operator pager dead-letter queue holds >= 1 message: a page exhausted every retry (relay) or SNS could not invoke the relay. Each message is an undelivered page."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  alarm_actions       = [var.email_topic_arn]
  ok_actions          = [var.email_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "relay_dropped" {
  alarm_name          = "${local.prefix}-operator-pager-dropped"
  alarm_description   = "Operator pager relay DROPPED a message (>= 1 in 5 min): unmapped alarm, a condition outside the five, or an unparseable message. Something is publishing to the pager that should not, or a pager alarm is missing from PAGER_ALARM_MAP."
  namespace           = "Qontinui/OperatorPager"
  metric_name         = "Dropped"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { Environment = var.environment }
  alarm_actions       = [var.email_topic_arn]
  ok_actions          = [var.email_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "relay_fallback" {
  alarm_name          = "${local.prefix}-operator-pager-fallback"
  alarm_description   = "Operator pager vendor adapter failed and the relay fell back to the email topic (>= 1 in 5 min). Pages are reaching email only; the vendor channel is broken."
  namespace           = "Qontinui/OperatorPager"
  metric_name         = "FallbackForwarded"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { Environment = var.environment }
  alarm_actions       = [var.email_topic_arn]
  ok_actions          = [var.email_topic_arn]
}
