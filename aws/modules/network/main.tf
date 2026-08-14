# VPC + public/private subnets across N AZs + 4 Security Groups.
#
# SG roles:
#   alb_sg          — accepts 80/443 from anywhere (public ingress)
#   client_sg       — attached to the ECS coord task; can reach data plane
#   data_plane_sg   — attached to RDS + ElastiCache; only accepts traffic
#                     from client_sg (and itself, for ElastiCache repl)
#   bastion_sg      — reserved for future SSM tunnels; no rules in v0
#
# This shape is the canonical "ALB → app → managed-data-stores" pattern.

variable "environment" { type = string }
variable "vpc_cidr" { type = string }
variable "az_count" { type = number }

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # Carve /20 chunks: first half public, second half private.
  public_cidrs  = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  private_cidrs = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i + 8)]
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "qontinui-${var.environment}" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "qontinui-${var.environment}-igw" }
}

resource "aws_subnet" "public" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.public_cidrs[count.index]
  availability_zone = local.azs[count.index]

  map_public_ip_on_launch = true

  tags = {
    Name = "qontinui-${var.environment}-public-${local.azs[count.index]}"
    Tier = "public"
  }
}

resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = {
    Name = "qontinui-${var.environment}-private-${local.azs[count.index]}"
    Tier = "private"
  }
}

# NAT in one AZ only — cost optimization. Prod should use one NAT per AZ.
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "qontinui-${var.environment}-nat" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "qontinui-${var.environment}-nat" }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "qontinui-${var.environment}-public-rt" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = { Name = "qontinui-${var.environment}-private-rt" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ─── VPC Endpoints ──────────────────────────────────────────────────────
#
# Without this, EVERY byte the Fargate tasks exchange with S3 leaves via the
# NAT gateway and is billed at $0.045/GB of data processing on top of the NAT's
# own ~$32/month. Measured over 2026-07-24 → 08-02 the NAT carried ~55 GB/day
# (~85% of it inbound and near-constant day to day — the signature of machine
# traffic, not human), i.e. ~$74/month of pure processing charges.
#
# A GATEWAY endpoint is the only endpoint type that is unconditionally worth
# adding: no hourly charge, no per-GB charge, no volume at which it fails to pay
# off. It captures more than "S3" suggests, because ECR image layers are served
# from S3 — a large fraction of every container pull shifts off the NAT without
# any ECR endpoint at all.
#
# INTERFACE endpoints (ecr.api, ecr.dkr, logs, secretsmanager) are deliberately
# NOT here: each costs ~$0.01/hr/AZ (~$15/month across 2 AZs) PLUS $0.01/GB, so
# the break-even is ~420 GB/month per service ($14.60 / ($0.045 - $0.010)) and
# adding one below that INCREASES the bill. The account has no VPC flow logs, so
# nothing currently attributes the NAT volume by destination — that measurement
# is the prerequisite, not a formality.
#
# The ssmmessages/ssm/ec2messages family is excluded UNCONDITIONALLY, not on
# cost grounds: those are network plumbing in form and an interactive-access
# transport in effect (ECS Exec), and a cost change has no business widening
# that surface.
#
# Plan: plans/2026-08-03-vpc-endpoints-nat-egress-baseline.md

# This module takes only environment/vpc_cidr/az_count — there is no var.region
# here, and adding one would mean changing the module call in aws/staging/main.tf
# too. Resolve it from the provider instead, as modules/coord/main.tf already does.
data "aws_region" "current" {}

# NOTE: no aws_vpc_endpoint_policy. The default (full-access) endpoint policy is
# the correct one here. A policy scoped to "our own buckets" would break the ECR
# image pulls this endpoint exists to move off the NAT, because ECR layers live
# in AWS-owned buckets (prod-<region>-starport-layer-bucket-*), not in this
# account. Traffic through the endpoint is still gated by each caller's own IAM;
# the endpoint changes the path, not the permissions.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"

  # Private table only. The public table routes via the IGW and pays no NAT
  # processing, so associating it buys nothing. If az_count ever yields per-AZ
  # private route tables, this must become a list.
  route_table_ids = [aws_route_table.private.id]

  # Name only. `Environment` is already supplied by the provider's `default_tags`
  # (aws/staging/providers.tf), so declaring it here too made this the ONE resource
  # in the repo that sets a default tag at resource level — and that is not
  # cosmetic. On `terraform import` the provider records `tags` as live-minus-
  # default_tags, i.e. `{Name}`, while the config asked for `{Name, Environment}`,
  # so the imported endpoint planned a permanent 1-to-change that no apply could
  # settle. Dropping it leaves `tags_all` — and therefore the live endpoint —
  # byte-identical. (Found importing this endpoint, 2026-08-15; plan
  # 2026-08-04-stack-terraform-state-reconciliation P2.)
  tags = {
    Name = "qontinui-${var.environment}-s3-endpoint"
  }
}

# ─── Security Groups ────────────────────────────────────────────────────

resource "aws_security_group" "alb" {
  name        = "qontinui-${var.environment}-alb"
  description = "ALB ingress"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "qontinui-${var.environment}-alb" }
}

resource "aws_security_group" "client" {
  name        = "qontinui-${var.environment}-client"
  description = "ECS task SG; reaches data plane"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "qontinui-${var.environment}-client" }
}

# Allow ALB → coord task on the coord port.
resource "aws_security_group_rule" "client_from_alb_9870" {
  type                     = "ingress"
  from_port                = 9870
  to_port                  = 9870
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
  security_group_id        = aws_security_group.client.id
  description              = "ALB to coord task"
}

# Allow coord task → coord task on the coord port (HA Phase C git-plane
# replication). With desired_count >= 2 a follower bootstraps, and the leader
# replicates, by dialing the PEER's :9870 git-http endpoint DIRECTLY
# (task-to-task within this SG) — not via the ALB. The ALB rule above only
# covers ALB→task, so without this self-ingress the follower's bootstrap fetch
# to the leader's internal endpoint (e.g. http://ip-10-20-x-y.ec2.internal:9870)
# silently TIMES OUT (SYNs dropped), the follower never reaches `in_sync`, and
# no git-plane replication happens. Mirrors `data_self` for the data-plane SG.
resource "aws_security_group_rule" "client_self_9870" {
  type              = "ingress"
  from_port         = 9870
  to_port           = 9870
  protocol          = "tcp"
  self              = true
  security_group_id = aws_security_group.client.id
  description       = "coord task to coord task (HA Phase C replication, port 9870)"
}

resource "aws_security_group" "data_plane" {
  name        = "qontinui-${var.environment}-data-plane"
  description = "RDS + ElastiCache; ingress from client_sg only"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "qontinui-${var.environment}-data-plane" }
}

resource "aws_security_group_rule" "data_pg_from_client" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.client.id
  security_group_id        = aws_security_group.data_plane.id
  description              = "client to RDS Postgres"
}

resource "aws_security_group_rule" "data_redis_from_client" {
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.client.id
  security_group_id        = aws_security_group.data_plane.id
  description              = "client to ElastiCache Redis"
}

# Self-ingress for ElastiCache replication group internals.
resource "aws_security_group_rule" "data_self" {
  type              = "ingress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  self              = true
  security_group_id = aws_security_group.data_plane.id
  description       = "data plane self-ingress (Redis replication)"
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "vpc_id" { value = aws_vpc.main.id }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "alb_sg_id" { value = aws_security_group.alb.id }
output "client_sg_id" { value = aws_security_group.client.id }
output "data_plane_sg_id" { value = aws_security_group.data_plane.id }
output "s3_endpoint_id" { value = aws_vpc_endpoint.s3.id }
