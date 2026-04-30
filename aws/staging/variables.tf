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
  description = "ECR URI of the qontinui-canonical-coord image. Push via scripts/push-coord-image.sh first."
  type        = string
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
