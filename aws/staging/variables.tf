variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment label — used in resource names and tags."
  type        = string
  default     = "staging"
}

# ─── Networking ─────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of AZs to span. 2 is minimum for an ALB; single-AZ for cost is achieved by setting RDS multi_az=false, not by going to 1 AZ here."
  type        = number
  default     = 2
}

# ─── DNS / TLS ──────────────────────────────────────────────────────────

variable "domain_name" {
  description = "Base domain (e.g. qontinui.io). Coord ingress goes to <coord_subdomain>.<domain>."
  type        = string
  default     = "staging.qontinui.io"
}

variable "route53_zone_id" {
  description = "Existing Route53 hosted zone id for var.domain_name."
  type        = string
  default     = "Z02792161EHR967BO9804"
}

variable "coord_subdomain" {
  description = "Subdomain for the coord ALB ingress."
  type        = string
  default     = "coord"
}

variable "web_subdomain" {
  description = "Subdomain for the qontinui-web backend ALB ingress."
  type        = string
  default     = "web"
}

variable "frontend_url" {
  description = "Vercel frontend origin — used for the web backend's CORS allow-list and absolute links."
  type        = string
  default     = "https://qontinui.io"
}

variable "first_superuser_email" {
  description = "Email address of the bootstrap superuser that qontinui-web's init_db seeds at startup (a shell auth.users row with is_superuser=true; cognito_sub is stamped on by verified email at that operator's first Cognito login). This is the non-HTTP first-admin path — without it a deployed environment that reaches zero superusers is stranded. An address, not a credential: wired as a plain container environment variable, never through Secrets Manager. DEFAULTLESS ON PURPOSE — see below."
  type        = string

  # No default, deliberately, and NOT `default = \"\"` either. This variable
  # confers a superuser grant, so both silent outcomes are unacceptable:
  #   - a concrete default silently seeds THIS operator's address into any
  #     environment stood up from a copy of this root, handing a superuser row
  #     to a mailbox that environment's owner does not control;
  #   - an empty default silently makes the seed inert (init_db.py:55 is
  #     `if settings.FIRST_SUPERUSER_EMAIL:`), which is exactly the state this
  #     change exists to fix — the failure would be invisible until someone
  #     needed the recovery path and found it missing.
  # Defaultless fails LOUDLY at plan time instead, forcing an explicit choice.
  # The other operator-identity value in this root, `signup_allowlist`, reached
  # the same conclusion and went further: having no correct default, it stopped
  # being a variable at all and now comes from SSM (see below, and main.tf's
  # "Operator-staged, out-of-region" block). The concrete-default variables here (route53_zone_id,
  # cognito_user_pool_arn) are infrastructure identifiers, not identities that
  # confer privilege. The real value lives in terraform.tfvars.example as
  # documentation, and in the operator's own gitignored tfvars as behaviour.

  validation {
    # init_db.py:58 matches `User.email == FIRST_SUPERUSER_EMAIL` case-SENSITIVELY
    # against a column declared `unique=True` (models/user.py:36-37), while every
    # organically-created row is lowercased first (cognito_provision.py:_extract_email
    # does `.strip().lower()`). So a case variant misses the `if not user:` guard,
    # then trips the unique constraint on insert; the IntegrityError propagates out
    # of init_db and main.py's `except Exception: raise` aborts startup — an ECS
    # boot loop from a config typo. Reject the typo here instead.
    condition     = var.first_superuser_email == lower(var.first_superuser_email)
    error_message = "first_superuser_email must be lowercase: qontinui-web's init_db matches auth.users.email case-sensitively against a unique column, so a case variant crashes web startup on boot."
  }
}

# ─── Web backend service ────────────────────────────────────────────────

variable "web_image_uri" {
  description = <<-EOT
    ECR URI of the qontinui-web-backend image, used to render
    module.web's aws_ecs_task_definition.web. Ongoing deploys do NOT come from
    here: CI (qontinui-web/.github/workflows/deploy-web.yml) describes
    this family's latest revision, swaps in the SHA-pinned image it just built,
    and registers a new one — see the TF/CI seam comment in modules/web/main.tf.

    Defaults to the floating `:staging` tag, which the deploy pipeline moves onto
    every image it pushes, matching migrator_image_uri directly above. It was
    `""` until 2026-08-15; that was the entire reason `terraform plan` showed
    `aws_ecs_task_definition.web` "must be replaced" forever — state held the
    floating tag, config resolved to the empty string, and the two could never
    agree. An empty default also does not do what its old comment claimed: plan
    accepts `image = ""` silently, so a missed push surfaces at apply time as a
    confusing failure rather than at plan time as a clear one.
  EOT
  type        = string
  default     = "047719635665.dkr.ecr.us-east-1.amazonaws.com/qontinui-web-backend:staging"
}

