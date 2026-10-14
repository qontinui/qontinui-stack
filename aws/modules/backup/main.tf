# Off-instance backups of the coord database (RDS), kept in two regions.
#
# An AWS Backup plan snapshots the RDS instance on `schedule`, keeps each
# recovery point `retention_days` in a vault in this region, and copies it to a
# vault in a second region (the `aws.dr` provider), kept `dr_retention_days`.
# Those copies live OUTSIDE the instance. Deleting the instance, setting its
# backup retention to 0 (which deletes every automated backup), or losing the
# region leaves them in place. Before this module, every backup of the
# database lived and died with the instance's own 7-day automated retention.
#
# Plan 2026-10-14-coord-database-is-one-command-from-total-loss (qontinui-dev-
# notes), claim r02-off-instance-backup (domain_spec/coord-review-2026-10).
# Why AWS Backup snapshots and not a nightly pg_dump: the instance's volume is
# already at its read IOPS/throughput ceiling, and a dump is a full nightly read
# plus an hours-long snapshot that holds back vacuum. Why not RDS cross-region
# automated-backup replication: it replicates transaction logs (~26 GB/day
# measured) and keeps no in-region copy past the 7-day window. Both are in the
# plan's D2.
#
# Facts this module relies on (AWS documentation, checked 2026-10-14):
#   - AWS Backup's periodic snapshots of an RDS instance are RDS MANUAL
#     snapshots in the instance's single incremental chain. RDS bills them as
#     backup storage alongside the automated backups; AWS Backup itself adds
#     no charge for RDS ("Amazon RDS backups" and "Metering, costs, and
#     billing" in the AWS Backup guide).
#   - The first copy into a region is full. Later copies are incremental while
#     the previous copy still exists there and every copy there uses the same
#     KMS key. AWS Backup encrypts every copy with the destination vault's
#     key, so the key is constant by construction.
#   - AWS Backup does not fully manage RDS, so the DESTINATION vault's key
#     must be a customer-managed key or RDS's own AWS-managed key in that
#     region. A vault left on the default aws/backup key fails every RDS copy
#     job. The source vault's key is not used for RDS recovery points at all:
#     they keep the instance's own key (aws/rds here).
#   - enable_continuous_backup stays false. Continuous backup hands the
#     instance's automated backups to AWS Backup, which then moves the
#     instance's backup window and owns its retention: permanent drift against
#     aws_db_instance in modules/postgres. And copying a continuous backup
#     takes a fresh snapshot anyway.
#   - AWS Backup fails an RDS job scheduled during the instance's backup or
#     maintenance window or in the hour before either, and a job overlapping
#     the backup window branches the snapshot lineage. The root's default
#     schedule (07:00 UTC) clears 03:00-04:00 and Sun 04:30-05:30.
#   - On this single-AZ instance each snapshot starts with a brief I/O
#     suspension, "a few seconds to a few minutes" (RDS, "Creating a DB
#     snapshot"). It then completes in the background. The automated backups
#     measured 28-40 min end to end (RDS events, 2026-10-08 to 10-14), well
#     inside completion_window. That window counts from when the job STARTS,
#     not from the scheduled time ("after a backup job is successfully
#     started", AWS Backup API, CompletionWindowMinutes).
#
# The alarms are keyed to the schedule's cadence. CloudWatch caps an alarm's
# evaluation window at 7 days, so no "nothing completed for N hours" alarm can
# cover a weekly gap without false alarms:
#   - daily:  two staleness alarms, no backup job and no copy job COMPLETED for
#             26 h.
#   - weekly: three event alarms, a backup job FAILED or EXPIRED and a copy job
#             FAILED. Staleness detection on that cadence rests on coord's
#             infra observer (claim gap-infra-observer-rds).
# The cadence is read from `schedule`, so switching the schedule switches the
# alarms in the same apply. Only the two cron shapes below are accepted.
#
# `enabled` is the off-switch for the recurring cost. It gates the plan, the
# selection and the alarms, so turning it off stops NEW backups and copies. The
# vaults and the role stay: existing recovery points expire on their own
# lifecycle, and an empty vault costs nothing. The vaults keep
# force_destroy = false, so removing this module fails while they hold
# recovery points. That is the safe direction.

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      configuration_aliases = [aws.dr]
    }
  }
}

variable "environment" { type = string }

