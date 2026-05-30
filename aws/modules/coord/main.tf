# ECS Fargate service for qontinui-coord. Pulls connection strings from
# Secrets Manager at task-launch time; logs to CloudWatch.
#
# Public traffic comes via the ALB target group (passed in from the tunnel
# module). The task itself runs in private subnets and only accepts ALB
# traffic.

variable "environment" { type = string }
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "private_subnet_ids" { type = list(string) }
variable "client_sg_id" { type = string }
variable "alb_sg_id" { type = string }

variable "image_uri" { type = string }
variable "cpu" { type = number }
variable "memory_mb" { type = number }
variable "desired_count" { type = number }

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
variable "coord_admin_secret" {
  type      = string
  sensitive = true
  # Shared with the web backend's StrategyClient. coord reads env
  # COORD_ADMIN_SECRET to gate POST /coord/auth/service-token.
}

variable "s3_bucket_arn" { type = string }

# Phase 8 cold-tier PTY-output bucket. coord is the sole writer/reader; the
# policy below is scoped to ONLY this bucket and its objects.
variable "session_output_cold_bucket_arn" {
  type        = string
  description = "ARN of the cold-tier session-output S3 bucket (Phase 8). coord writes one object per shared session and reads them back for the dashboard xterm pane."
}

variable "session_output_cold_key_prefix" {
  type        = string
  default     = "tenant/"
  description = "Fixed key prefix all cold-tier objects live under (tenant/<tenant_id>/session/<session_id>.log). Scopes s3:ListBucket so coord can only enumerate session-output keys."
}

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

resource "aws_secretsmanager_secret" "coord_admin_secret" {
  name        = "qontinui/${var.environment}/coord/admin_secret"
  description = "Shared admin secret for the web→coord strategy bridge (${var.environment})"
}

resource "aws_secretsmanager_secret_version" "coord_admin_secret" {
  secret_id     = aws_secretsmanager_secret.coord_admin_secret.id
  secret_string = var.coord_admin_secret
}

