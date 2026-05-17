# Composition root for qontinui-stack staging on AWS.
#
# Wires the modules and runs nothing else here. Each module is independently
# usable; the staging environment composes them in a topology that matches
# qontinui-stack/docker-compose.yml with managed-service substitutions.
#
# Service topology:
#
#   ┌─ Internet ─┐
#   │            │
#   v            v
#  ALB (TLS)    GitHub webhook
#   │            │
#   └─→ ECS Fargate cluster (qontinui-staging)
#          ├─ coord service  (coord.<domain>  — listener default)
#          └─ web service     (web.<domain>   — host-routed listener rule)
#          │       │
#          v       v
#      RDS PG   ElastiCache Redis
#       │  └─ db qontinui_db   (coord, Rust)
#       └──── db qontinui_web  (web, FastAPI/Alembic — one-off migrate task)
#                  │
#               (also reachable from runner clients via Tailscale/VPN —
#                see "Client connectivity" in README)

# ─── Shared secret: web↔coord strategy bridge ───────────────────────────
# coord reads COORD_ADMIN_SECRET to gate POST /coord/auth/service-token.
# Generated here (fresh clean-room staging — intentionally NOT mirrored
# from the local dev .env). The web backend (deferred — see plan
# "DEFERRED-ON-IMAGE-SLIM") will consume the same value when it ships.
#
# DB topology: CANONICAL ONE-DB model. coord + (future) web share
# `qontinui_db`; the unified qontinui-web alembic chain (run via
# qontinui-canonical-migrator) creates `coord.*` AND `public.*`. The
# earlier separate-`qontinui_web`-DB design was removed (it was premised
# on coord/web schemas being independent — they are not; 45 web alembic
# migrations build coord.*, incl. `CREATE SCHEMA coord`).

resource "random_password" "coord_admin_secret" {
  length  = 48
  special = false
}

# ─── Network ────────────────────────────────────────────────────────────

module "network" {
  source = "../modules/network"

  environment = var.environment
  vpc_cidr    = var.vpc_cidr
  az_count    = var.az_count
}

# ─── Postgres ───────────────────────────────────────────────────────────

module "postgres" {
  source = "../modules/postgres"

  environment           = var.environment
  vpc_id                = module.network.vpc_id
  subnet_ids            = module.network.private_subnet_ids
  data_plane_sg_id      = module.network.data_plane_sg_id
  client_sg_id          = module.network.client_sg_id
  instance_class        = var.postgres_instance_class
  allocated_storage_gb  = var.postgres_allocated_storage_gb
  max_allocated_storage = var.postgres_max_allocated_storage_gb
  username              = var.postgres_username
  db_name               = var.postgres_db_name
  multi_az              = var.postgres_multi_az
  backup_retention_days = var.postgres_backup_retention_days
}

# ─── Redis ──────────────────────────────────────────────────────────────

module "redis" {
  source = "../modules/redis"

  environment      = var.environment
  vpc_id           = module.network.vpc_id
  subnet_ids       = module.network.private_subnet_ids
  data_plane_sg_id = module.network.data_plane_sg_id
  client_sg_id     = module.network.client_sg_id
  node_type        = var.redis_node_type
}

# ─── Blob ───────────────────────────────────────────────────────────────

module "blob" {
  source = "../modules/blob"

  environment = var.environment
}

# ─── Coord service (ECS Fargate) ────────────────────────────────────────

module "coord" {
  source = "../modules/coord"

  environment        = var.environment
  region             = var.region
  vpc_id             = module.network.vpc_id
  public_subnet_ids  = module.network.public_subnet_ids
  private_subnet_ids = module.network.private_subnet_ids
  client_sg_id       = module.network.client_sg_id
  alb_sg_id          = module.network.alb_sg_id

  image_uri     = var.coord_image_uri
  cpu           = var.coord_cpu
  memory_mb     = var.coord_memory_mb
  desired_count = var.coord_desired_count

  database_url          = module.postgres.connection_string
  redis_url             = module.redis.connection_string
  github_webhook_secret = var.coord_github_webhook_secret
  coord_admin_secret    = random_password.coord_admin_secret.result

  s3_bucket_arn = module.blob.bucket_arn
}

# ─── Canonical-DB migrator (one-off alembic upgrade head) ───────────────
# Creates coord.* + public.* in qontinui_db. SQLAlchemy/alembic need the
# `postgresql://` scheme; the postgres module emits `postgres://`.

module "migrator" {
  source = "../modules/migrator"

  environment = var.environment
  region      = var.region
  image_uri   = var.migrator_image_uri
  # alembic env.py feeds DATABASE_URL through ConfigParser, which treats
  # `%` as interpolation. The urlencoded RDS password contains %XX; escape
  # `%`→`%%` so ConfigParser unescapes back to the real %XX before
  # SQLAlchemy URL-decodes it. coord consumes the raw DSN via env (no
  # ConfigParser) so its module input is deliberately NOT escaped.
  database_url = replace(replace(module.postgres.connection_string, "postgres://", "postgresql://"), "%", "%%")
}

# ─── Web backend service — DEFERRED ─────────────────────────────────────
# modules/web removed: the 13.6 GB web image is undeployable as-is and
# the separate-DB design was premised on a wrong DB-topology framing.
# Re-introduced (canonical one-DB; slimmed image) by the follow-up plan
# `2026-05-17-web-image-slim.md`. Tunnel keeps dormant web-host routing.

# ─── Tunnel (ALB + ACM + Route53) ───────────────────────────────────────

module "tunnel" {
  source = "../modules/tunnel"

  environment        = var.environment
  vpc_id             = module.network.vpc_id
  public_subnet_ids  = module.network.public_subnet_ids
  alb_sg_id          = module.network.alb_sg_id
  domain_name        = var.domain_name
  route53_zone_id    = var.route53_zone_id
  coord_subdomain    = var.coord_subdomain
  coord_target_group = module.coord.target_group_arn
  web_subdomain      = var.web_subdomain
  web_target_group   = "" # web deferred; dormant host-routing (count-guarded)
}

# ─── Cost control (budget + SNS-email alert) ────────────────────────────

module "cost_control" {
  source = "../modules/cost-control"

  environment   = var.environment
  monthly_limit = var.budget_monthly_limit
  alert_email   = var.budget_alert_email
}

# ─── Observability (CloudWatch alarms for coord) ────────────────────────

module "observability" {
  source = "../modules/observability"

  environment         = var.environment
  alb_arn_suffix      = module.tunnel.alb_arn_suffix
  coord_tg_arn_suffix = module.coord.target_group_arn_suffix
  sns_topic_arn       = module.cost_control.sns_topic_arn
}