variable "enabled" {
  description = "Run the backup plan and its alarms. The off-switch for the recurring cost; see the root's postgres_offinstance_backup_enabled for the figure."
  type        = bool
}

variable "db_instance_arn" {
  description = "ARN of the RDS instance to back up (module.postgres.arn). Selected by ARN, not by tag."
  type        = string
}

variable "schedule" {
  description = "AWS Backup cron expression, UTC: daily cron(M H * * ? *) or weekly cron(M H ? * DAY *). The alarm set follows the cadence. Keep the hour clear of the instance's backup and maintenance windows and of the hour before each, or RDS jobs fail."
  type        = string

  validation {
    # Regex literals inline, not in a local: a validation that references
    # other objects needs Terraform 1.9, and providers.tf still allows 1.7.
    condition = (
      can(regex("^cron\\([0-9]{1,2} [0-9]{1,2} \\* \\* \\? \\*\\)$", var.schedule)) ||
      can(regex("^cron\\([0-9]{1,2} [0-9]{1,2} \\? \\* (SUN|MON|TUE|WED|THU|FRI|SAT|[1-7]) \\*\\)$", var.schedule))
    )
    error_message = "schedule must be a daily cron(M H * * ? *) or a weekly cron(M H ? * DAY *) AWS Backup expression: the module keys its alarms to one of those two cadences."
  }
}

variable "retention_days" {
  description = "Days each recovery point stays in this region's vault. The floor of 7 is AWS's recommended minimum warm retention; the claim's 30-day floor is enforced at the root."
  type        = number

  validation {
    # AWS Backup accepts 1 day to 100 years ("Backup plan options"). One week is
    # its RECOMMENDED minimum: "configure your retention policy with a warm
    # storage duration of at least one week" ("Metering, costs, and billing"),
    # because below it the next backup cannot be incremental.
    condition     = var.retention_days >= 7
    error_message = "retention_days must be at least 7, AWS Backup's recommended minimum warm retention: with less, the next backup may find no point in warm storage and be a full one."
  }
}

variable "dr_retention_days" {
  description = "Days each copy stays in the second region's vault. A copy is incremental only while the previous copy still exists there. The floor of 7 is AWS's recommendation; the claim's 30-day floor is enforced at the root."
  type        = number

  validation {
    condition     = var.dr_retention_days >= 7
    error_message = "dr_retention_days must be at least 7, AWS Backup's recommended minimum warm retention: a copy is incremental only while the previous copy still exists in that vault."
  }
}

variable "sns_topic_arn" {
  description = "Topic the alarms notify. Its policy must already let cloudwatch.amazonaws.com publish; modules/cost-control's topic does."
  type        = string
}

locals {
  # Same regex as the daily arm of var.schedule's validation, which admits
  # only the two shapes, so anything that is not daily is weekly.
  cadence = can(regex("^cron\\([0-9]{1,2} [0-9]{1,2} \\* \\* \\? \\*\\)$", var.schedule)) ? "daily" : "weekly"

  daily_alarms  = var.enabled && local.cadence == "daily"
  weekly_alarms = var.enabled && local.cadence == "weekly"
}

data "aws_partition" "current" {}

data "aws_region" "dr" {
  provider = aws.dr
}

# RDS's AWS-managed key in the second region. Calling DescribeKey on this
# predefined alias makes KMS create the AWS-managed key if the region has never
# used one. That happens once, at the first plan. Such a key cannot be disabled or
# scheduled for deletion, so no action on it can make the copies unrecoverable.
# A customer-managed key is what a later cross-ACCOUNT copy would need. The claim
# defers that to an operator decision, and switching keys later costs one full
# copy. Decided and notified as a security-surface fork, coord finding
# 71335077-7de0-4dc4-b516-2bc8e06a943c.
data "aws_kms_key" "rds_dr" {
  provider = aws.dr
  key_id   = "alias/aws/rds"
}

resource "aws_backup_vault" "primary" {
  name = "qontinui-${var.environment}-rds"
}

resource "aws_backup_vault" "dr" {
  provider = aws.dr
  name     = "qontinui-${var.environment}-rds-dr"
  # The KEY ARN, never the alias ARN. kms_key_arn is ForceNew and AWS stores and
  # returns the key ARN, so an alias ARN would plan a replacement of this vault
  # on every run. That replacement would then fail, because force_destroy is
  # false and the vault holds recovery points.
  kms_key_arn = data.aws_kms_key.rds_dr.arn
}