# JWT signing key (Ed25519 PKCS#8 PEM). Operator-staged out-of-band; this
# data source resolves it by name at plan time.
#
# Why a data source, not an aws_secretsmanager_secret resource:
#   - Mirrors the qontinui/staging/cc_token pattern — keypair material is
#     operator-decided, terraform never sees or stores the value.
#     Per feedback_deployment_config_gap_class: secret-management posture
#     stays operator-decided; terraform handles wiring/IAM, not generation.
#   - Eliminates the bootstrap chicken-and-egg of terraform-managed secret
#     resources that need a `secret_string` at create time but a
#     `lifecycle.ignore_changes = [secret_string]` to avoid fighting
#     operator overwrites.
#
# Operator-staging recipe (run ONCE before first `terraform apply` that
# references this data source):
#
#   # 1. Generate keypair (Ed25519 PKCS#8 PEM):
#   openssl genpkey -algorithm Ed25519 -out coord-jwt.pkcs8 -outform PEM
#
#   # 2. Create secret with PEM as value (use file:// to avoid shell-escaping
#   #    the multi-line PEM):
#   aws secretsmanager create-secret \
#     --region us-east-1 \
#     --name qontinui/staging/coord_jwt_signing_key \
#     --description "Ed25519 PKCS#8 PEM signing key for coord JWT issuance (staging)" \
#     --secret-string file://coord-jwt.pkcs8
#
#   # 3. Securely delete the local PEM (key never leaves Secrets Manager
#   #    after this point):
#   shred -u coord-jwt.pkcs8     # POSIX
#   # OR (PowerShell): Remove-Item coord-jwt.pkcs8 -Force
#
# Rotation: aws secretsmanager update-secret --secret-id ... --secret-string
# file://new.pkcs8 + ECS force-new-deployment. Until automated rotation with
# overlapping kid lands (jwt.rs §6 risk bullet), outstanding tokens minted
# under the prior key 401 immediately. Coordinate with consumers.
data "aws_secretsmanager_secret" "coord_jwt_signing_key" {
  name = "qontinui/${var.environment}/coord_jwt_signing_key"
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

# Account id for constructing prefix-scoped secret ARNs below.
data "aws_caller_identity" "current" {}

# Task exec role needs to read every secret coord consumes at task-launch.
#
# PREFIX-SCOPED (not an explicit ARN list) — deliberately. coord's full
# runtime task definition is authored in `qontinui-coord/deploy/taskdef.json`
# and shipped by `deploy-coord.yml` (the reproducible per-commit-SHA model);
# terraform owns only the infra shell. coord adds secrets to taskdef.json over
# time (GitHub OAuth/App, GitHub token, Vercel deploy hooks, NATS, …). An
# explicit per-ARN grant here rots the instant a new secret is added — every
# such addition was previously made out-of-band to the live role and never
# reflected in terraform, so a full `terraform apply` would STRIP the role
# back to the 5 secrets terraform knew about and break coord. (See plan
# 2026-05-30-qontinui-stack-terraform-state-reconciliation §1.2/§3-B1.)
#
# Granting on coord's two secret namespaces auto-covers any future coord
# secret WITHOUT a terraform change, so the role can never strip-drift again:
#   - qontinui/${env}/coord*    database_url, redis_url, github_webhook_secret,
#                               admin_secret, github_app_private_key (all under
#                               .../coord/…) PLUS coord_jwt_signing_key (no
#                               slash) — the trailing `coord*` glob matches both
#   - qontinui-coord-${env}/*   github-oauth, github-token, vercel-deploy-hooks,
#                               nats-url, nats-password
# Secrets Manager ARNs always end in `-<6char>`, so the trailing `*` is
# mandatory regardless. These two globs are least-privilege: they do NOT match
# web's secrets (qontinui/${env}/web/*) or migrator's
# (qontinui/${env}/migrator/*) — a broad `qontinui/${env}/*` WOULD, so it's
# intentionally avoided.
data "aws_iam_policy_document" "task_exec_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:qontinui/${var.environment}/coord*",
      "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:qontinui-coord-${var.environment}/*",
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

# Phase 8 cold-tier session-output access. Deliberately a SEPARATE, narrower
# policy from task_blob (not folded in) so the cold-tier grant stays auditable
# and minimal:
#   - Object actions: GetObject + PutObject only. No DeleteObject — objects are
#     immutable per session and expiry is owned by the bucket's 90-day
#     lifecycle rule, not by coord. (Per-tenant cold quota is enforced
#     coord-side by refusing new PutObjects once the tenant's summed object
#     size exceeds its limit — see docs/session-output-cold-tier.md. S3 has no
#     native per-prefix quota, so this is an application-level check.)
#   - ListBucket: scoped with an s3:prefix condition to the `tenant/` key
#     namespace so coord can enumerate session-output keys (for quota
#     accounting) but cannot list anything else that might land in the bucket.
data "aws_iam_policy_document" "task_session_output_cold" {
  statement {
    sid = "SessionOutputColdObjects"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = [
      "${var.session_output_cold_bucket_arn}/${var.session_output_cold_key_prefix}*",
    ]
  }

  statement {
    sid     = "SessionOutputColdListScoped"
    actions = ["s3:ListBucket"]
    resources = [
      var.session_output_cold_bucket_arn,
    ]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.session_output_cold_key_prefix}*"]
    }
  }
}

resource "aws_iam_role_policy" "task_session_output_cold" {
  name   = "qontinui-${var.environment}-coord-session-output-cold"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_session_output_cold.json
}

