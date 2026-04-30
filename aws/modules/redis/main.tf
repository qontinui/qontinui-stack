# ElastiCache Redis 7 — single-node staging, replication group prepped for
# Multi-AZ promotion. AUTH token + TLS in transit on by default; matches
# the password gate the local stack uses.

variable "environment"      { type = string }
variable "vpc_id"            { type = string }
variable "subnet_ids"        { type = list(string) }
variable "data_plane_sg_id"  { type = string }
variable "client_sg_id"      { type = string }
variable "node_type"         { type = string }

resource "random_password" "auth" {
  length  = 64
  special = false # ElastiCache AUTH tokens: alphanumeric only
}

resource "aws_secretsmanager_secret" "auth" {
  name        = "qontinui/${var.environment}/redis/auth"
  description = "ElastiCache AUTH token for qontinui-${var.environment}"
}

resource "aws_secretsmanager_secret_version" "auth" {
  secret_id     = aws_secretsmanager_secret.auth.id
  secret_string = random_password.auth.result
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "qontinui-${var.environment}-redis"
  subnet_ids = var.subnet_ids
}

resource "aws_elasticache_parameter_group" "main" {
  name   = "qontinui-${var.environment}-redis7"
  family = "redis7"

  # Match the local stack's eviction policy.
  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "qontinui-${var.environment}"
  description          = "qontinui canonical Redis (${var.environment})"

  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.node_type
  num_cache_clusters   = 1               # staging single-node; prod ≥2 + multi_az_enabled
  parameter_group_name = aws_elasticache_parameter_group.main.name
  subnet_group_name    = aws_elasticache_subnet_group.main.name
  port                 = 6379

  security_group_ids = [var.data_plane_sg_id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.auth.result
  auth_token_update_strategy = "ROTATE"

  multi_az_enabled           = false
  automatic_failover_enabled = false

  apply_immediately       = false
  auto_minor_version_upgrade = true
  snapshot_retention_limit   = 1   # staging keeps 1 daily snapshot

  tags = { Name = "qontinui-${var.environment}-redis" }

  lifecycle {
    ignore_changes = [auth_token]
  }
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "primary_endpoint" {
  value = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "port" { value = 6379 }

output "auth_secret_arn" {
  value = aws_secretsmanager_secret.auth.arn
}

# rediss:// because TLS-in-transit is on. Password URL-encoded inline.
output "connection_string" {
  value = "rediss://:${urlencode(random_password.auth.result)}@${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
  sensitive = true
}