variable "migrator_image_uri" {
  description = "ECR URI of the canonical-DB migrator image (alembic upgrade head; built from origin/main qontinui-web alembic chain)."
  type        = string
  default     = "047719635665.dkr.ecr.us-east-1.amazonaws.com/qontinui-migrator:staging"
}

variable "web_cpu" {
  description = "Fargate task CPU units for web. 512 = 0.5 vCPU (FastAPI + asyncpg)."
  type        = number
  default     = 512
}

variable "web_memory_mb" {
  description = "Fargate task memory (MB) for web."
  type        = number
  default     = 1024
}

variable "web_desired_count" {
  description = "Web task replicas."
  type        = number
  default     = 1
}

# ─── Cross-IdP account linking (Cognito) ─────────────────────────────────

variable "cognito_user_pool_arn" {
  description = <<-EOT
    ARN of the Cognito user pool used for federated auth + cross-IdP account
    linking. This pool is MANUALLY managed and intentionally NOT in Terraform
    (never imported) — it is referenced by ARN only. Two things scope to it:
      1. the web ECS task role's cognito-idp admin grant (module.web), and
      2. the PreSignUp auto-link Lambda's grant + invoke permission
         (module.cross_idp_linking).
    The PreSignUp trigger attachment on the pool itself is a one-time manual
    `aws cognito-idp update-user-pool` step (pool not in TF) — see
    modules/cross-idp-linking/main.tf.
  EOT
  type        = string
  default     = "arn:aws:cognito-idp:us-east-1:047719635665:userpool/us-east-1_rgTB9dbZ1"
}

# ─── Transactional email (SES) ───────────────────────────────────────────

variable "ses_sender_identity_arn" {
  description = <<-EOT
    ARN of the SES identity the web task sends transactional mail from. Like the
    Cognito pool above, this identity is MANUALLY managed and intentionally NOT
    in Terraform (never imported) — it is referenced by ARN only. The web ECS
    task role's ses:SendEmail grant scopes to this ARN and nothing else, so a
    Source outside the identity is AccessDenied by IAM before SES sees it.
    staging.qontinui.io is domain-verified with DKIM in us-east-1/047719635665,
    and the account has SES production access (no sandbox recipient list).
  EOT
  type        = string
  default     = "arn:aws:ses:us-east-1:047719635665:identity/staging.qontinui.io"
}

variable "ses_from_email" {
  description = <<-EOT
    Source address for transactional mail — reaches the backend as
    SMTP_FROM_EMAIL. MUST be under the domain of ses_sender_identity_arn. This
    is the same address the Cognito invitation template already sends from, so
    an invited user sees one sender across the invite and every later notice.
  EOT
  type        = string
  default     = "no-reply@staging.qontinui.io"
}

# signup_allowlist is no longer a variable. It moved to SSM
# (/qontinui/ops/signup-allowlist, eu-central-1) and reaches
# module.cross_idp_linking as a data source — see the "Operator-staged,
# out-of-region" block in main.tf.
#
# Removed rather than kept-and-ignored on purpose. As a variable it defaulted to
# "", and "" means enforcement DISABLED (fail-open) in the PreSignUp handler. The
# live pool has always carried a real allowlist set out-of-band, so ANY apply run
# without `-var signup_allowlist=...` would have silently dropped the
# invitation-only gate on production — a security regression triggered by
# forgetting a flag, which is exactly the shape of failure a default should never
# have. There is no correct default for this value, so it stops being a variable.
# (plan 2026-08-04-stack-terraform-state-reconciliation, P3.)

# ─── Cost control ───────────────────────────────────────────────────────

variable "budget_monthly_limit" {
  description = "Monthly AWS Budget limit in USD."
  type        = string
  default     = "100"
}


# ─── Postgres ───────────────────────────────────────────────────────────

variable "postgres_instance_class" {
  description = <<-EOT
    RDS instance class. History: db.t4g.micro (1GiB) -> db.t4g.medium (4GiB)
    2026-06-09 after chronic OOM (FreeableMemory floor ~90MiB, 4 crashes/36h,
    RDS auto-halving shared_buffers).

    Then -> db.m6g.xlarge (16GiB, 4 vCPU) out-of-band, i.e. by a console or CLI
    ModifyDBInstance rather than an apply. The date is not established: RDS's
    event history only retains 14 days and shows no class change in that window
    (checked 2026-08-15). Reconciled into config here, because terraform's
    default was still db.t4g.medium and an untargeted `terraform plan` therefore
    read as a 4x DOWNSIZE of production Postgres, with a reboot, presented as an
    innocuous "1 to change".

    Keep this value equal to live. If you resize, resize here and apply — an
    out-of-band resize re-arms exactly the trap this line documents.
  EOT
  type        = string
  default     = "db.m6g.xlarge"
}

