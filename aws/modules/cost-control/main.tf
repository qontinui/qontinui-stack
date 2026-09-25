# Cost guardrail: a monthly AWS Budget with an SNS-email alert.
# Verification mechanism, not manual observation — fires automatically
# at 80% actual and 100% forecasted spend.

variable "environment" { type = string }
variable "monthly_limit" {
  type    = string
  default = "100" # USD
}
# PII, and this repo is PUBLIC. The address reaches three attributes below:
# `aws_sns_topic_subscription.budget_email.endpoint` and both
# `aws_budgets_budget.monthly` notification blocks' `subscriber_email_addresses`.
# Terraform redacts only what it KNOWS is sensitive, so without this marker the
# operator's address rendered verbatim in every `terraform plan` diff that
# touched the subscription — recorded as an open residual in
# docs/terraform-state-secret-inventory.md, "What follows from it" 1.
#
# That stopped being a terminal-only exposure with Phase 3b: a scheduled
# `scripts/terraform-plan-drift.py` now runs a FULL plan and machine-processes
# it, and terraform's own stderr is printed verbatim when it exits non-zero.
# The classification it posts to coord carries attribute NAMES only, so the
# marker is about the plan rendering, not the payload.
#
# Same treatment as `variable "signup_allowlist"` in modules/cross-idp-linking,
# which is the same class of value and was already marked. The asymmetry between
# the two is what the inventory flagged.
variable "alert_email" {
  type        = string
  description = "Address AWS Budgets and the SNS topic send the budget alert to. Operator PII, not a credential: staged out-of-band in SSM (/qontinui/ops/budget-alert-email) and supplied by the composition root, never a committed default in this public repo."
  sensitive   = true
}
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

# Account id, region and partition for scoping the publish grants below.
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

# This topic carries THREE publishers, so the policy needs THREE statements.
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
#   3. RDS events  — the observability module's aws_db_event_subscription
#      (events.rds.amazonaws.com). SNS's default policy would have let RDS
#      publish, but that default is exactly what this policy replaces, so
#      without this statement RDS event delivery is silently denied. For RDS,
#      aws:SourceArn is the SOURCE RESOURCE's ARN (the DB instance,
#      `…:rds:<region>:<account>:db:<id>`), NOT the event subscription's
#      `…:es:<name>` — the RDS User Guide, "Granting permissions to publish
#      notifications to an Amazon SNS topic". An `es:*` condition would never
#      match, so every event would be denied. Scoped to this account's DB
#      instances in this region: aws:SourceAccount (confused-deputy guard) plus
#      ArnLike on `db:*`. It is `db:*` rather than one instance id so that
#      adding a source_id to the subscription does not silently lose delivery.
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

  statement {
    sid       = "AllowRdsEventsPublish"
    actions   = ["SNS:Publish"]
    effect    = "Allow"
    resources = [aws_sns_topic.budget.arn]
    principals {
      type        = "Service"
      identifiers = ["events.rds.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:rds:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:db:*"]
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
# Read through the topic POLICY rather than the topic. The value is identical
# (the policy's `arn` is the topic ARN), but the reference orders every
# consumer — the observability alarms and its RDS event subscription — after
# the publish grants above exist, so a first apply cannot wire a publisher to a
# topic that still denies it.
output "sns_topic_arn" { value = aws_sns_topic_policy.budget.arn }
# `sensitive = true` is REQUIRED, not decorative: a root module re-exporting an
# unmarked child output that carries a sensitive value is a plan-time error
# ("Output refers to sensitive values"). Nothing consumes this output today, so
# marking it keeps the module's own plan honest AND keeps that guard armed for
# whoever wires it up next.
output "alert_email" {
  value     = var.alert_email
  sensitive = true
}
