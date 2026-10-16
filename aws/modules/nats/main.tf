# NATS JetStream server for coord — ECS Fargate service, its EFS store, and
# the Cloud Map private DNS namespace coord resolves it through.
#
# BROUGHT UNDER CODE, NOT CREATED. Every resource here existed before this
# module did: the stack was hand-built on 2026-05-20 and no Terraform managed
# it. The definitions below were written to match the live configuration read
# with describe calls on 2026-10-16, and staging/nats-imports.tf adopts each
# one with an `import {}` block. The acceptance test for this file is that
# `terraform plan` shows ONLY imports — 0 to add, 0 to change, 0 to destroy.
# Any edit that makes the plan show a change is a real change to production
# NATS, and must be made deliberately, not as a side effect of the import.
# The root instantiates this module only when var.nats_under_code is true
# (default false), so merging it is a no-op until the operator's gated import.
# Plan: 2026-10-15-coord-and-web-data-move-off-aws-by-logical-replication,
# Phase 1c / decision D13.
#
# THE DATA. The EFS holds coord's JetStream store (11 streams, ~41.5 GB at the
# time of the import). It is the ONLY copy of the `coord-park` stream (parked
# poison messages) and has no EFS backup policy. A plan that shows the file
# system, its access point or the task definition being REPLACED would lose
# that data or orphan the running task — never apply one. The file system
# carries `prevent_destroy` for that reason.
#
# WHY THE SUPPORTING RESOURCES ARE IMPORTED, NOT READ AS DATA SOURCES. The two
# security groups, the task role (+ its inline policy) and the log group were
# hand-built with the service and nothing else owns them. Read through data
# sources they would stay unmanaged: a teardown would miss them, and a later
# change to them (the leafnode port D13 needs is a security-group rule) would
# again happen outside code. The plan resolved the same question for the
# namespace the same way. What IS shared with coord is referenced, not
# re-declared: the cluster, coord's exec role and coord's client SG (the
# ingress source) come in as variables from modules that already own them.
#
# THE TASK DEFINITION REVISION. Importing revision 1 does not replace it, as
# long as container_definitions and the other ForceNew arguments match what
# ECS reports — which is why the JSON below is field-for-field. A mismatch
# does not plan an in-place edit: container_definitions is ForceNew in the
# provider, so it plans a NEW revision, the deregistration of :1 (the
# provider's destroy for a task definition) and an in-place repoint of the
# service. docs/nats-import-plan.txt records one such mutation run. That is
# the intended path for a deliberate change later; for the import it is the
# failure signal.
#
# TAGS. The live resources carry Project + Environment (+ Name) only. Project
# and Environment come from the provider's default_tags with the same values,
# so only Name is declared here — redeclaring the default keys makes the
# provider plan them as an added tag. default_tags also add ManagedBy + Stack,
# which the live resources lack; every resource ignores `tags_all` so the
# import does not turn into a tag-only update of production resources.
# Dropping that ignore later is a deliberate, separately-reviewed tag change.
#
# ENTRYPOINT. coord reaches the server at nats.<namespace>:4222 (the A record
# Cloud Map publishes for the task's ENI); the client credential is the
# Secrets Manager secret qontinui-coord-<env>/nats-password, referenced by ARN
# below and never read here.

variable "environment" { type = string }
variable "vpc_id" { type = string }

variable "subnet_ids" {
  description = "Private subnets the NATS task runs in and the EFS mount targets live in, one per AZ. Mount targets are keyed by subnet id, so the order does not matter."
  type        = list(string)
}

variable "client_sg_id" {
  description = "coord's task security group (network module `client`). The only source allowed to reach NATS on 4222 (clients) and 8222 (monitor)."
  type        = string
}

variable "cluster_id" {
  description = "ECS cluster the NATS service runs in (coord's cluster, one per environment)."
  type        = string
}

variable "task_exec_role_arn" {
  description = "Task execution role. The live task definition uses coord's exec role, whose prefix-scoped secrets grant (qontinui-coord-<env>/*) already covers the NATS password."
  type        = string
}

variable "region" { type = string }

data "aws_secretsmanager_secret" "nats_password" {
  # Metadata only (DescribeSecret) — resolves the full ARN, random suffix
  # included, so it is not hard-coded. The secret's value is never read.
  name = "qontinui-coord-${var.environment}/nats-password"
}

locals {
  name = "qontinui-${var.environment}-nats"

}

# ─── Cloud Map: private DNS namespace + the nats service registration ───

