# ECS Fargate service for qontinui-coord. Pulls connection strings from
# Secrets Manager at task-launch time; logs to CloudWatch.
#
# Public traffic comes via the ALB target group (passed in from the tunnel
# module). The task itself runs in private subnets and only accepts ALB
# traffic.

variable "environment"           { type = string }
variable "region"                { type = string }
variable "vpc_id"                { type = string }
variable "public_subnet_ids"     { type = list(string) }
variable "private_subnet_ids"    { type = list(string) }
variable "client_sg_id"          { type = string }
variable "alb_sg_id"             { type = string }

variable "image_uri"             { type = string }
variable "cpu"                   { type = number }
variable "memory_mb"             { type = number }
variable "desired_count"         { type = number }

variable "database_url" {
  type      = string
  sensitive = true
}
variable "redis_url" {
  type      = string
  sensitive = true
}
variable "github_webhook_secret" {
  type      = string
  sensitive = true
}

variable "s3_bucket_arn"         { type = string }

# ─── Cluster ────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = "qontinui-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "disabled" # staging — flip to "enhanced" in prod
  }
}

# ─── Logging ────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "coord" {
  name              = "/ecs/qontinui-${var.environment}/coord"
  retention_in_days = 14
}

# ─── Secrets ────────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "database_url" {
  name        = "qontinui/${var.environment}/coord/database_url"
  description = "DATABASE_URL for qontinui-coord (${var.environment})"
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = var.database_url
}

resource "aws_secretsmanager_secret" "redis_url" {
  name        = "qontinui/${var.environment}/coord/redis_url"
  description = "REDIS_URL for qontinui-coord (${var.environment})"
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id     = aws_secretsmanager_secret.redis_url.id
  secret_string = var.redis_url
}

resource "aws_secretsmanager_secret" "webhook_secret" {
  name        = "qontinui/${var.environment}/coord/github_webhook_secret"
  description = "GitHub webhook HMAC secret for qontinui-coord (${var.environment})"
}

resource "aws_secretsmanager_secret_version" "webhook_secret" {
  secret_id     = aws_secretsmanager_secret.webhook_secret.id
  secret_string = var.github_webhook_secret
}

# ─── IAM ────────────────────────────────────────────────────────────────

# Execution role: ECS uses this to pull the image and write logs and
# resolve secrets at task-launch time.
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
  name               = "qontinui-${var.environment}-coord-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "task_exec_managed" {
  role       = aws_iam_role.task_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task exec role needs to read the secrets we just created.
data "aws_iam_policy_document" "task_exec_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.redis_url.arn,
      aws_secretsmanager_secret.webhook_secret.arn,
    ]
  }
}

resource "aws_iam_role_policy" "task_exec_secrets" {
  role   = aws_iam_role.task_exec.id
  policy = data.aws_iam_policy_document.task_exec_secrets.json
}

# Task role: what the running container itself can do. Currently just
# read/write to its blob bucket. Add narrower permissions as features land.
resource "aws_iam_role" "task" {
  name               = "qontinui-${var.environment}-coord-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task_blob" {
  statement {
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      var.s3_bucket_arn,
      "${var.s3_bucket_arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "task_blob" {
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_blob.json
}

# ─── Task definition ────────────────────────────────────────────────────

resource "aws_ecs_task_definition" "coord" {
  family                   = "qontinui-${var.environment}-coord"
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
      name      = "coord"
      image     = var.image_uri
      essential = true

      portMappings = [{
        containerPort = 9870
        hostPort      = 9870
        protocol      = "tcp"
      }]

      environment = [
        { name = "COORD_BIND_ADDR", value = "0.0.0.0:9870" },
        { name = "RUST_LOG",        value = "qontinui_coord=info,axum=info,tower_http=info" },
      ]

      secrets = [
        { name = "DATABASE_URL",          valueFrom = aws_secretsmanager_secret.database_url.arn },
        { name = "REDIS_URL",             valueFrom = aws_secretsmanager_secret.redis_url.arn },
        { name = "GITHUB_WEBHOOK_SECRET", valueFrom = aws_secretsmanager_secret.webhook_secret.arn },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.coord.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "coord"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "wget -qO- http://localhost:9870/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
    }
  ])
}

# ─── Target group + service ─────────────────────────────────────────────

resource "aws_lb_target_group" "coord" {
  name        = "qontinui-${var.environment}-coord"
  port        = 9870
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/health"
    port                = "9870"
    protocol            = "HTTP"
    interval            = 30
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }

  deregistration_delay = 30
}

resource "aws_ecs_service" "coord" {
  name            = "coord"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.coord.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.client_sg_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.coord.arn
    container_name   = "coord"
    container_port   = 9870
  }

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200

  enable_execute_command = true # SSM Session Manager into a running task

  lifecycle {
    ignore_changes = [desired_count] # let stop/start scripts manage replicas
  }

  depends_on = [aws_lb_target_group.coord]
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "cluster_name"     { value = aws_ecs_cluster.main.name }
output "service_name"     { value = aws_ecs_service.coord.name }
output "target_group_arn" { value = aws_lb_target_group.coord.arn }
