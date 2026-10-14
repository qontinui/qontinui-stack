# ─── Operator pager: one topic, one relay ───────────────────────────────
#
# Plan 2026-10-14-coord-pages-the-operator-for-five-conditions-and-names-the-agent-fixing-each.
# Phase 1 (this file): the SNS topic every pager source publishes to, and the relay Lambda that is
# its only subscriber (decisions D2 and D4). Phase 2 adds the AWS-side detectors and coord's grants
# in sibling files of this module. Phase 6 switches the relay's adapter to the operator's vendor.
#
# What pages: at most five conditions (train stalled, coord crash loop, deploy failed, database
# storage or IOPS, GitHub API budget exhausted). The cap is enforced in three places: coord's operator
# leg, the topic policy below (only `qontinui-<env>-pager-*` alarms and rules may publish), and the
# relay, which drops any message whose condition is not one of the five.
#
# What a page is: information. Every page names the remediation already in force and ends with "No
# action needed from you." (decision_record/operational-work-is-autonomous).
#
# Where the vendor key lives: nowhere in this repo or its state. The operator saves it in Parameter
# Store (eu-central-1) in the AWS Console; the relay reads it at run time (D4).
#
# The topic is unencrypted: pages carry no secrets, and a KMS key would need grants for CloudWatch
# and EventBridge to publish.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  account      = data.aws_caller_identity.current.account_id
  region       = data.aws_region.current.name
  partition    = data.aws_partition.current.partition
  pager_prefix = "qontinui-${var.environment}-pager-"
  relay_name   = "qontinui-${var.environment}-pager-relay"

  coord_deploy_role_arn = "arn:${local.partition}:iam::${local.account}:role/${var.coord_deploy_role_name}"

  # Alarm name -> { condition, title, remediation }. The relay looks every CloudWatch notification up
  # here; an alarm missing from the map is dropped. Phase 2 fills it with this module's own alarms.
  pager_alarm_map = {}
}

# ─── The topic ──────────────────────────────────────────────────────────

resource "aws_sns_topic" "pager" {
  name = "qontinui-${var.environment}-operator-pager"
}

# An explicit topic policy REPLACES SNS's default one, so every publisher is named here (the same
# rule aws/modules/cost-control/main.tf documents). The aws:SourceArn conditions admit only alarms
# and rules named `qontinui-<env>-pager-*`: without them any alarm or rule in the account that names
# this topic could publish to the pager. The relay would still drop an unmapped alarm, but the cap
# should not rest on one layer when a policy condition costs nothing.
data "aws_iam_policy_document" "pager_topic" {
  statement {
    sid       = "AllowPagerAlarmsPublish"
    actions   = ["SNS:Publish"]
    effect    = "Allow"
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
      values   = ["arn:${local.partition}:cloudwatch:${local.region}:${local.account}:alarm:${local.pager_prefix}*"]
    }
  }
  statement {
    sid       = "AllowPagerRulesPublish"
    actions   = ["SNS:Publish"]
    effect    = "Allow"
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
      values   = ["arn:${local.partition}:events:${local.region}:${local.account}:rule/${local.pager_prefix}*"]
    }
  }
  statement {
    sid       = "AllowCoordOperatorLegPublish"
    actions   = ["SNS:Publish"]
    effect    = "Allow"
    resources = [aws_sns_topic.pager.arn]
    principals {
      type        = "AWS"
      identifiers = [var.coord_task_role_arn]
    }
  }
  statement {
    sid       = "AllowCoordDeployRollbackPublish"
    actions   = ["SNS:Publish"]
    effect    = "Allow"
    resources = [aws_sns_topic.pager.arn]
    principals {
      type        = "AWS"
      identifiers = [local.coord_deploy_role_arn]
    }
  }
}

resource "aws_sns_topic_policy" "pager" {
  arn    = aws_sns_topic.pager.arn
  policy = data.aws_iam_policy_document.pager_topic.json
}

# ─── The relay ──────────────────────────────────────────────────────────

# boto3 ships in the Lambda Python runtime, so only relay.py is zipped.
data "archive_file" "relay" {
  type        = "zip"
  source_file = "${path.module}/../../lambdas/operator_pager_relay/relay.py"
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
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Least privilege, statement by statement:
#   - publish to the email topic (the sns_forward adapter, and the fallback after Phase 6);
#   - read the operator-staged vendor key, and nothing else in Parameter Store. First used in Phase 6.
#     With the Console's default key (alias/aws/ssm) no kms:Decrypt grant is needed: that AWS-managed
#     key's policy admits account principals through SSM (plan D4);
#   - send a failed invocation to the dead-letter queue (the async on-failure destination).
data "aws_iam_policy_document" "relay" {
  statement {
    sid       = "ForwardPagesToTheEmailTopic"
    actions   = ["sns:Publish"]
    resources = [var.email_topic_arn]
  }
  statement {
    sid       = "ReadTheOperatorStagedVendorKey"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${local.partition}:ssm:${var.vendor_parameter_region}:${local.account}:parameter${var.vendor_parameter_prefix}*"]
  }
  statement {
    sid       = "DeadLetterFailedInvocations"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.relay_dlq.arn]
  }
}

