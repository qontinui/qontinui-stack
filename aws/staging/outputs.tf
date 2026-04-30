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

output "github_webhook_url" {
  description = "URL to register in GitHub repo webhook settings."
  value       = "https://${var.coord_subdomain}.${var.domain_name}/webhooks/github"
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
