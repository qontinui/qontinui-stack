# Replication tunnel: a Tailscale subnet router running as a one-task Fargate
# service, the network path between the private RDS instance and the local
# Postgres subscriber that replicates it (plan
# 2026-10-15-coord-and-web-data-move-off-aws-by-logical-replication, design D2(a)
# with the vet's corrections; credentials per D2 "Credentials" and OD3).
#
# DISABLED BY DEFAULT. Every resource and data source below is counted on
# var.enabled, so the module plans zero resources and makes zero AWS reads until
# a composition turns it on. aws/staging calls it with enabled = false.
#
# What it builds when enabled:
#
#   tailnet ──WireGuard (outbound from AWS only)──> router task (public subnet,
#                                                    public IP, no inbound rules)
#     forward path:  local subscriber ─tailnet─> advertised DB subnet CIDRs
#                    ─> router task re-originates ─> RDS :5432
#                    (the one rule this module adds to the RDS group admits
#                    :5432 from the router group)
#     reverse path:  RDS ─> <service>.<namespace>:<forward_listen_port> (Cloud Map)
#                    ─> socat in the router task ─> tailscaled SOCKS5 proxy
#                    ─> local database's tailnet name:port
#                    (router group admits that port from the RDS group only; in
#                    staging that is the shared data-plane group, which
#                    ElastiCache also uses)
#
# Why each piece is shaped this way:
#   - Userspace networking. Fargate has no /dev/net/tun and no NET_ADMIN, so
#     tailscaled runs with --tun=userspace-networking. The task cannot be a VPC
#     route target (its ENI changes every restart and a Fargate ENI's
#     source/destination check cannot be disabled), so nothing in the VPC can
#     open a connection INTO the tailnet by routing. The reverse path therefore
#     goes through an explicit forwarder (socat over tailscaled's SOCKS5
#     outbound proxy), registered in Cloud Map so RDS connects by a name that
#     survives a task restart. That path is what D8's reverse subscription needs.
#   - No inbound rules on the router group except the forwarder port from the
#     RDS group. Security groups are stateful, so a public-IP task reaches
#     direct (non-DERP) WireGuard paths with no inbound rule at all.
#   - Advertise the DB subnet group's CIDRs, not the instance's /32: a
#     single-AZ instance's private IP can change on host replacement or a class
#     change, and a /32 would then silently stop matching. The tailnet ACL (held
#     in the operator's tailnet console, not here) narrows reach to :5432 from
#     the one tagged local host.
#   - Node state in SSM. A Fargate task has no disk; with --state=arn:aws:ssm:...
#     a restarted task keeps its node identity instead of spending a fresh auth
#     key. The parameter is deliberately NOT a Terraform resource: tailscaled
#     creates it on first boot and rewrites it on every state change, and an
#     aws_ssm_parameter resource would refresh the node's private key into
#     terraform state, which already holds too many plaintext secrets
#     (docs/terraform-state-secret-inventory.md). The task role is granted
#     Get/Put on exactly that one parameter name.
#   - The auth key never passes through Terraform or an agent. Terraform creates
#     an EMPTY Secrets Manager secret (no aws_secretsmanager_secret_version); the
#     operator mints a tag-scoped, pre-approved key in the tailnet console and
#     pastes it into that secret in the AWS console (OD3). The execution role
#     injects it into the task by ARN.
#   - One node identity, so never two tasks: the service deploys with
#     minimum 0% / maximum 100%, which stops the old task before starting a new
#     one. Two tasks sharing one SSM state would fight over the same node key.
#
# Enabling, in order (the D2(a) upgrade path: OD3(ii) made D2(e), the WireGuard
# module, the Phase 3 tunnel; the operator holds the tailnet-side steps):
#   1. enabled = true, running = false; set forward_target_host
#      (cloud_map_namespace_id comes from aws/staging's module.nats, which needs
#      nats_under_code = true). Apply: creates the empty
#      secret, the groups and rules, roles, log group, Cloud Map service, task
#      definition, and the ECS service at desired_count 0.
#   2. Operator pastes the auth key into the secret (OD3).
#   3. running = true. Apply: the one task starts, logs in, advertises routes.
#      Approve the routes in the tailnet console unless the ACL auto-approves.
# Turning it off again is running = false (keeps the node state) or
# enabled = false (deletes everything here; the SSM parameter tailscaled wrote
# stays and is deleted by hand if the node is retired).
#
# Cost (D17): 0.25 vCPU / 0.5 GB x86 Fargate is about $9.0/month plus $3.65 for
# the public IPv4 address, about $12.6/month while running = true. A public
# subnet avoids the NAT gateway's per-GB processing fee and the DERP-relay
# fallback Tailscale takes behind an AWS NAT gateway.