variable "postgres_allocated_storage_gb" {
  description = "Initial allocated storage. RDS auto-grows up to max_allocated_storage."
  type        = number
  default     = 20
}

variable "postgres_max_allocated_storage_gb" {
  description = <<-EOT
    Storage auto-grow ceiling (RDS MaxAllocatedStorage).

    Raised 100 -> 200 on the live instance qontinui-staging by AWS CLI at
    2026-09-24T19:54:10Z (CloudTrail ModifyDBInstance). Storage autoscaling had
    grown the disk to the 100 GiB ceiling on 2026-09-12, and by 2026-09-24 only
    about 15 GB was free, falling ~1.5-2 GB/day.

    This default matches live, so an apply does not lower the ceiling. Lowering
    it back to 100, where allocated storage already sits, would at best fail
    the apply and at worst re-arm the storage-full trap. A gitignored operator
    *.tfvars can still override this default, so check that file before
    applying. Neither tracked tfvars file sets it. Plan:
    2026-09-24-coord-rds-storage-has-fifteen-gb-headroom-and-no-autoscaling.
  EOT
  type        = number
  default     = 200
}

variable "postgres_username" {
  description = "Master username."
  type        = string
  default     = "qontinui_user"
}

variable "postgres_db_name" {
  description = "Initial database name."
  type        = string
  default     = "qontinui_db"
}

variable "postgres_multi_az" {
  description = "Multi-AZ RDS. Currently single-AZ; set true when HA is needed."
  type        = bool
  default     = false
}

variable "postgres_backup_retention_days" {
  description = "RDS automated-backup retention in days: the point-in-time recovery window, 7 by default. modules/postgres refuses 0, because 0 disables automated backups and RDS then deletes every existing one when that change takes effect."
  type        = number
  default     = 7
}

# ─── Postgres off-instance backups (modules/backup) ─────────────────────

variable "postgres_offinstance_backup_enabled" {
  description = <<-EOT
    Off-switch for the AWS Backup plan that keeps copies of the coord database
    OUTSIDE the RDS instance: a snapshot in a vault in this region, copied to a
    vault in postgres_offinstance_backup_dr_region. Default true, because the
    capability ships on. It costs money every month, so this is the
    preference to turn off.

    false removes the plan, the selection and the two staleness alarms, so no
    NEW backup or copy is taken. Recovery points already taken stay until
    their own retention runs out. The vaults and the role stay too: an empty
    vault costs nothing, and a vault holding points refuses to be destroyed.

    Recurring cost at the defaults (daily, 35 days in both regions). RDS bills
    these snapshots as backup storage at $0.095/GB-month in us-east-1 and
    us-east-2, and transfer from us-east-1 to us-east-2 costs $0.01/GB. With
    B = the first full copy (~125 GB) and d = GB of distinct blocks changed per
    day:
      $0.095 x (B + 62 x d) + $0.30 x d per month = $11.9 + $6.19 x d
    That is $34-142/month at the two readings of d (3.5 and 21 GB/day), with a
    hard ceiling of about $210/month at today's churn (d <= ~32 GiB/day), plus
    $0.20/month for two alarms and about $1.25 once for the first full copy. B
    grows with the database, adding about $5/month for each month of growth. d
    is measured from Cost Explorer 8 days after the first copy. A weekly
    schedule (see postgres_offinstance_backup_schedule) is the cheaper setting
    that still keeps 35 days: $11.9 + $0.80 x w, where w = distinct blocks
    changed per week, i.e. $15-130/month (ceiling about $192).

    Plan 2026-10-14-coord-database-is-one-command-from-total-loss, claim
    r02-off-instance-backup.
  EOT
  type        = bool
  default     = true
}

variable "postgres_offinstance_backup_schedule" {
  description = <<-EOT
    AWS Backup cron expression (UTC) for the off-instance snapshot. The default,
    07:00 daily, stays clear of the instance's backup window (03:00-04:00) and its
    Sunday maintenance window (04:30-05:30). AWS Backup fails an RDS job
    scheduled inside either window or in the hour before it. The cheaper
    weekly setting is cron(0 7 ? * MON *).

    Only a daily cron(M H * * ? *) or a weekly cron(M H ? * DAY *) is accepted,
    because modules/backup keys its alarms to the cadence. Daily gets two "no
    backup / no copy completed for 26 h" staleness alarms. Weekly gets
    failed/expired event alarms instead: CloudWatch caps an alarm window at 7
    days, so no staleness alarm can cover a weekly gap without firing every
    week. Changing this value changes the alarm set in the same apply.
  EOT
  type        = string
  default     = "cron(0 7 * * ? *)"
}

