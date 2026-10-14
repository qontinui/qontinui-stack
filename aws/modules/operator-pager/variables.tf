variable "environment" { type = string }

variable "email_topic_arn" {
  description = "The existing alert topic (qontinui-<env>-budget-alerts, one confirmed email subscription). The relay's sns_forward adapter publishes pages there, it is the fallback when a vendor fails, and the pager's own health alarms notify it."
  type        = string
}

variable "coord_task_role_arn" {
  description = "coord's ECS task role. Its operator leg (plan Phase 3) publishes coord-side pages to the pager topic."
  type        = string
}

variable "coord_deploy_role_name" {
  description = "The hand-managed role deploy-coord.yml assumes (qontinui-coord deploy/BOOTSTRAP.md). Its CI rollback step (plan Phase 3) publishes deploy_failed pages."
  type        = string
  default     = "qontinui-coord-deploy"
}

variable "enabled" {
  description = "Off-switch: false removes the relay's subscription, so nothing published to the pager topic reaches a human. Every source keeps publishing; the relay stops delivering."
  type        = bool
  default     = true
}

variable "adapter" {
  description = "How the relay delivers. sns_forward forwards each page to email_topic_arn; it is the only adapter until the operator's vendor decision (plan Phase 6)."
  type        = string
  default     = "sns_forward"
  validation {
    condition     = contains(["sns_forward"], var.adapter)
    error_message = "This build has only the sns_forward adapter. A vendor adapter arrives with plan Phase 6."
  }
}

variable "vendor_parameter_prefix" {
  description = "Parameter Store path under which the operator saves the vendor key in the AWS Console (plan D4). No agent ever writes or reads its value."
  type        = string
  default     = "/qontinui/ops/operator-pager/"
}

variable "vendor_parameter_region" {
  description = "Region of the operator-staged parameter: eu-central-1, the established home of /qontinui/ops/* values. The relay builds its SSM client for this region."
  type        = string
  default     = "eu-central-1"
}

variable "relay_log_retention_days" {
  description = "Retention of the relay's log group, which holds the one outcome record per message that coord ingests (plan Phase 5)."
  type        = number
  default     = 90
}

# ─── Phase 2: the AWS-side detectors ────────────────────────────────────

variable "alb_arn_suffix" {
  description = "The ALB's arn_suffix, for coord's HealthyHostCount and TargetResponseTime."
  type        = string
}

variable "coord_tg_arn_suffix" {
  description = "coord's target group arn_suffix."
  type        = string
}

variable "coord_cluster_name" {
  description = "The ECS cluster coord and web run in."
  type        = string
}

variable "coord_service_name" {
  description = "coord's ECS service name (the ECS task group is service:<name>)."
  type        = string
}

variable "web_service_name" {
  description = "web's ECS service name. A failed web deployment pages deploy_failed too."
  type        = string
}

variable "postgres_instance_identifier" {
  description = "The RDS instance whose storage, IOPS and storage events page."
  type        = string
}

