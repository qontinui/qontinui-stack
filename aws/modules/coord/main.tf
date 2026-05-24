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

# ─── Non-secret env literals ──────────────────────────────────────────────
# These are plain configuration values (not credentials) injected as task
# `environment` entries. Defaults match the LIVE staging task-def
# (qontinui-coord/deploy/taskdef.staging.json) so a `terraform plan` is a
# no-op against the running service; override per-environment as needed.

variable "coord_sso_default_role" {
  type        = string
  default     = "operator"
  description = "COORD_SSO_DEFAULT_ROLE — role assigned to SSO-authenticated users lacking an explicit mapping."
}

variable "coord_sso_default_tenant" {
  type        = string
  default     = "default"
  description = "COORD_SSO_DEFAULT_TENANT — tenant assigned to SSO-authenticated users lacking an explicit mapping."
}

variable "coord_oidc_audience" {
  type        = string
  default     = "1r83igrltq3hfko2fnifhmjvvk"
  description = "COORD_OIDC_AUDIENCE — expected `aud` claim (Cognito app-client id) for SSO ID tokens."
}

variable "coord_oidc_issuer" {
  type        = string
  default     = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_rgTB9dbZ1"
  description = "COORD_OIDC_ISSUER — OIDC issuer URL (Cognito user-pool) for SSO ID-token validation."
}

variable "coord_ecs_auto_recover" {
  type        = string
  default     = "1"
  description = "ECS_AUTO_RECOVER — when set, coord runs its self-recovery routines on boot in the ECS/Fargate environment."
}

variable "coord_vercel_watch_projects" {
  type        = string
  default     = "[{\"name\":\"qontinui-web\",\"github_repo\":\"qontinui/qontinui-web\",\"branch\":\"main\",\"root_directory\":\"frontend\"}]"
  description = "VERCEL_WATCH_PROJECTS — JSON array of Vercel projects coord watches for deploy-hook firing."
}

variable "coord_github_app_id" {
  type        = string
  default     = "3825026"
  description = "GITHUB_APP_ID — public GitHub App identifier (not a secret). Paired with the github_app_private_key secret for App auth."
}

