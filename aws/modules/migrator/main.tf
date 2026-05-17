# Canonical-DB migrator — one-off `alembic upgrade head` against the
# shared qontinui_db. Creates BOTH `public.*` (web) and `coord.*` (coord)
# schemas from the unified qontinui-web alembic chain. Idempotent: a
# re-run is a no-op when the DB is already at head.
#
# No ECS *service* — this is a task definition run on demand via
# `aws ecs run-task` (post-apply, and any time the chain advances).

variable "environment" { type = string }
variable "region" { type = string }
variable "cpu" {
  type    = number
  default = 512
}
variable "memory_mb" {
  type    = number
  default = 1024
}

variable "database_url" { # postgresql://...:.../qontinui_db?sslmode=disable
  type      = string
  sensitive = true
}

resource "aws_cloudwatch_log_group" "migrator" {
  name              = "/ecs/qontinui-${var.environment}/migrator"
  retention_in_days = 14
}

resource "aws_secretsmanager_secret" "database_url" {
  name        = "qontinui/${var.environment}/migrator/database_url"
  description = "Canonical DSN (qontinui_db, postgresql:// scheme) for alembic upgrade head"
}
resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = var.database_url
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_exec" {
  name               = "qontinui-${var.environment}-migrator-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "task_exec_managed" {
  role       = aws_iam_role.task_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "exec_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.database_url.arn]
  }
}

resource "aws_iam_role_policy" "exec_secret" {
  role   = aws_iam_role.task_exec.id
  policy = data.aws_iam_policy_document.exec_secret.json
}

resource "aws_iam_role" "task" {
  name               = "qontinui-${var.environment}-migrator-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_ecs_task_definition" "migrator" {
  family                   = "qontinui-${var.environment}-migrator"
  cpu                      = var.cpu
  memory                   = var.memory_mb
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.task_exec.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "migrator"
      image     = var.image_uri
      essential = true

      # Image ENTRYPOINT = tini -> entrypoint.sh -> alembic upgrade head.

      secrets = [
        { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.migrator.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "migrator"
        }
      }
    }
  ])
}

variable "image_uri" { type = string }

output "task_family" { value = aws_ecs_task_definition.migrator.family }
output "log_group" { value = aws_cloudwatch_log_group.migrator.name }
