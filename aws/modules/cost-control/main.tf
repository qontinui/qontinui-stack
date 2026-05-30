# Cost guardrail: a monthly AWS Budget with an SNS-email alert.
# Verification mechanism, not manual observation — fires automatically
# at 80% actual and 100% forecasted spend.

variable "environment" { type = string }
variable "monthly_limit" {
  type    = string
  default = "100" # USD
}
variable "alert_email" { type = string }
variable "alert_threshold_percent" {
  type    = number
  default = 80
}

resource "aws_sns_topic" "budget" {
  name = "qontinui-${var.environment}-budget-alerts"
}

resource "aws_sns_topic_subscription" "budget_email" {
  topic_arn = aws_sns_topic.budget.arn
  protocol  = "email"
  endpoint  = var.alert_email
  # Email subscriptions are PENDING until the recipient clicks the
  # confirmation link AWS emails them. Surface this to the operator.
}

# Account id for scoping the CloudWatch publish grant below.
data "aws_caller_identity" "current" {}

# This topic carries TWO publishers, so the policy needs TWO statements.
# Setting an explicit aws_sns_topic_policy REPLACES SNS's default policy
# (which would otherwise allow same-account principals to publish), so every
# publisher must be granted EXPLICITLY here or its publish is silently denied.
#
#   1. AWS Budgets — the budget-alert path (budgets.amazonaws.com).
#   2. CloudWatch  — the coord alarm path (cloudwatch.amazonaws.com). The
#      observability module's coord alarms (no-healthy-hosts / 5xx / latency)
#      target this topic; without this statement their SNS publish fails with
#      "Failed to execute action …" and NO email is delivered. (Regression
#      caught 2026-05-30 by forcing an alarm via `set-alarm-state` and reading
#      the Action history — it had been broken since the alarms were created.)
#      Scoped to this account's CloudWatch via aws:SourceAccount (least-priv;
#      a foreign account's CloudWatch can't publish here).
data "aws_iam_policy_document" "budget_sns" {
  statement {
    sid       = "AllowBudgetsPublish"
    actions   = ["SNS:Publish"]
    effect    = "Allow"
    resources = [aws_sns_topic.budget.arn]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
  }

  statement {
    sid       = "AllowCloudWatchAlarmsPublish"
    actions   = ["SNS:Publish"]
    effect    = "Allow"
    resources = [aws_sns_topic.budget.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "budget" {
  arn    = aws_sns_topic.budget.arn
  policy = data.aws_iam_policy_document.budget_sns.json
}

resource "aws_budgets_budget" "monthly" {
  name         = "qontinui-${var.environment}-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_limit
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = var.alert_threshold_percent
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.budget.arn]
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [aws_sns_topic.budget.arn]
    subscriber_email_addresses = [var.alert_email]
  }
}

output "budget_name" { value = aws_budgets_budget.monthly.name }
output "sns_topic_arn" { value = aws_sns_topic.budget.arn }
output "alert_email" { value = var.alert_email }