resource "aws_iam_role_policy" "relay" {
  name   = local.relay_name
  role   = aws_iam_role.relay.id
  policy = data.aws_iam_policy_document.relay.json
}

resource "aws_cloudwatch_log_group" "relay" {
  name              = "/aws/lambda/${local.relay_name}"
  retention_in_days = var.relay_log_retention_days
}

resource "aws_sqs_queue" "relay_dlq" {
  name                      = "${local.relay_name}-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_lambda_function" "relay" {
  function_name = local.relay_name
  description   = "Operator pager relay: one page format, five conditions, never asks (plan 2026-10-14 operator pager)."
  role          = aws_iam_role.relay.arn
  runtime       = "python3.12"
  handler       = "relay.handler"
  # Worst case: the primary twice and the fallback once, each bounded at 13 s by the SNS client's
  # timeouts, plus one 5 s retry wait = 44 s (relay.WORST_CASE_DELIVERY_SECONDS; a test checks this
  # value exceeds it). A timeout would leave no outcome record and invite a duplicate async retry.
  timeout     = 60
  memory_size = 128

  filename         = data.archive_file.relay.output_path
  source_code_hash = data.archive_file.relay.output_base64sha256

  environment {
    variables = merge(
      {
        PAGER_ADAPTER       = var.adapter
        EMAIL_TOPIC_ARN     = var.email_topic_arn
        PAGER_ALARM_MAP     = jsonencode(local.pager_alarm_map)
        RETRY_DELAY_SECONDS = "5"
      },
      # Only a vendor adapter reads a key; sns_forward reads none.
      var.adapter == "sns_forward" ? {} : {
        VENDOR_PARAMETER_NAME   = "${var.vendor_parameter_prefix}${var.adapter}"
        VENDOR_PARAMETER_REGION = var.vendor_parameter_region
      },
    )
  }

  depends_on = [
    aws_iam_role_policy_attachment.relay_logs,
    aws_cloudwatch_log_group.relay,
  ]
}

# A raise means no channel delivered the page: Lambda retries the async invocation twice, then the
# event lands in the dead-letter queue, whose depth alarm below emails the operator.
resource "aws_lambda_function_event_invoke_config" "relay" {
  function_name                = aws_lambda_function.relay.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600

  destination_config {
    on_failure {
      destination = aws_sqs_queue.relay_dlq.arn
    }
  }
}

resource "aws_lambda_permission" "pager_topic_invoke" {
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

  depends_on = [aws_lambda_permission.pager_topic_invoke]
}

# ─── The pager's own health ─────────────────────────────────────────────
#
# These notify the EMAIL topic, deliberately not the pager: a broken pager cannot report itself.
# Their names do not start with the pager prefix, so the pager topic policy refuses them anyway.
# Each counts a discrete event that is emitted only when it happens, so no data means none happened
# (notBreaching).

resource "aws_cloudwatch_metric_alarm" "relay_errors" {
  alarm_name          = "qontinui-${var.environment}-operator-pager-health-relay-errors"
  alarm_description   = "The operator pager relay raised: a page reached no channel. Lambda retries it twice, then dead-letters it. Read the relay's log group."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    FunctionName = aws_lambda_function.relay.function_name
  }
  alarm_actions = [var.email_topic_arn]
  ok_actions    = [var.email_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "relay_dlq_depth" {
  alarm_name          = "qontinui-${var.environment}-operator-pager-health-dead-letters"
  alarm_description   = "A page the relay could not deliver after its retries is waiting in the dead-letter queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    QueueName = aws_sqs_queue.relay_dlq.name
  }
  alarm_actions = [var.email_topic_arn]
  ok_actions    = [var.email_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "relay_dropped" {
  alarm_name          = "qontinui-${var.environment}-operator-pager-health-dropped"
  alarm_description   = "The relay dropped a message: an unmapped alarm, a condition outside the five, or an unrecognised shape. Its record (outcome dropped) names the reason."
  namespace           = "Qontinui/OperatorPager"
  metric_name         = "Dropped"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.email_topic_arn]
  ok_actions          = [var.email_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "relay_fallback" {
  alarm_name          = "qontinui-${var.environment}-operator-pager-health-fallback"
  alarm_description   = "The configured adapter could not deliver a page and the email fallback did. The page reached a human; the pager path did not."
  namespace           = "Qontinui/OperatorPager"
  metric_name         = "FallbackForwarded"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.email_topic_arn]
  ok_actions          = [var.email_topic_arn]
}