resource "aws_service_discovery_private_dns_namespace" "main" {
  name        = "qontinui-${var.environment}"
  description = "Private DNS namespace for qontinui ${var.environment} services"
  vpc         = var.vpc_id

  lifecycle {
    ignore_changes = [tags_all]
  }
}

resource "aws_service_discovery_service" "nats" {
  name        = "nats"
  description = "NATS server service registration for qontinui-${var.environment}"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.main.id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "A"
      ttl  = 10
    }
  }

  health_check_custom_config {
    failure_threshold = 1
  }

  lifecycle {
    # force_destroy is a Terraform-only flag the import leaves null.
    # This also swallows any future edit to that flag in this file: a
    # deliberate change to it must remove it from ignore_changes first.
    ignore_changes = [tags_all, force_destroy]
  }
}

# ─── Security groups ────────────────────────────────────────────────────
# Same style as modules/network: egress inline, ingress as separate rules.

resource "aws_security_group" "nats" {
  name        = "${local.name}-sg"
  description = "NATS server task SG (4222 NATS client, 8222 monitor)"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-sg" }

  lifecycle {
    # revoke_rules_on_delete is a Terraform-only flag the import leaves null.
    # This also swallows any future edit to that flag in this file: a
    # deliberate change to it must remove it from ignore_changes first.
    ignore_changes = [tags_all, revoke_rules_on_delete]
  }
}

resource "aws_security_group_rule" "nats_client_from_coord" {
  type                     = "ingress"
  from_port                = 4222
  to_port                  = 4222
  protocol                 = "tcp"
  source_security_group_id = var.client_sg_id
  security_group_id        = aws_security_group.nats.id
  description              = "NATS client from coord SG"
}

resource "aws_security_group_rule" "nats_monitor_from_coord" {
  type                     = "ingress"
  from_port                = 8222
  to_port                  = 8222
  protocol                 = "tcp"
  source_security_group_id = var.client_sg_id
  security_group_id        = aws_security_group.nats.id
  description              = "NATS monitor port from coord SG"
}

resource "aws_security_group" "efs" {
  name        = "${local.name}-efs-sg"
  description = "EFS mount target SG for NATS JetStream (NFS from NATS task SG)"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-efs-sg" }

  lifecycle {
    # revoke_rules_on_delete is a Terraform-only flag the import leaves null.
    # This also swallows any future edit to that flag in this file: a
    # deliberate change to it must remove it from ignore_changes first.
    ignore_changes = [tags_all, revoke_rules_on_delete]
  }
}

resource "aws_security_group_rule" "efs_nfs_from_nats" {
  type                     = "ingress"
  from_port                = 2049
  to_port                  = 2049
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.nats.id
  security_group_id        = aws_security_group.efs.id
  description              = "NFS from NATS task"
}

# ─── EFS: the JetStream store ───────────────────────────────────────────

resource "aws_efs_file_system" "jetstream" {
  # creation_token is ForceNew; the live value carries the epoch suffix the
  # hand-built stack appended.
  creation_token   = "${local.name}-jetstream-1779280238"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = { Name = "${local.name}-jetstream" }

  lifecycle {
    # The only copy of coord-park lives here (see THE DATA above).
    prevent_destroy = true
    ignore_changes  = [tags_all]
  }
}

# Keyed by subnet id, not count-indexed: with count, a reorder of
# private_subnet_ids would re-pair index and subnet and plan both mount
# targets for replacement (unmounting the store under the running task).
# Keyed by subnet id, a reorder is a no-op. Limitation: the keys must be known
# at plan time, which holds here because the subnets already exist; a brand
# new environment would have to create the network first.
resource "aws_efs_mount_target" "jetstream" {
  for_each = toset(var.subnet_ids)

  file_system_id  = aws_efs_file_system.jetstream.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "jetstream" {
  file_system_id = aws_efs_file_system.jetstream.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/jetstream"

    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  tags = { Name = "${local.name}-jetstream-ap" }

  lifecycle {
    ignore_changes = [tags_all]
  }
}

# ─── IAM: task role (EFS access through the access point only) ──────────

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  description        = "Task role for ${local.name} (EFS access only)"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json

  lifecycle {
    ignore_changes = [tags_all]
  }
}

