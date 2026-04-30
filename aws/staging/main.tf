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
#   └─→ ECS Fargate (coord, qontinui-coord image)
#          │       │
#          v       v
#      RDS PG   ElastiCache Redis
#                  │
#               (also reachable from runner clients via Tailscale/VPN —
#                see "Client connectivity" in README)

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

  environment             = var.environment
  vpc_id                  = module.network.vpc_id
  subnet_ids              = module.network.private_subnet_ids
  data_plane_sg_id        = module.network.data_plane_sg_id
  client_sg_id            = module.network.client_sg_id
  instance_class          = var.postgres_instance_class
  allocated_storage_gb    = var.postgres_allocated_storage_gb
  max_allocated_storage   = var.postgres_max_allocated_storage_gb
  username                = var.postgres_username
  db_name                 = var.postgres_db_name
  multi_az                = var.postgres_multi_az
  backup_retention_days   = var.postgres_backup_retention_days
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

  environment            = var.environment
  region                 = var.region
  vpc_id                 = module.network.vpc_id
  public_subnet_ids      = module.network.public_subnet_ids
  private_subnet_ids     = module.network.private_subnet_ids
  client_sg_id           = module.network.client_sg_id
  alb_sg_id              = module.network.alb_sg_id

  image_uri              = var.coord_image_uri
  cpu                    = var.coord_cpu
  memory_mb              = var.coord_memory_mb
  desired_count          = var.coord_desired_count

  database_url           = module.postgres.connection_string
  redis_url              = module.redis.connection_string
  github_webhook_secret  = var.coord_github_webhook_secret

  s3_bucket_arn          = module.blob.bucket_arn
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
}