variable "environment" { type = string }
variable "region" { type = string }

variable "enabled" {
  description = "Create the replication tunnel. false (the default) plans zero resources and makes no AWS reads."
  type        = bool
  default     = false
}

variable "running" {
  description = "Run the router task (desired_count 1). Only meaningful when enabled. Leave false until the operator has pasted the auth key into the secret, so ECS does not crash-loop on an empty secret."
  type        = bool
  default     = false
}

variable "vpc_id" { type = string }

variable "public_subnet_ids" {
  description = "Public subnets the router task runs in. It takes a public IP so WireGuard can find direct paths without a NAT gateway."
  type        = list(string)
}

variable "cluster_id" {
  description = "ECS cluster the router service runs in (the environment's one cluster)."
  type        = string
}

variable "db_security_group_id" {
  description = "Security group attached to RDS. Gets an ingress rule admitting 5432 from the router group, and is the only source the router admits on the forwarder port. In aws/staging this is the data-plane group, which ElastiCache shares."
  type        = string
}

variable "db_subnet_ids" {
  description = "Subnets of the RDS DB subnet group. Their CIDRs are the routes the router advertises (not the instance's /32, which moves on host replacement)."
  type        = list(string)
}

variable "cloud_map_namespace_id" {
  description = "Id of the EXISTING private DNS namespace the forwarder registers in (qontinui-<env>, which already serves NATS). Not created here; aws/staging passes module.nats's namespace, imported in Phase 1c. Required when enabled."
  type        = string
  default     = null
}

variable "cloud_map_service_name" {
  description = "Cloud Map service name of the reverse forwarder; RDS reaches it at <name>.<namespace>."
  type        = string
  default     = "replication-tunnel"
}

variable "forward_listen_port" {
  description = "VPC-side port the reverse forwarder listens on inside the router task."
  type        = number
  default     = 15432
}

# A tailnet hostname. Marked sensitive because this repo is PUBLIC and plans of
# it are printed: the name identifies a node on the operator's tailnet.
variable "forward_target_host" {
  description = "Tailnet name of the local database the reverse forwarder dials through tailscaled's SOCKS5 proxy (tailscaled resolves it). Required when enabled."
  type        = string
  default     = null
  sensitive   = true
}

variable "forward_target_port" {
  description = "Port of the local database on the tailnet."
  type        = number
  default     = 5432
}

variable "tailscale_hostname" {
  description = "Hostname the router registers on the tailnet. Defaults to qontinui-<env>-replication-router."
  type        = string
  default     = null
}

# Pinned by tag AND digest: a tag on Docker Hub is mutable, a digest is not.
# tailscale v1.102.5 published 2026-09-24; alpine/socat 1.8.1.1 2026-06-20
# (SOCKS5-CONNECT needs socat >= 1.8). Both multi-arch manifest-list digests.
variable "tailscale_image" {
  description = "tailscale/tailscale image, pinned by tag and digest."
  type        = string
  default     = "tailscale/tailscale:v1.102.5@sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065"
}

variable "socat_image" {
  description = "alpine/socat image for the reverse forwarder, pinned by tag and digest."
  type        = string
  default     = "alpine/socat:1.8.1.1@sha256:7f9a06753033f2b7de18edc2353f2c15153413d95a039163c6db270fc7a6c3b0"
}

variable "log_retention_days" {
  description = "CloudWatch retention for the router's logs."
  type        = number
  default     = 14
}

locals {
  name          = "qontinui-${var.environment}-replication-tunnel"
  enabled_count = var.enabled ? 1 : 0
  hostname      = coalesce(var.tailscale_hostname, "qontinui-${var.environment}-replication-router")

  # tailscaled's SOCKS5 outbound proxy, on the task's loopback. awsvpc tasks
  # share one network namespace, so the forwarder container reaches it here and
  # nothing outside the task can.
  socks5_addr = "127.0.0.1:1055"

  # Name of the SSM parameter tailscaled keeps its node state in. The ARN form
  # drops the name's leading slash after "parameter".
  state_parameter_name = "/qontinui/${var.environment}/replication-tunnel/tailscaled-state"
}