# The role AWS Backup assumes to snapshot, copy and restore. It carries the
# AWS-managed backup and restore policies rather than a hand-enumerated set. The
# managed policies track the service as AWS changes it. A hand-written list rests
# on the premise that it names every call AWS Backup makes, and breaks silently
# when that stops being true. Only backup.amazonaws.com can assume the role.
# Only a principal holding iam:PassRole plus backup:Start* (an admin, who already
# holds rds:*) can direct it. Same fork notification as above.
data "aws_iam_policy_document" "backup_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name               = "qontinui-${var.environment}-backup"
  description        = "AWS Backup service role for the off-instance RDS backups (modules/backup)."
  assume_role_policy = data.aws_iam_policy_document.backup_assume.json
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "restore" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores"
}

resource "aws_backup_plan" "rds" {
  count = var.enabled ? 1 : 0
  name  = "qontinui-${var.environment}-rds"

  rule {
    rule_name                = "snapshot-and-copy-to-${data.aws_region.dr.name}"
    target_vault_name        = aws_backup_vault.primary.name
    schedule                 = var.schedule
    start_window             = 60
    completion_window        = 300
    enable_continuous_backup = false

    lifecycle {
      delete_after = var.retention_days
    }

    copy_action {
      destination_vault_arn = aws_backup_vault.dr.arn

      lifecycle {
        delete_after = var.dr_retention_days
      }
    }

    recovery_point_tags = {
      Claim = "r02-off-instance-backup"
    }
  }
}

resource "aws_backup_selection" "rds" {
  count        = var.enabled ? 1 : 0
  name         = "qontinui-${var.environment}-rds"
  plan_id      = aws_backup_plan.rds[0].id
  iam_role_arn = aws_iam_role.backup.arn
  resources    = [var.db_instance_arn]

  # The role must hold its grants before the selection exists, so that no job
  # can start under a role that cannot yet snapshot or copy.
  depends_on = [
    aws_iam_role_policy_attachment.backup,
    aws_iam_role_policy_attachment.restore,
  ]
}

# ─── Alarms, daily cadence: staleness ───────────────────────────────────
#
# Staleness, not failure: these page when no backup job, and no copy job, has
# COMPLETED for 26 hours. A failed job, a job that expired in its start window,
# a deleted plan and a plan that never ran all look the same here. A failure
# alarm would see only the first of those.
#
# AWS Backup reports these metrics only when they are non-zero, so a quiet hour
# is missing data and treat_missing_data = "breaching" counts it. The alarm
# fires once 26 consecutive hourly periods hold no completed job. The daily gap
# between completions is at most 24 h, plus the 60-minute start window, plus
# the snapshot's run-time spread, so it stays under 26 periods. The 26 x 3600 s
# window is valid only because the period is a full hour: PutMetricAlarm limits
# a sub-hour period to a one-day window.
#
# This design needs NumberOf*JobsCompleted to be an event count, non-zero only
# in the hour a job finished. If it turns out to be a running total that stays
# non-zero, the alarm goes OK once and can never fire again. The post-apply
# check reads the hourly series to settle that.
#
# Both alarms sit in ALARM from apply until the first scheduled run completes,
# up to about a day, because there is no datapoint yet. That sends two expected
# ALARM emails. The flip to OK is the post-apply proof that the dimension
# resolves, the same evidence qontinui-stack#94 used for its storage alarm.
#
# A copy job's vault-name dimension is its DESTINATION vault: "The vault name of
# copy jobs is that of their destination vault" (AWS Backup guide, CloudWatch
# metrics). The docs do not say which region emits it. It is expected here, in
# the source region, where copy jobs are listed. If the post-apply list-metrics
# read finds it only in the second region, this alarm stays in ALARM, which
# fails loud. It then moves there with a topic of its own.
resource "aws_cloudwatch_metric_alarm" "backup_stale" {
  count               = local.daily_alarms ? 1 : 0
  alarm_name          = "qontinui-${var.environment}-rds-backup-stale"
  alarm_description   = "No AWS Backup job COMPLETED for the coord RDS instance in vault ${aws_backup_vault.primary.name} for 26 h: the off-instance backup has stopped (failed, expired in its start window, or the plan is gone). Check aws backup list-backup-jobs --by-backup-vault-name ${aws_backup_vault.primary.name}. A stopped RDS instance cannot be snapshotted."
  namespace           = "AWS/Backup"
  metric_name         = "NumberOfBackupJobsCompleted"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 26
  datapoints_to_alarm = 26
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    BackupVaultName = aws_backup_vault.primary.name
  }

  alarm_actions = [var.sns_topic_arn]
  ok_actions    = [var.sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "copy_stale" {
  count               = local.daily_alarms ? 1 : 0
  alarm_name          = "qontinui-${var.environment}-rds-backup-copy-stale"
  alarm_description   = "No AWS Backup copy job COMPLETED into ${aws_backup_vault.dr.name} (${data.aws_region.dr.name}) for 26 h: the second-region copy of the coord database has stopped. Check aws backup list-copy-jobs --by-destination-vault-arn ${aws_backup_vault.dr.arn}."
  namespace           = "AWS/Backup"
  metric_name         = "NumberOfCopyJobsCompleted"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 26
  datapoints_to_alarm = 26
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    BackupVaultName = aws_backup_vault.dr.name
  }

  alarm_actions = [var.sns_topic_arn]
  ok_actions    = [var.sns_topic_arn]
}

