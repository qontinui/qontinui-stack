variable "region" {
  description = "AWS region for the staging environment."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment label — used in resource names and tags."
  type        = string
  default     = "staging"
}

# ─── Networking ─────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR block for the staging VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of AZs to span. 2 is minimum for an ALB; staging single-AZ for cost is achieved by setting RDS multi_az=false, not by going to 1 AZ here."
  type        = number
  default     = 2
}

# ─── DNS / TLS ──────────────────────────────────────────────────────────

variable "domain_name" {
  description = "Base domain (e.g. qontinui.io). Coord ingress goes to coord-staging.<domain>."
  type        = string
}

variable "route53_zone_id" {
  description = "Existing Route53 hosted zone id for var.domain_name."
  type        = string
}

variable "coord_subdomain" {
  description = "Subdomain for the coord ALB ingress."
  type        = string
  default     = "coord-staging"
}

variable "web_subdomain" {
  description = "Subdomain for the qontinui-web backend ALB ingress."
  type        = string
  default     = "web-staging"
}

variable "frontend_url" {
  description = "Vercel frontend origin — used for the web backend's CORS allow-list and absolute links."
  type        = string
}

# ─── Web backend service ────────────────────────────────────────────────

variable "web_image_uri" {
  description = "ECR URI of the qontinui-web-backend image. Push first (built from qontinui-web origin/main — strategy proxy lives there)."
  type        = string
  default     = ""
}

variable "migrator_image_uri" {
  description = "ECR URI of the canonical-DB migrator image (alembic upgrade head; built from origin/main qontinui-web alembic chain)."
  type        = string
}

variable "web_cpu" {
  description = "Fargate task CPU units for web. 512 = 0.5 vCPU (FastAPI + asyncpg)."
  type        = number
  default     = 512
}

variable "web_memory_mb" {
  description = "Fargate task memory (MB) for web."
  type        = number
  default     = 1024
}

variable "web_desired_count" {
  description = "Web task replicas. 1 for staging."
  type        = number
  default     = 1
}

# ─── Cost control ───────────────────────────────────────────────────────

variable "budget_monthly_limit" {
  description = "Monthly AWS Budget limit in USD."
  type        = string
  default     = "100"
}

variable "budget_alert_email" {
  description = "Email for budget threshold alerts (SNS + direct). Confirm the SNS subscription email AWS sends."
  type        = string
}

# ─── Postgres ───────────────────────────────────────────────────────────

variable "postgres_instance_class" {
  description = "RDS instance class. db.t4g.micro is staging-cheap; bump for prod."
  type        = string
  default     = "db.t4g.micro"
}

variable "postgres_allocated_storage_gb" {
  description = "Initial allocated storage. RDS auto-grows up to max_allocated_storage."
  type        = number
  default     = 20
}

variable "postgres_max_allocated_storage_gb" {
  description = "Storage auto-grow ceiling."
  type        = number
  default     = 100
}

variable "postgres_username" {
  description = "Master username."
  type        = string
  default     = "qontinui_user"
}

variable "postgres_db_name" {
  description = "Initial database name."
  type        = string
  default     = "qontinui_db"
}

variable "postgres_multi_az" {
  description = "Multi-AZ RDS. Set true for prod, false for staging."
  type        = bool
  default     = false
}

variable "postgres_backup_retention_days" {
  description = "RDS automated-backup retention. 7 is the default; 0 disables (don't)."
  type        = number
  default     = 7
}

# ─── Redis ──────────────────────────────────────────────────────────────

variable "redis_node_type" {
  description = "ElastiCache node type. cache.t4g.micro is staging-cheap."
  type        = string
  default     = "cache.t4g.micro"
}

# ─── Coord service ──────────────────────────────────────────────────────

variable "coord_image_uri" {
  description = <<-EOT
    ECR URI of the qontinui-canonical-coord image. Used ONLY by the
    initial `terraform apply` that creates the task definition; ongoing
    coord deploys SHA-pin a fresh revision via
    `scripts/push-coord-image.sh` + `aws ecs update-service`. The
    script tags every build as both `:<sha>` and `:staging`, so leaving
    this at `:staging` in terraform.tfvars is safe — terraform won't
    be re-applied for image changes.
  EOT
  type    = string
  # Default left empty so a missed push surfaces immediately at apply time.
  default = ""
}

variable "coord_cpu" {
  description = "Fargate task CPU units. 256 = 0.25 vCPU."
  type        = number
  default     = 256
}

variable "coord_memory_mb" {
  description = "Fargate task memory in MB."
  type        = number
  default     = 512
}

variable "coord_desired_count" {
  description = "Number of coord task replicas. 1 for staging, ≥2 + Multi-AZ for prod."
  type        = number
  default     = 1
}

variable "coord_github_webhook_secret" {
  description = "Shared HMAC secret for GitHub webhooks. Stored in Secrets Manager."
  type        = string
  sensitive   = true
}

# ─── Cold-tier session output (Phase 8) ─────────────────────────────────

variable "session_output_cold_ttl_days" {
  description = "Days after which a cold-tier PTY-output session object expires (S3 lifecycle). Phase 8 default is 90."
  type        = number
  default     = 90
}
