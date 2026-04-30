# RDS Postgres 16 with pgvector. Master credentials live in Secrets Manager;
# the coord task pulls them from there at runtime via the ECS task role.

variable "environment"           { type = string }
variable "vpc_id"                { type = string }
variable "subnet_ids"            { type = list(string) }
variable "data_plane_sg_id"      { type = string }
variable "client_sg_id"          { type = string }
variable "instance_class"        { type = string }
variable "allocated_storage_gb"  { type = number }
variable "max_allocated_storage" { type = number }
variable "username"              { type = string }
variable "db_name"               { type = string }
variable "multi_az"              { type = bool }
variable "backup_retention_days" { type = number }

resource "random_password" "master" {
  length  = 32
  special = true
  # RDS rejects /, @, " and space.
  override_special = "!#$%&*()-_=+[]{}<>?"
}

resource "aws_secretsmanager_secret" "master" {
  name        = "qontinui/${var.environment}/postgres/master"
  description = "RDS master credentials for qontinui-${var.environment}"
}

resource "aws_secretsmanager_secret_version" "master" {
  secret_id = aws_secretsmanager_secret.master.id
  secret_string = jsonencode({
    username = var.username
    password = random_password.master.result
    dbname   = var.db_name
  })
}

resource "aws_db_subnet_group" "main" {
  name       = "qontinui-${var.environment}-pg"
  subnet_ids = var.subnet_ids
  tags       = { Name = "qontinui-${var.environment}-pg-subnets" }
}

# Parameter group enables pgvector via shared_preload_libraries equivalent
# (vector is a regular extension; CREATE EXTENSION at app time suffices).
# We tune a few connection-management knobs to match the local stack.
resource "aws_db_parameter_group" "main" {
  name        = "qontinui-${var.environment}-pg16"
  family      = "postgres16"
  description = "qontinui canonical PG; idle timeout + keepalives"

  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "300000"
  }
  parameter {
    name  = "tcp_keepalives_idle"
    value = "60"
  }
  parameter {
    name  = "tcp_keepalives_interval"
    value = "10"
  }
  parameter {
    name  = "tcp_keepalives_count"
    value = "6"
  }
}

resource "aws_db_instance" "main" {
  identifier     = "qontinui-${var.environment}"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.username
  password = random_password.master.result
  port     = 5432

  vpc_security_group_ids = [var.data_plane_sg_id]
  db_subnet_group_name   = aws_db_subnet_group.main.name
  parameter_group_name   = aws_db_parameter_group.main.name

  multi_az                = var.multi_az
  publicly_accessible     = false
  backup_retention_period = var.backup_retention_days
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:30-sun:05:30"
  deletion_protection     = false        # staging — flip to true in prod
  skip_final_snapshot     = true         # staging — flip to false in prod
  apply_immediately       = false
  auto_minor_version_upgrade = true

  performance_insights_enabled = false   # staging — enable in prod for $7/mo
  monitoring_interval          = 0       # staging — enable enhanced mon in prod

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  tags = { Name = "qontinui-${var.environment}-pg" }

  lifecycle {
    ignore_changes = [password] # rotate via Secrets Manager rotation, not TF
  }
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "endpoint" {
  value = "${aws_db_instance.main.address}:${aws_db_instance.main.port}"
}

output "address" {
  value = aws_db_instance.main.address
}

output "port" {
  value = aws_db_instance.main.port
}

output "db_name" {
  value = var.db_name
}

output "username" {
  value = var.username
}

output "master_secret_arn" {
  value = aws_secretsmanager_secret.master.arn
}

# Connection string with the password URL-encoded inline. Sensitive output.
output "connection_string" {
  value = "postgres://${var.username}:${urlencode(random_password.master.result)}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${var.db_name}?sslmode=require"
  sensitive = true
}
