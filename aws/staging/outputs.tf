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

output "budget_sns_topic_arn" {
  description = "SNS topic for budget alerts — confirm the email subscription AWS sends."
  value       = module.cost_control.sns_topic_arn
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
