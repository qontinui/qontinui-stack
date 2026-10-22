output "database_url" {
  description = "Postgres DSN for ~/.qontinui/profiles.json staging profile."
  value       = module.postgres.connection_string
  sensitive   = true
}

output "redis_url" {
  description = "Redis URL for ~/.qontinui/profiles.json staging profile."
  value       = module.redis.connection_string
  sensitive   = true
}

output "blob" {
  description = "S3 blob configuration."
  value = {
    bucket   = module.blob.bucket_name
    region   = var.region
    endpoint = "https://s3.${var.region}.amazonaws.com"
  }
}

output "session_output_cold" {
  description = "Cold-tier PTY-output S3 config (Phase 8). coord derives its writer config from this. Key layout: tenant/<tenant_id>/session/<session_id>.log."
  value = {
    bucket     = module.session_output_cold.bucket_name
    region     = var.region
    endpoint   = "https://s3.${var.region}.amazonaws.com"
    key_prefix = module.session_output_cold.key_prefix
    ttl_days   = module.session_output_cold.cold_ttl_days
  }
}

output "coord_url" {
  description = "Public WSS URL for the coord service."
  value       = "wss://${var.coord_subdomain}.${var.domain_name}"
}

output "coord_https_url" {
  description = "Public HTTPS base URL for coord (REST: service-token mint, /strategy, /health)."
  value       = "https://${var.coord_subdomain}.${var.domain_name}"
}

# web_url / web_* outputs removed — web backend DEFERRED-ON-IMAGE-SLIM
# (follow-up plan 2026-05-17-web-image-slim.md). web.staging.qontinui.io
# DNS + ACM SAN remain provisioned (dormant) for that plan.

output "migrator_task_family" {
  description = "ECS task-def family for the canonical-DB migrator (run via aws ecs run-task; idempotent)."
  value       = module.migrator.task_family
}

output "migrator_log_group" {
  description = "CloudWatch log group for the canonical-DB migrator."
  value       = module.migrator.log_group
}

output "github_webhook_url" {
  description = "URL to register in GitHub repo webhook settings."
  value       = "https://${var.coord_subdomain}.${var.domain_name}/webhooks/github"
}

output "cross_idp_presignup_lambda_arn" {
  description = "ARN of the PreSignUp auto-link Lambda. Attach it to the Cognito pool's PreSignUp trigger via the manual `aws cognito-idp update-user-pool --lambda-config PreSignUp=<arn>` step (describe-then-merge; pool is not in Terraform). See modules/cross-idp-linking/main.tf."
  value       = module.cross_idp_linking.lambda_arn
}

output "budget_sns_topic_arn" {
  description = "SNS topic for budget alerts — confirm the email subscription AWS sends."
  value       = module.cost_control.sns_topic_arn
}

output "operator_pager_topic_arn" {
  description = "The operator pager topic (qontinui-<env>-operator-pager). qontinui-coord's task definition carries it as QONTINUI_COORD_OPERATOR_PAGER_TOPIC_ARN, and its deploy role's PublishOperatorPage statement scopes sns:Publish to it."
  value       = module.operator_pager.pager_topic_arn
}

output "operator_pager_relay_log_group_name" {
  description = "The pager relay's log group: one JSON record per message. qontinui-coord's task definition carries it as QONTINUI_COORD_OPERATOR_PAGER_RELAY_LOG_GROUP, and scripts/operator-pager-drill.sh reads its records back."
  value       = module.operator_pager.relay_log_group_name
}

output "operator_pager_alarm_names" {
  description = "The three pager composites, the only pager alarms that carry actions. scripts/operator-pager-drill.sh --target routing checks each routes to the pager topic."
  value       = module.operator_pager.pager_alarm_names
}

output "rds_endpoint" {
  description = "RDS endpoint (host:port). Internal — not for direct profile use."
  value       = module.postgres.endpoint
}

output "redis_endpoint" {
  description = "ElastiCache primary endpoint."
  value       = module.redis.primary_endpoint
}

output "ecs_cluster_name" {
  description = "ECS cluster — used by stop/start scripts."
  value       = module.coord.cluster_name
}

output "ecs_service_name" {
  description = "ECS service name — used by stop/start scripts."
  value       = module.coord.service_name
}