# The required-when-enabled inputs are checked here, on the first object the
# module reads, so a missing one fails the plan with its own message before
# anything else is evaluated. (A cross-variable `validation` would need
# Terraform >= 1.9; the stack requires >= 1.7.)
data "aws_caller_identity" "current" {
  count = local.enabled_count

  lifecycle {
    precondition {
      condition     = var.cloud_map_namespace_id != null && var.cloud_map_namespace_id != ""
      error_message = "cloud_map_namespace_id is required when the replication tunnel is enabled (the existing qontinui-<env> namespace; in aws/staging it comes from module.nats, so nats_under_code must be true)."
    }
    precondition {
      condition     = var.forward_target_host != null && var.forward_target_host != ""
      error_message = "forward_target_host (the local database's tailnet name) is required when the replication tunnel is enabled."
    }
  }
}

data "aws_partition" "current" {
  count = local.enabled_count
}

data "aws_subnet" "db" {
  for_each = var.enabled ? toset(var.db_subnet_ids) : toset([])
  id       = each.value
}

locals {
  advertise_routes = sort(distinct([for s in data.aws_subnet.db : s.cidr_block]))

  state_parameter_arn = var.enabled ? "arn:${data.aws_partition.current[0].partition}:ssm:${var.region}:${data.aws_caller_identity.current[0].account_id}:parameter${local.state_parameter_name}" : null
}

# ─── Auth key: an EMPTY secret the operator fills (OD3) ─────────────────
# No aws_secretsmanager_secret_version on purpose: the key is minted in the
# operator's tailnet console and pasted in the AWS console, so it never reaches
# Terraform state, an agent, or this public repo.
resource "aws_secretsmanager_secret" "ts_authkey" {
  count       = local.enabled_count
  name        = "qontinui/${var.environment}/replication-tunnel/ts-authkey"
  description = "Tailscale auth key for the replication router. Pasted by the operator; tag-scoped and pre-approved. Terraform never writes a value."
}

# ─── Logs ───────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "router" {
  count             = local.enabled_count
  name              = "/ecs/qontinui-${var.environment}/replication-tunnel"
  retention_in_days = var.log_retention_days
}

# ─── Security groups ────────────────────────────────────────────────────

resource "aws_security_group" "router" {
  count       = local.enabled_count
  name        = local.name
  description = "Replication router; no inbound except the reverse forwarder from the RDS group"
  vpc_id      = var.vpc_id

  # Outbound to the tailnet (control plane, DERP, direct WireGuard peers), to
  # RDS on the forward path, and to Docker Hub for the image pulls.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = local.name }
}

resource "aws_security_group_rule" "router_forwarder_from_db" {
  count                    = local.enabled_count
  type                     = "ingress"
  from_port                = var.forward_listen_port
  to_port                  = var.forward_listen_port
  protocol                 = "tcp"
  source_security_group_id = var.db_security_group_id
  security_group_id        = aws_security_group.router[0].id
  description              = "RDS to the reverse forwarder (reverse subscription)"
}

resource "aws_security_group_rule" "db_pg_from_router" {
  count                    = local.enabled_count
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.router[0].id
  security_group_id        = var.db_security_group_id
  description              = "Replication router to RDS Postgres (forward path)"
}

# ─── IAM ────────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "ecs_assume" {
  count = local.enabled_count
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: what ECS itself does to start the task. Least privilege
# instead of AmazonECSTaskExecutionRolePolicy: both images come from Docker Hub
# (no ECR grant needed), so it writes this one log group and reads this one
# secret, nothing else.
resource "aws_iam_role" "exec" {
  count              = local.enabled_count
  name               = "${local.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume[0].json
}

data "aws_iam_policy_document" "exec" {
  count = local.enabled_count
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.router[0].arn}:*"]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.ts_authkey[0].arn]
  }
}

resource "aws_iam_role_policy" "exec" {
  count  = local.enabled_count
  name   = "${local.name}-exec"
  role   = aws_iam_role.exec[0].id
  policy = data.aws_iam_policy_document.exec[0].json
}

# Task role: what tailscaled does at runtime — read and write its own state
# parameter. The parameter is a SecureString under the AWS-managed aws/ssm key,
# whose key policy admits any principal in the account calling through SSM, so
# no KMS grant is needed here.
resource "aws_iam_role" "task" {
  count              = local.enabled_count
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume[0].json
}