# jsonencode rather than an aws_iam_policy_document data source: the data
# source depends on resources that are being imported, which defers its read
# to apply time and makes the plan show this policy as "known after apply".
resource "aws_iam_role_policy" "efs_access" {
  name = "efs-jetstream-access"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EfsMountForJetStream"
        Effect = "Allow"
        Action = [
          "elasticfilesystem:ClientMount",
          "elasticfilesystem:ClientWrite",
          "elasticfilesystem:ClientRootAccess",
        ]
        Resource = aws_efs_file_system.jetstream.arn
        Condition = {
          StringEquals = {
            "elasticfilesystem:AccessPointArn" = aws_efs_access_point.jetstream.arn
          }
        }
      },
    ]
  })
}

# ─── Logs ───────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "nats" {
  name              = "/ecs/qontinui-${var.environment}/nats"
  retention_in_days = 14

  lifecycle {
    ignore_changes = [tags_all]
  }
}

# ─── Task definition + service ──────────────────────────────────────────

resource "aws_ecs_task_definition" "nats" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = var.task_exec_role_arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "jetstream"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.jetstream.id
      root_directory     = "/"
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.jetstream.id
        iam             = "ENABLED"
      }
    }
  }

  # Field-for-field the registered revision 1, including the empty lists and
  # `cpu = 0` ECS reports back — container_definitions is ForceNew, so any
  # difference here would plan a NEW REVISION and point the service at it.
  # The command expands $NATS_PASSWORD from the container's environment at
  # start; the value comes from Secrets Manager and never appears here.
  container_definitions = jsonencode([
    {
      name       = "nats"
      image      = "nats:2-alpine"
      cpu        = 0
      essential  = true
      entryPoint = ["/bin/sh", "-c"]
      command    = ["exec nats-server -m 8222 -js -sd /data --user qontinui_client --pass \"$NATS_PASSWORD\""]
      portMappings = [
        { containerPort = 4222, hostPort = 4222, protocol = "tcp" },
        { containerPort = 8222, hostPort = 8222, protocol = "tcp" },
      ]
      environment = []
      mountPoints = [
        { sourceVolume = "jetstream", containerPath = "/data", readOnly = false },
      ]
      volumesFrom = []
      secrets = [
        { name = "NATS_PASSWORD", valueFrom = data.aws_secretsmanager_secret.nats_password.arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.nats.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "nats"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "wget -q -O- http://localhost:8222/healthz || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 15
      }
      systemControls = []
    },
  ])

  lifecycle {
    # skip_destroy is a Terraform-only flag the import leaves null; ignoring it
    # keeps the import from planning a state-only update.
    # This also swallows any future edit to that flag in this file: a
    # deliberate change to it must remove it from ignore_changes first.
    ignore_changes = [tags_all, skip_destroy]
  }
}

resource "aws_ecs_service" "nats" {
  name    = "nats"
  cluster = var.cluster_id
  # family:revision — the form ECS stores and the import reads back. The full
  # ARN names the same revision but plans as an in-place update.
  task_definition  = "${aws_ecs_task_definition.nats.family}:${aws_ecs_task_definition.nats.revision}"
  launch_type      = "FARGATE"
  platform_version = "LATEST"
  desired_count    = 1

  # The live setting, kept as-is so the import changes nothing. It is NOT
  # single-writer safe: max 200 lets a deployment start the new task while
  # the old one still runs, so two nats-server processes can briefly share
  # the one JetStream directory on EFS. That is an existing risk, not one this
  # import adds. The single-writer setting is max 100 / min 0 (stop the old
  # task first); change it together with D13's leafnode task-definition change,
  # which is the next deployment of this service anyway.
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 0
  availability_zone_rebalancing      = "ENABLED"
  propagate_tags                     = "NONE"
  enable_ecs_managed_tags            = false
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = false
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.nats.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn   = aws_service_discovery_service.nats.arn
    container_name = "nats"
  }

  lifecycle {
    # wait_for_steady_state is a Terraform-only flag the import leaves null.
    # This also swallows any future edit to that flag in this file: a
    # deliberate change to it must remove it from ignore_changes first.
    ignore_changes = [tags_all, wait_for_steady_state]
  }
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "service_name" { value = aws_ecs_service.nats.name }
output "task_definition_arn" { value = aws_ecs_task_definition.nats.arn }
output "security_group_id" { value = aws_security_group.nats.id }
output "efs_file_system_id" { value = aws_efs_file_system.jetstream.id }
output "efs_access_point_id" { value = aws_efs_access_point.jetstream.id }
output "namespace_id" { value = aws_service_discovery_private_dns_namespace.main.id }
output "namespace_arn" { value = aws_service_discovery_private_dns_namespace.main.arn }
output "namespace_name" { value = aws_service_discovery_private_dns_namespace.main.name }
output "discovery_service_arn" { value = aws_service_discovery_service.nats.arn }