variable "postgres_offinstance_backup_retention_days" {
  description = "Days each recovery point stays in this region's vault. 35 clears the r02-off-instance-backup claim's 30-day floor with margin; below 30 is refused so the config cannot quietly drop under what the claim requires."
  type        = number
  default     = 35

  validation {
    condition     = var.postgres_offinstance_backup_retention_days >= 30
    error_message = "postgres_offinstance_backup_retention_days must be at least 30: claim r02-off-instance-backup requires the off-instance copy to be kept at least 30 days."
  }
}

variable "postgres_offinstance_backup_dr_region" {
  description = <<-EOT
    Second region for the backup copies. us-east-2 has the same snapshot storage
    price as us-west-2 at half the transfer price from us-east-1 ($0.01 vs
    $0.02/GB). Changing it creates a new copy vault in the new region and leaves
    the old one behind. The points in the old vault expire on their own, and the
    first copy into the new vault is a full copy.
  EOT
  type        = string
  default     = "us-east-2"
}

variable "postgres_offinstance_backup_dr_retention_days" {
  description = "Days each copy stays in the second region's vault. 35 keeps the claim's 30-day floor in the second region too; below 30 is refused."
  type        = number
  default     = 35

  validation {
    condition     = var.postgres_offinstance_backup_dr_retention_days >= 30
    error_message = "postgres_offinstance_backup_dr_retention_days must be at least 30: claim r02-off-instance-backup requires the second-region copy to be kept at least 30 days too."
  }
}

# ─── Redis ──────────────────────────────────────────────────────────────

variable "redis_node_type" {
  description = "ElastiCache node type. cache.t4g.micro is current sizing."
  type        = string
  default     = "cache.t4g.micro"
}

# ─── Coord service ──────────────────────────────────────────────────────

variable "coord_image_uri" {
  description = <<-EOT
    ECR URI of the qontinui-canonical-coord image. Used ONLY by the
    initial `terraform apply` that creates the task definition; ongoing
    coord deploys SHA-pin a fresh revision via
    `scripts/push-coord-image.sh` + `aws ecs update-service`. The
    script tags every build as both `:<sha>` and `:staging`, so leaving
    this at `:staging` in terraform.tfvars is safe — terraform won't
    be re-applied for image changes.
  EOT
  type        = string
  # Defaults to the floating `:staging` tag the push script always moves, exactly
  # as the description above says is safe, and matching migrator_image_uri. This
  # was `""` until 2026-08-15 with the comment "left empty so a missed push
  # surfaces immediately at apply time" — it did not do that. `terraform plan`
  # accepts `image = ""` without complaint, so nothing surfaced at plan time;
  # what it DID produce was a permanent `aws_ecs_task_definition.coord` "must be
  # replaced" (state held `:staging`, config resolved to `""`), which is one of
  # the two destroys plan 2026-08-04-stack-terraform-state-reconciliation exists
  # to clear.
  default = "047719635665.dkr.ecr.us-east-1.amazonaws.com/qontinui-coord:staging"
}

variable "coord_cpu" {
  description = "Fargate task CPU units. 256 = 0.25 vCPU."
  type        = number
  default     = 256
}

variable "coord_memory_mb" {
  description = "Fargate task memory in MB."
  type        = number
  default     = 512
}

variable "coord_desired_count" {
  description = <<-EOT
    DESIRED BASELINE replica count for the coord ECS service (HA Phase C).
    Default raised to 2 so Terraform provisions the service with at least two
    tasks spread across the two private subnets (one per AZ — see az_count).

    IMPORTANT — this value is the baseline written into the Terraform state.
    The LIVE running count is managed operationally by the replica-management
    stop/start scripts (aws/scripts/stop.sh + start.sh), which call
    `aws ecs update-service --desired-count N` directly.  Because the ECS
    service resource has `lifecycle { ignore_changes = [desired_count] }`,
    running `terraform apply` will NOT override whatever count the scripts last
    set.  The baseline here only takes effect on a fresh `terraform apply`
    against a service that does not yet exist, or after an explicit
    `terraform apply -target=module.coord.aws_ecs_service.coord`.

    Cross-reference: HA Phase C plan — coord HA Phase C.6 (multi-AZ replica
    baseline + chaos validation).
  EOT
  type        = number
  default     = 2
}


# ─── Cold-tier session output (Phase 8) ─────────────────────────────────

variable "session_output_cold_ttl_days" {
  description = "Days after which a cold-tier PTY-output session object expires (S3 lifecycle). Phase 8 default is 90."
  type        = number
  default     = 90
}