# ─── Task definition (BOOTSTRAP-ONLY) ─────────────────────────────────────
#
# This definition exists ONLY to satisfy the `task_definition` argument the
# `aws_ecs_service.coord` resource requires at first-ever create time (a fresh
# environment can't stand the service up with no task def to point at). It is
# NOT the running definition: the service `ignore_changes`s its task_definition
# pointer, and CI (deploy-coord.yml) registers the real revision from
# qontinui-coord/deploy/taskdef.json immediately after first apply and on every
# merge thereafter. Its staleness is therefore harmless by design — the floating
# `:staging` image, 3-env, 5-secret shape below is a placeholder, never live
# traffic. Do NOT try to keep it in sync with the live revision; that's CI's job.
# (See plan 2026-05-30-qontinui-stack-terraform-state-reconciliation §3-A1 +
# Open-Q1.)
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
        { name = "RUST_LOG", value = "qontinui_coord=info,axum=info,tower_http=info" },
        # Strategy substrate (markdown docs) baked into the coord image as
        # the full qontinui-dev-notes repo at /srv/dev-notes (qontinui-coord
        # PRs #40 `a9c1b07` + #41 `a4e3155` + #42 — the last preserves .git/
        # so coord's `git -C <substrate>` provenance reads succeed). Path
        # here points INTO the repo so safe_doc_name's bounded reads target
        # project-strategy/*.md, while git's parent-walk from there finds
        # .git/ at /srv/dev-notes/.git. Overrides the relative-path default
        # in `substrate_dir()` which only resolves on the local dogfood
        # checkout. Without this env set, /strategy/docs on Fargate returns
        # `500 substrate unreadable: No such file or directory`. Closes
        # proj_aws_staging_coord_deploy_2026-05-17 Open Issues
        # (substrate-path-on-Fargate). Interim posture — retires when
        # strategy docs migrate to coord-mediated storage.
        { name = "STRATEGY_SUBSTRATE_PATH", value = "/srv/dev-notes/project-strategy" },
      ]

      secrets = [
        { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
        { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
        { name = "GITHUB_WEBHOOK_SECRET", valueFrom = aws_secretsmanager_secret.webhook_secret.arn },
        { name = "COORD_ADMIN_SECRET", valueFrom = aws_secretsmanager_secret.coord_admin_secret.arn },
        # Companion to qontinui-coord PR `feat(jwt): load signing key from
        # COORD_JWT_SIGNING_KEY env`. Eliminates the ephemeral-Fargate-FS
        # JWT key regeneration that invalidated service tokens on every
        # coord task replacement (proj_aws_staging_coord_deploy_2026-05-17
        # Open Issues #2). Requires a coord image built from a commit that
        # includes the env-var loading path; older images that only look at
        # /root/.qontinui/coord-jwt-ed25519.pkcs8 ignore this env value and
        # mint-and-persist as before — no regression, just no improvement.
        { name = "COORD_JWT_SIGNING_KEY", valueFrom = data.aws_secretsmanager_secret.coord_jwt_signing_key.arn },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.coord.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "coord"
        }
      }

      # Container-level healthCheck intentionally omitted. coord's image is a
      # minimal Rust runtime that does NOT ship wget/curl/sh-fetch utilities,
      # so the original `wget -qO- http://localhost:9870/health` CMD-SHELL
      # probe always failed → ECS replaced the task every ~3 min, which
      # regenerated the ephemeral `/root/.qontinui/coord-jwt-ed25519.pkcs8`
      # JWT signing key and invalidated outstanding service tokens (web's
      # StrategyClient → 401). The ALB target group (`aws_lb_target_group`
      # below) is the sole authoritative health source: HTTP GET /health on
      # port 9870 from the ALB's SG. One source of truth; container-HC and
      # ALB-HC can no longer disagree.
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

  # Coord HA D.3 (PR-S1) — the ALB health check gates ECS rollout: a target is
  # "healthy enough to drain the previous one" only when it returns 200 here.
  # Repointed from /health (liveness: PG+Redis) to /ready (git-readiness: this
  # task has bootstrapped to the fleet ack-frontier, or the fleet is at
  # cold-start frontier 0). With this, `aws ecs wait services-stable` waits for
  # a new follower to hold the frontier before the old caught-up task is
  # deregistered — keeping >=1 caught-up live node across a rolling deploy and
  # closing the deploy-induced write-plane stall (plan
  # 2026-05-30-coord-ha-d3-failover-calibration, Axis c1). REQUIRES a coord
  # image that serves /ready (coord PR-1) deployed FIRST — see the landing
  # sequence; repointing before /ready exists would brick the next deploy.
  health_check {
    enabled             = true
    path                = "/ready"
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

# ─── HA Phase C — replica count, Fargate storage, and Multi-AZ notes ───────
#
# (a) SCRIPT-DRIVEN COUNT vs TERRAFORM
#     desired_count here sets the baseline that Terraform writes on first
#     apply.  Because of `lifecycle { ignore_changes = [desired_count] }` the
#     LIVE running count is owned by the replica-management stop/start scripts
#     (aws/scripts/staging-stop.sh + staging-start.sh).  `terraform apply`
#     will NOT reset the count after the service exists.  To promote a new
#     baseline: update var.desired_count AND run
#       terraform apply -target=module.coord.aws_ecs_service.coord
#     OR set the count via `aws ecs update-service --desired-count N`.
#
# (b) FARGATE EPHEMERAL STORAGE — MANDATORY C.1 BOOTSTRAP ON EVERY PLACEMENT
#     Fargate tasks have NO persistent local disk.  Every task replacement
#     (ECS stops a task and schedules a new one) starts with an EMPTY git
#     store.  The Phase C.1 standby bootstrap — cloning canonical repos from
#     the current leader via the coord git-http API — is therefore MANDATORY
#     on EVERY task start, not only the first boot.  The metric
#     `coord_git_replica_bootstrap_seconds` must be measured against the
#     largest repo to gate the Fargate-vs-EC2+EBS store decision (see
#     HA-PHASE-C-STORE-DECISION.md in this directory).
#
# (c) MULTI-AZ — depends on private_subnet_ids spanning >=2 AZs
#     ECS Fargate spreads tasks across the subnets listed in
#     network_configuration.subnets.  Multi-AZ placement is guaranteed only
#     when those subnets cover >=2 availability zones.  The staging network
#     module uses az_count=2 (default), producing two private subnets — one
#     per AZ (us-east-1a + us-east-1b).  If az_count is ever reduced to 1,
#     Multi-AZ placement is lost silently; do NOT do this on a live HA
#     cluster.  The nat_gateway is in AZ[0] only (cost optimization);
#     prod should add one NAT per AZ for full AZ-failure isolation.
# ─────────────────────────────────────────────────────────────────────────────

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

  # Coord HA D.3 (PR-S1) — give a freshly-placed task time to bootstrap its
  # local git store to the fleet ack-frontier and start returning 200 from
  # /ready before the ALB's unhealthy-threshold can fail it out. A cold Fargate
  # task starts with an EMPTY git store and must mirror-fetch the canonical
  # repos from the current leader (coord_git_replica_bootstrap_seconds), so the
  # grace window must comfortably exceed that bootstrap; 180s leaves headroom
  # over the largest repo. Without this, a slow bootstrap could be killed before
  # it ever reaches readiness. (Liveness regressions still surface: a task that
  # never serves /ready fails the deployment — see the circuit breaker, PR-S2.)
  health_check_grace_period_seconds = 180

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200

  # Coord HA D.3 (PR-S2) — with the ALB health check now pointed at /ready
  # (PR-S1), a revision whose new tasks never catch up to the fleet ack-frontier
  # never go healthy. The circuit breaker turns that into a FAILED deployment
  # that auto-rolls-back to the last-good task set, instead of leaving the write
  # plane wedged. Belt-and-suspenders alongside coord CI's own post-deploy smoke
  # + PRIOR_TD rollback. Applies to ECS deployments however triggered (CI
  # update-service or terraform); composes with ignore_changes[task_definition].
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  enable_execute_command = true # SSM Session Manager into a running task

  # TF/CI seam. CI (qontinui-coord/.github/workflows/deploy-coord.yml) owns
  # DEPLOYMENT: on every merge it renders the full deploy/taskdef.json (image
  # pinned to the commit SHA, all env + secrets), `register-task-definition`s a
  # new revision, and points the service at it via `update-service`. Terraform
  # must therefore NOT revert the service's `task_definition` pointer to its own
  # `aws_ecs_task_definition.coord` revision — that would undo every CI deploy,
  # reverting the live SHA-pinned image back to the floating `:staging` bootstrap
  # and dropping the 14 live-only env vars + 6 live-only secrets coord needs
  # (GitHub OAuth/App, GitHub token, Vercel hooks, NATS). The terraform task def
  # is bootstrap-only (see its comment block) and the running revision is owned
  # entirely by CI. `desired_count` is owned by the stop/start replica scripts
  # (same rationale). So `terraform apply` stays a human-gated provisioning op
  # that never touches the running revision or the replica count.
  # (Mirrors module.web; see plan
  # 2026-05-30-qontinui-stack-terraform-state-reconciliation §3-A1.)
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_target_group.coord]
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "cluster_name" { value = aws_ecs_cluster.main.name }
output "cluster_arn" { value = aws_ecs_cluster.main.arn }
output "cluster_id" { value = aws_ecs_cluster.main.id }
output "service_name" { value = aws_ecs_service.coord.name }
output "target_group_arn" { value = aws_lb_target_group.coord.arn }
output "target_group_arn_suffix" { value = aws_lb_target_group.coord.arn_suffix }
output "task_exec_role_arn" { value = aws_iam_role.task_exec.arn }
output "task_role_arn" { value = aws_iam_role.task.arn }