# ─── Alarms, weekly cadence: failure events ─────────────────────────────
#
# One week between jobs is longer than any staleness window CloudWatch allows,
# so this cadence watches the failure events instead: Sum >= 1 in an hour.
# These counts are reported only when non-zero, so a quiet hour is
# notBreaching rather than an alarm.
#
# There are no ok_actions. These alarms return to OK an hour after the event
# whether or not the backup was retried, so an OK email would read as
# "recovered" when nothing recovered. The same reasoning is behind the
# observability module's pr-hydration alarm.
resource "aws_cloudwatch_metric_alarm" "backup_failed" {
  count               = local.weekly_alarms ? 1 : 0
  alarm_name          = "qontinui-${var.environment}-rds-backup-failed"
  alarm_description   = "An AWS Backup job for the coord RDS instance FAILED in vault ${aws_backup_vault.primary.name} (weekly cadence). Check aws backup list-backup-jobs --by-state FAILED --by-backup-vault-name ${aws_backup_vault.primary.name}; RDS rejects jobs scheduled inside or within an hour before its backup or maintenance window. OK one hour later is NOT recovery."
  namespace           = "AWS/Backup"
  metric_name         = "NumberOfBackupJobsFailed"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    BackupVaultName = aws_backup_vault.primary.name
  }

  alarm_actions = [var.sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "backup_expired" {
  count               = local.weekly_alarms ? 1 : 0
  alarm_name          = "qontinui-${var.environment}-rds-backup-expired"
  alarm_description   = "An AWS Backup job for the coord RDS instance EXPIRED in vault ${aws_backup_vault.primary.name} (weekly cadence): it could not start within its 60-minute start window, so that week has no backup. Check aws backup list-backup-jobs --by-state EXPIRED --by-backup-vault-name ${aws_backup_vault.primary.name}. OK one hour later is NOT recovery."
  namespace           = "AWS/Backup"
  metric_name         = "NumberOfBackupJobsExpired"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    BackupVaultName = aws_backup_vault.primary.name
  }

  alarm_actions = [var.sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "copy_failed" {
  count               = local.weekly_alarms ? 1 : 0
  alarm_name          = "qontinui-${var.environment}-rds-backup-copy-failed"
  alarm_description   = "An AWS Backup copy job into ${aws_backup_vault.dr.name} (${data.aws_region.dr.name}) FAILED (weekly cadence): the second-region copy of the coord database is missing for that week. Check aws backup list-copy-jobs --by-state FAILED --by-destination-vault-arn ${aws_backup_vault.dr.arn}. OK one hour later is NOT recovery."
  namespace           = "AWS/Backup"
  metric_name         = "NumberOfCopyJobsFailed"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    BackupVaultName = aws_backup_vault.dr.name
  }

  alarm_actions = [var.sns_topic_arn]
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "vault_name" { value = aws_backup_vault.primary.name }
output "vault_arn" { value = aws_backup_vault.primary.arn }
output "dr_vault_name" { value = aws_backup_vault.dr.name }
output "dr_vault_arn" { value = aws_backup_vault.dr.arn }
output "dr_region" { value = data.aws_region.dr.name }
output "role_arn" { value = aws_iam_role.backup.arn }
output "plan_id" { value = var.enabled ? aws_backup_plan.rds[0].id : null }
output "cadence" { value = local.cadence }
