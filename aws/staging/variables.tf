variable "region" {
  description = "AWS region."
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
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of AZs to span. 2 is minimum for an ALB; single-AZ for cost is achieved by setting RDS multi_az=false, not by going to 1 AZ here."
  type        = number
  default     = 2
}

# ─── DNS / TLS ──────────────────────────────────────────────────────────

variable "domain_name" {
  description = "Base domain (e.g. qontinui.io). Coord ingress goes to <coord_subdomain>.<domain>."
  type        = string
}

variable "route53_zone_id" {
  description = "Existing Route53 hosted zone id for var.domain_name."
  type        = string
}

variable "coord_subdomain" {
  description = "Subdomain for the coord ALB ingress."
  type        = string
  default     = "coord"
}

variable "web_subdomain" {
  description = "Subdomain for the qontinui-web backend ALB ingress."
  type        = string
  default     = "api"
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
  description = "Web task replicas."
  type        = number
  default     = 1
}

# ─── Cross-IdP account linking (Cognito) ─────────────────────────────────

variable "cognito_user_pool_arn" {
  description = <<-EOT
    ARN of the Cognito user pool used for federated auth + cross-IdP account
    linking. This pool is MANUALLY managed and intentionally NOT in Terraform
    (never imported) — it is referenced by ARN only. Two things scope to it:
      1. the web ECS task role's cognito-idp admin grant (module.web), and
      2. the PreSignUp auto-link Lambda's grant + invoke permission
         (module.cross_idp_linking).
    The PreSignUp trigger attachment on the pool itself is a one-time manual
    `aws cognito-idp update-user-pool` step (pool not in TF) — see
    modules/cross-idp-linking/main.tf.
  EOT
  type        = string
  default     = "arn:aws:cognito-idp:us-east-1:047719635665:userpool/us-east-1_rgTB9dbZ1"
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
  description = "RDS instance class. db.t4g.micro is current sizing."
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
  description = "Multi-AZ RDS. Currently single-AZ; set true when HA is needed."
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
  description = "ElastiCache node type. cache.t4g.micro is current sizing."
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
  description = <<-EOT
    DESIRED BASELINE replica count for the coord ECS service (HA Phase C).
    Default raised to 2 so Terraform provisions the service with at least two
    tasks spread across the two private subnets (one per AZ — see az_count).

    IMPORTANT — this value is the baseline written into the Terraform state.
    The LIVE running count is managed operationally by the replica-management
    stop/start scripts (aws/scripts/stop.sh + start.sh), which call
    `aws ecs update-service --desired-count N` directly.  Because the ECS
    service resource has `lifecycle { ignore_changes = [desired_count] }`,
    running `terraform apply` will NOT override whatever count the scripts last
    set.  The baseline here only takes effect on a fresh `terraform apply`
    against a service that does not yet exist, or after an explicit
    `terraform apply -target=module.coord.aws_ecs_service.coord`.

    Cross-reference: HA Phase C plan — coord HA Phase C.6 (multi-AZ replica
    baseline + chaos validation).
  EOT
  type        = number
  default     = 2
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
