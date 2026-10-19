variable "environment" { type = string }

variable "enabled" {
  type        = bool
  default     = true
  description = "Off-switch for the operator pager (root tfvar operator_pager_enabled). false removes the relay's subscription to the pager topic, so nothing published there reaches the relay or the operator; every publisher, alarm and rule stays in place, so turning it back on is one apply."
}

variable "adapter" {
  type        = string
  default     = "sns_forward"
  description = "The relay's delivery adapter. Phase 1 ships only sns_forward (forward to the email topic under a [PAGE] prefix); the vendor adapter the operator chooses is added in Phase 6 (plan D3), and only that one (delete-over-deprecate)."
  validation {
    condition     = contains(["sns_forward"], var.adapter)
    error_message = "adapter must be one of the shipped adapters: sns_forward."
  }
}

variable "email_topic_arn" {
  type        = string
  description = "The existing email topic (cost-control's qontinui-<env>-budget-alerts). The sns_forward adapter and the vendor fallback publish here, and the delivery-health alarms notify here: deliberately NOT the pager, so a broken pager is reported on a channel that does not depend on it. From module.cost_control.sns_topic_arn."
}

variable "coord_task_role_arn" {
  type        = string
  description = "coord's ECS task role (qontinui-<env>-coord-task). Granted publish in the pager topic policy, and given sns:Publish + logs:FilterLogEvents by iam_coord.tf. From module.coord.task_role_arn."
}

variable "coord_deploy_role_name" {
  type        = string
  default     = "qontinui-coord-deploy"
  description = "coord's GitHub-OIDC deploy role, managed out of band from qontinui-coord deploy/iam/ (deploy/BOOTSTRAP.md). Named in the pager topic policy so deploy-coord.yml's post-rollback step can publish a deploy_failed page (plan D12). The role's own sns:Publish statement lives in that repo, not here."
}

variable "coord_cluster_name" {
  type        = string
  description = "ECS cluster name (service ARNs for the deploy-failed rule). From module.coord.cluster_name."
}

variable "coord_cluster_arn" {
  type        = string
  description = "ECS cluster ARN (the task-exit rule's clusterArn match). From module.coord.cluster_arn."
}

variable "coord_service_name" {
  type        = string
  description = "coord's ECS service name. From module.coord.service_name."
}

variable "web_service_name" {
  type        = string
  description = "web's ECS service name. From module.web.service_name."
}

variable "alb_arn_suffix" {
  type        = string
  description = "ALB ARN suffix (AWS/ApplicationELB dimension). From module.tunnel.alb_arn_suffix."
}

variable "coord_tg_arn_suffix" {
  type        = string
  description = "coord target group ARN suffix (AWS/ApplicationELB dimension). From module.coord.target_group_arn_suffix."
}

variable "postgres_instance_identifier" {
  type        = string
  description = "coord's RDS instance identifier (AWS/RDS dimension, RDS event SourceIdentifier, and the data.aws_db_instance read for provisioned IOPS). From module.postgres.identifier."
}

variable "operator_pager_ssm_region" {
  type        = string
  default     = "eu-central-1"
  description = "Region of the operator-staged vendor parameter /qontinui/ops/operator-pager/* (plan D4: the established home of operator-staged values, beside budget-alert-email). The relay builds its SSM client for this region."
}
