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

variable "environment"  { type = string }
variable "vpc_cidr"     { type = string }
variable "az_count"     { type = number }

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
  description              = "ALB → coord task"
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
  description              = "client → RDS Postgres"
}

resource "aws_security_group_rule" "data_redis_from_client" {
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.client.id
  security_group_id        = aws_security_group.data_plane.id
  description              = "client → ElastiCache Redis"
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

output "vpc_id"             { value = aws_vpc.main.id }
output "public_subnet_ids"  { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "alb_sg_id"          { value = aws_security_group.alb.id }
output "client_sg_id"       { value = aws_security_group.client.id }
output "data_plane_sg_id"   { value = aws_security_group.data_plane.id }