data "aws_iam_policy_document" "task" {
  count = local.enabled_count
  statement {
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = [local.state_parameter_arn]
  }
}

resource "aws_iam_role_policy" "task" {
  count  = local.enabled_count
  name   = "${local.name}-task"
  role   = aws_iam_role.task[0].id
  policy = data.aws_iam_policy_document.task[0].json
}

# ─── Cloud Map: the reverse forwarder's stable name ─────────────────────

resource "aws_service_discovery_service" "forwarder" {
  count = local.enabled_count
  name  = var.cloud_map_service_name

  dns_config {
    # coalesce: a null here would fail on the provider schema before the
    # precondition on data.aws_caller_identity.current could name the input.
    namespace_id   = coalesce(var.cloud_map_namespace_id, "unset")
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 10
    }
  }

  # ECS reports task health; an unhealthy or stopped task is deregistered.
  health_check_custom_config {
    failure_threshold = 1
  }
}

# ─── Task definition + service ──────────────────────────────────────────

resource "aws_ecs_task_definition" "router" {
  count                    = local.enabled_count
  family                   = local.name
  cpu                      = 256
  memory                   = 512
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.exec[0].arn
  task_role_arn            = aws_iam_role.task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "tailscale"
      image     = var.tailscale_image
      essential = true

      entryPoint = ["/bin/sh", "-c"]
      command    = [file("${path.module}/router-entrypoint.sh")]

      environment = [
        { name = "TS_STATE_ARN", value = local.state_parameter_arn },
        { name = "TS_HOSTNAME", value = local.hostname },
        { name = "TS_ROUTES", value = join(",", local.advertise_routes) },
        { name = "TS_SOCKS5_ADDR", value = local.socks5_addr },
        { name = "TS_UP_TIMEOUT", value = "120s" },
      ]

      secrets = [
        { name = "TS_AUTHKEY", valueFrom = aws_secretsmanager_secret.ts_authkey[0].arn },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.router[0].name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "tailscale"
        }
      }
    },
    {
      # Reverse forwarder: accepts on the VPC-side port and dials the local
      # database's tailnet name through tailscaled's SOCKS5 proxy. socat sends
      # the name unresolved (SOCKS5 ATYP domain), so tailscaled resolves it
      # against the tailnet. One child per connection (fork).
      name      = "forwarder"
      image     = var.socat_image
      essential = true

      command = [
        "TCP-LISTEN:${var.forward_listen_port},fork,reuseaddr",
        "SOCKS5-CONNECT:${local.socks5_addr}:${coalesce(var.forward_target_host, "unset")}:${var.forward_target_port}",
      ]

      portMappings = [{
        containerPort = var.forward_listen_port
        hostPort      = var.forward_listen_port
        protocol      = "tcp"
      }]

      dependsOn = [{ containerName = "tailscale", condition = "START" }]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.router[0].name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "forwarder"
        }
      }
    },
  ])

  lifecycle {
    precondition {
      condition     = length(local.advertise_routes) > 0
      error_message = "db_subnet_ids resolved to no CIDRs; the router would advertise nothing."
    }
  }
}

resource "aws_ecs_service" "router" {
  count           = local.enabled_count
  name            = "replication-tunnel"
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.router[0].arn
  desired_count   = var.running ? 1 : 0
  launch_type     = "FARGATE"

  # One node identity: stop the old task before starting the new one, never two
  # tasks sharing the same SSM state.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.router[0].id]
    assign_public_ip = true
  }

  service_registries {
    registry_arn = aws_service_discovery_service.forwarder[0].arn
  }

  enable_execute_command = false
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "router_security_group_id" {
  value = var.enabled ? aws_security_group.router[0].id : null
}

output "ts_authkey_secret_arn" {
  description = "The empty secret the operator pastes the Tailscale auth key into."
  value       = var.enabled ? aws_secretsmanager_secret.ts_authkey[0].arn : null
}

output "state_parameter_name" {
  description = "SSM parameter tailscaled creates and keeps its node state in."
  value       = var.enabled ? local.state_parameter_name : null
}

output "forwarder_service_name" {
  description = "Cloud Map service name of the reverse forwarder (<name>.<namespace>:<port> from RDS)."
  value       = var.enabled ? var.cloud_map_service_name : null
}

output "forward_listen_port" {
  value = var.forward_listen_port
}

output "advertise_routes" {
  value = local.advertise_routes
}
