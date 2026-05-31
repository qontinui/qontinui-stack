# Composition root for qontinui-stack production on AWS.
#
# The directory and `environment` variable still say "staging" — renaming
# would destroy and recreate data resources. This IS production.
#
# Wires the modules and runs nothing else here. Each module is independently
# usable; this environment composes them in a topology that matches
# qontinui-stack/docker-compose.yml with managed-service substitutions.
#
# Service topology:
#
#   ┌─ Internet ─┐
#   │            │
#   v            v
#  ALB (TLS)    GitHub webhook
#   │            │
#   └─→ ECS Fargate cluster (qontinui-staging — name is historical)
#          ├─ coord service  (coord.<domain>  — listener default)
#          └─ web service     (web.<domain>   — host-routed listener rule)
#          │       │
#          v       v
#      RDS PG   ElastiCache Redis
#         │
#         └─ db qontinui_db  ← canonical one-DB
#              ├─ coord.*    (coord Rust reads/writes)
#              ├─ public.*   (web models — projects, runners, sessions, …)
#              ├─ auth.*     (web fastapi-users — users, oauth)
#              └─ cloud.*    (cloud-control models — subscriptions, admin)
#         (also reachable from runner clients via Tailscale/VPN — see
#          "Client connectivity" in README)

# ─── Shared secret: web↔coord strategy bridge ───────────────────────────
# coord reads COORD_ADMIN_SECRET to gate POST /coord/auth/service-token;
# web mints service-tokens against that endpoint using the same value to
# call coord's /strategy/* APIs. Generated here (intentionally NOT mirrored
# from the local dev .env). Passed into both module.coord and module.web
# from this single source of truth.
#
# DB topology: CANONICAL ONE-DB model. coord + web share `qontinui_db`;
# the unified qontinui-web alembic chain (run via qontinui-canonical-
# migrator) creates `coord.*` + `public.*` + `auth.*` + `cloud.*`. The
# earlier separate-`qontinui_web`-DB design was removed — coord/web
# schemas are NOT independent; 45 web alembic migrations build coord.*,
# incl. `CREATE SCHEMA coord`. See proj_canonical_one_db_unified_alembic.

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

# ─── Cold-tier session output (Phase 8) ─────────────────────────────────
# Object-per-session PTY-output store, 90-day TTL. The COLD tier of the
# three-tier retention model (hot = JetStream replay buffer, warm =
# coord.session_output Postgres, cold = this bucket). Per-session key layout:
# tenant/<tenant_id>/session/<session_id>.log. coord is the sole writer/reader
# (least-privilege policy attached to its task role below). See
# modules/session-output-cold/main.tf and docs/session-output-cold-tier.md.
# `terraform apply` for this is an OPERATOR step — nothing is provisioned here.

module "session_output_cold" {
  source = "../modules/session-output-cold"

  environment   = var.environment
  cold_ttl_days = var.session_output_cold_ttl_days
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

  # Phase 8 cold tier — coord is the writer/reader. A least-privilege policy
  # scoped to ONLY this bucket (Put/Get/List, no Delete — TTL handles
  # expiry) is attached to coord's task role inside the coord module.
  session_output_cold_bucket_arn = module.session_output_cold.bucket_arn
  session_output_cold_key_prefix = module.session_output_cold.key_prefix
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

# ─── Web backend service (ECS Fargate) ──────────────────────────────────
# Re-introduced 2026-05-17 by the web-image-slim plan: the slim OSS image
# (1.91 GB, torch-free, ECR push <5 min) is layered with the proprietary
# cloud-control package via qontinui-stack/web-prod/Dockerfile, yielding
# the staging-prod-<sha> image that boots with cloud-control's models
# (Subscription, AdminNotificationSettings) registered. Canonical one-DB:
# DATABASE_URL is the same DSN as coord, scheme converted to postgresql://
# so SQLAlchemy/asyncpg accept it. cloud-control runtime models map to
# cloud.* tables that the qontinui-web alembic chain already created in
# qontinui_db. See plan `2026-05-17-web-image-slim.md`.

resource "random_password" "web_secret_key" {
  length  = 48
  special = false # 48 alphanum chars exceeds the 32-char pydantic min length; avoids escape concerns
}

module "web" {
  source = "../modules/web"

  environment        = var.environment
  region             = var.region
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  client_sg_id       = module.network.client_sg_id
  alb_sg_id          = module.network.alb_sg_id

  cluster_id = module.coord.cluster_id # reuse coord's cluster (one per env)

  image_uri     = var.web_image_uri
  cpu           = var.web_cpu
  memory_mb     = var.web_memory_mb
  desired_count = var.web_desired_count

  # postgres module emits postgres://...?sslmode=disable. Two transforms:
  #   1. postgres:// → postgresql:// — SQLAlchemy + asyncpg want the longer
  #      scheme. (Migrator does the same; coord consumes the raw form OK.)
  #   2. Strip ?sslmode=disable — qontinui-web's db/session.py only handles
  #      sslmode=require (it strips that and configures SSL via connect_args).
  #      For any other sslmode value the bare URL is forwarded to asyncpg
  #      which rejects "sslmode" as an unknown kwarg with
  #      `TypeError: connect() got an unexpected keyword argument 'sslmode'`.
  #      Web doesn't need plaintext-vs-TLS hinting in the URL anyway: the
  #      else branch in session.py defaults connect_args["ssl"] = False,
  #      which is what we want against this RDS (rds.force_ssl=0; coord
  #      already runs plaintext for the same reason). Follow-up: qontinui-web
  #      PR to make session.py handle sslmode=disable + sslmode=prefer.
  database_url = replace(replace(module.postgres.connection_string, "postgres://", "postgresql://"), "?sslmode=disable", "")

  coord_url          = "https://${var.coord_subdomain}.${var.domain_name}"
  coord_admin_secret = random_password.coord_admin_secret.result # same value coord uses
  secret_key         = random_password.web_secret_key.result

  frontend_url = var.frontend_url
  backend_url  = "https://${var.web_subdomain}.${var.domain_name}"

  # CORS allowlist:
  #   - exact list: Vercel frontend (qontinui.io etc.). Apex domains
  #     can't be expressed by the staging-subtree regex, so they stay
  #     exact-match.
  #   - regex: any subdomain of the domain_name. Covers
  #     `web.<domain>` (own backend), `demo.<domain>`
  #     (coordination-layer demo), and any future preview / test
  #     subdomain — no task-def revision needed when a new subdomain
  #     is provisioned.
  # The anchored `^...$` + per-component charset `[a-z0-9-]+` reject
  # suffix-attack origins like `<domain>.attacker.com`.
  # Localhost origins intentionally excluded in production — don't expand
  # the production surface to dev browsers; set ENVIRONMENT=development
  # to use the broader defaults in app/main.py.
  backend_cors_origins      = jsonencode([var.frontend_url])
  backend_cors_origin_regex = "^https://([a-z0-9-]+\\.)*${replace(var.domain_name, ".", "\\.")}$"
}

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
  web_target_group   = module.web.target_group_arn # web Fargate now active
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
  coord_cluster_name  = module.coord.cluster_name
  coord_service_name  = module.coord.service_name
}