variable "coord_github_app_installation_id" {
  type        = string
  default     = "134903706"
  description = "GITHUB_APP_INSTALLATION_ID — public GitHub App installation identifier (not a secret)."
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

# GitHub App private key (RSA PKCS#1/#8 PEM). Same operator-staged keypair
# posture as coord_jwt_signing_key above: terraform never sees or stores the
# PEM, it only resolves the secret by name (for IAM wiring) and references the
# ARN in the task-def. Lives under the module-owned `qontinui/staging/coord/*`
# namespace (live ARN suffix `-EbMtNa`).
#
# Operator-staging recipe (run ONCE before the first apply/deploy that
# references this data source):
#
#   # 1. Download the App's private key (.pem) from the GitHub App settings
#   #    page (Settings → Developer settings → GitHub Apps → <app> →
#   #    "Generate a private key"). App id 3825026, installation 134903706.
#   # 2. Create the secret (file:// avoids shell-escaping the multi-line PEM):
#   aws secretsmanager create-secret \
#     --region us-east-1 \
#     --name qontinui/staging/coord/github_app_private_key \
#     --description "GitHub App private key (PEM) for qontinui-coord (staging)" \
#     --secret-string file://github-app.private-key.pem
#   # 3. Securely delete the local PEM.
#
# Rotation: update-secret + ECS force-new-deployment (coord reads the PEM at
# boot via GITHUB_APP_PRIVATE_KEY).
data "aws_secretsmanager_secret" "github_app_private_key" {
  name = "qontinui/${var.environment}/coord/github_app_private_key"
}

# Externally-created secrets (NOT managed by this module). These predate the
# module and live under the `qontinui-coord-staging/*` name prefix; terraform
# resolves them by name to wire IAM + task-def `valueFrom` without owning their
# lifecycle or values. The github-oauth secret is a JSON document — individual
# keys are projected with the `:<json-key>::` valueFrom suffix in the task-def.
data "aws_secretsmanager_secret" "github_oauth" {
  name = "qontinui-coord-${var.environment}/github-oauth"
}

data "aws_secretsmanager_secret" "github_token" {
  name = "qontinui-coord-${var.environment}/github-token"
}

data "aws_secretsmanager_secret" "vercel_deploy_hooks" {
  name = "qontinui-coord-${var.environment}/vercel-deploy-hooks"
}

data "aws_secretsmanager_secret" "nats_url" {
  name = "qontinui-coord-${var.environment}/nats-url"
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

# Task exec role needs to read every secret the task-def references at
# launch time: the four module-created secrets, the operator-staged JWT
# signing key + GitHub App private key, and the four externally-created
# `qontinui-coord-staging/*` secrets (all via data sources above). Mirrors the
# live exec-role policy (11 secret ARNs). GetSecretValue on the bare secret ARN
# covers JSON-key projections (github-oauth) too — the `:key::` suffix is a
# task-def valueFrom concern, not an IAM resource.
data "aws_iam_policy_document" "task_exec_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.redis_url.arn,
      aws_secretsmanager_secret.webhook_secret.arn,
      aws_secretsmanager_secret.coord_admin_secret.arn,
      data.aws_secretsmanager_secret.coord_jwt_signing_key.arn,
      data.aws_secretsmanager_secret.github_app_private_key.arn,
      data.aws_secretsmanager_secret.github_oauth.arn,
      data.aws_secretsmanager_secret.github_token.arn,
      data.aws_secretsmanager_secret.vercel_deploy_hooks.arn,
      data.aws_secretsmanager_secret.nats_url.arn,
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

      # Reconciled to the full live env set (11 entries) from
      # qontinui-coord/deploy/taskdef.staging.json. All non-secret literals;
      # values are module variables defaulted to the live staging values so a
      # `terraform plan` is a no-op against the running revision.
      environment = [
        { name = "COORD_BIND_ADDR", value = "0.0.0.0:9870" },
        { name = "COORD_SSO_DEFAULT_ROLE", value = var.coord_sso_default_role },
        { name = "COORD_SSO_DEFAULT_TENANT", value = var.coord_sso_default_tenant },
        { name = "COORD_OIDC_AUDIENCE", value = var.coord_oidc_audience },
        { name = "COORD_OIDC_ISSUER", value = var.coord_oidc_issuer },
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
        { name = "ECS_AUTO_RECOVER", value = var.coord_ecs_auto_recover },
        { name = "RUST_LOG", value = "qontinui_coord=info,axum=info,tower_http=info" },
        { name = "VERCEL_WATCH_PROJECTS", value = var.coord_vercel_watch_projects },
        # GitHub App identifiers are PUBLIC (not secrets); the App private key
        # is the secret (see secrets[] below).
        { name = "GITHUB_APP_ID", value = var.coord_github_app_id },
        { name = "GITHUB_APP_INSTALLATION_ID", value = var.coord_github_app_installation_id },
      ]

      # Reconciled to the full live secret set (11 entries) from
      # qontinui-coord/deploy/taskdef.staging.json. Four module-created secrets,
      # two operator-staged keypair secrets (JWT + GitHub App), four
      # externally-created `qontinui-coord-staging/*` secrets (resolved via data
      # sources above). github-oauth is a JSON secret; client_id/client_secret
      # are projected with the `:key::` valueFrom suffix.
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
        # GitHub App private key (PEM). Pairs with GITHUB_APP_ID /
        # GITHUB_APP_INSTALLATION_ID env above for App-based GitHub auth.
        { name = "GITHUB_APP_PRIVATE_KEY", valueFrom = data.aws_secretsmanager_secret.github_app_private_key.arn },
        # github-oauth is a JSON secret; project individual keys with the
        # `:<json-key>::` valueFrom suffix (ECS Secrets Manager JSON support).
        { name = "GITHUB_OAUTH_CLIENT_ID", valueFrom = "${data.aws_secretsmanager_secret.github_oauth.arn}:client_id::" },
        { name = "GITHUB_OAUTH_CLIENT_SECRET", valueFrom = "${data.aws_secretsmanager_secret.github_oauth.arn}:client_secret::" },
        { name = "GITHUB_TOKEN", valueFrom = data.aws_secretsmanager_secret.github_token.arn },
        { name = "VERCEL_DEPLOY_HOOKS", valueFrom = data.aws_secretsmanager_secret.vercel_deploy_hooks.arn },
        { name = "NATS_URL", valueFrom = data.aws_secretsmanager_secret.nats_url.arn },
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

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200

  enable_execute_command = true # SSM Session Manager into a running task

  # TF/CI seam (mirrors module.web, qontinui-stack PR #17). Terraform owns the
  # canonical task-def DEFINITION (aws_ecs_task_definition.coord above:
  # cpu/memory/env/secrets/IAM/log) and all provisioning. CI / the coord deploy
  # path (aws/scripts/push-coord-image.sh + `aws ecs update-service`) owns
  # DEPLOYMENT: it registers a fresh SHA-pinned revision (inheriting this
  # canonical definition) and points the service at it. Terraform must NOT
  # revert the service's task_definition pointer to its own revision — that
  # would undo every coord deploy. The canonical definition stays live because
  # the deploy path inherits it; TF-side task-def changes (a new secret, an env)
  # take effect on the next deploy. desired_count stays script-owned (replica
  # stop/start). `terraform apply` is a human-gated provisioning op that never
  # touches the running revision.
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
