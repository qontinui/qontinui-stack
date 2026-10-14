# qontinui-stack on AWS (production)

Production environment deployed on managed AWS services. The `staging/`
directory name and internal `environment = "staging"` resource prefix are
historical artifacts -- changing the environment label would destroy and
recreate data resources, so the names are preserved.

| Service          | Local (compose) | AWS (production)                 |
|------------------|-----------------|----------------------------------|
| Postgres 16      | container       | RDS db.t4g.medium                |
| Redis 7          | container       | ElastiCache cache.t4g.micro      |
| MinIO / S3       | MinIO container | S3 bucket                        |
| qontinui-coord   | container       | ECS Fargate (2 tasks, 0.25 vCPU) |
| Cloudflare Tunnel| host daemon     | ALB → ACM cert → Route53 record  |

The runner switches environments via the profile file
(`~/.qontinui/profiles.json`) — see topology plan §3. No code changes;
just a different `DATABASE_URL` / `REDIS_URL` / `coord_url`.

## Cost estimate (us-east-1, monthly, on)

| Item                           | Approx. |
|--------------------------------|---------|
| RDS db.t4g.medium (single AZ)  | ~$49    |
| ElastiCache cache.t4g.micro    | ~$12    |
| S3 (10 GB + minimal traffic)   | ~$1     |
| ECS Fargate (0.25 vCPU, 24/7)  | ~$9     |
| ALB                            | ~$16    |
| CloudWatch logs                | ~$2     |
| **Total when running**         | **~$89/mo** |

When stopped (RDS + ElastiCache stopped, ECS scaled to 0): ~$3/mo for
storage + ALB. The on/off ritual lives in `scripts/stop.sh` and
`start.sh`.

Not in the table above: the off-instance database backups, about
$34-142/month (hard ceiling about $210/month at today's churn). See "Backups
and DR" below for the formula and the off-switch.

## Layout

```
aws/
├─ README.md                         (this file)
├─ staging/                          (production — name is historical)
│  ├─ main.tf                        (composition root — wires modules)
│  ├─ variables.tf
│  ├─ outputs.tf
│  ├─ providers.tf
│  ├─ backend.tf                     (S3 + DynamoDB state lock)
│  └─ terraform.tfvars.example
└─ modules/
   ├─ network/                       (VPC, subnets, SGs)
   ├─ postgres/                      (RDS + parameter group + secrets)
   ├─ redis/                         (ElastiCache replication group)
   ├─ blob/                          (S3 bucket + IAM policy)
   ├─ coord/                         (ECS Fargate service for qontinui-coord)
   └─ tunnel/                        (ALB + ACM + Route53 — ingress)
```

`staging/` IS production. Modules are reusable across environments.

## Prerequisites

* Terraform 1.7+ installed.
* AWS CLI configured (`aws configure`) with credentials that can create
  VPC/RDS/ElastiCache/ECS/S3/IAM/ACM/Route53 resources.
* A Route53 hosted zone for the domain you'll attach (e.g. `qontinui.io`).
* The `qontinui-canonical-coord` Docker image pushed to ECR (see
  "Coord deploys" below). The terraform module expects an `image_uri`
  variable. Real deploys SHA-pin the task definition revision via
  `scripts/push-coord-image.sh`; terraform's default is used only for
  the initial bring-up.

## First apply

```bash
cd qontinui-stack/aws/staging
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars — domain name, hosted zone id
bash ../scripts/push-coord-image.sh   # first push, registers + deploys
terraform init
terraform plan
terraform apply
```

Output includes the `database_url`, `redis_url`, and `coord_url`.
Stash them into a profile in `~/.qontinui/profiles.json`,
then `qontinui_profile use <profile>` to switch.

## Coord deploys (ongoing)

For routine coord changes, do NOT re-run `terraform apply`. The push
script registers a new task definition revision pinned to the commit
SHA and updates the ECS service directly:

```bash
# From a checkout of qontinui-coord on the commit you want to ship:
cd qontinui-coord
git status   # must be clean — the script fails on dirty trees
cd ../qontinui-stack
AWS_REGION=us-east-1 ENVIRONMENT=staging \
    bash aws/scripts/push-coord-image.sh
```

The script:

1. Computes the short commit SHA from the qontinui-coord checkout.
2. Builds linux/amd64 + pushes both `:<sha>` and `:staging` to ECR.
3. Registers a new task definition revision pinned at `:<sha>`.
4. Updates the ECS service + waits for rollout completion.

Why SHA-pin instead of letting ECS chase `:staging`? Two reasons:
rollback (`aws ecs update-service --task-definition <family>:<old-rev>`
is one command if you know the SHA), and audit (every task definition
revision records the exact image it ran). The `:staging` alias is kept
moving so first-time `terraform apply` works without a separate push.

To dry-run (push image + register revision, skip the service update):
`SKIP_DEPLOY=1 bash aws/scripts/push-coord-image.sh`.

## On/off ritual (cost control)

```bash
# Stop everything billable. RDS + ElastiCache go to "stopped";
# ECS service scales to 0. Storage stays (cheap).
bash scripts/stop.sh

# Start everything back up.
bash scripts/start.sh
```

Note: RDS supports stopping for up to 7 days, then auto-restarts. For
longer pauses, snapshot + destroy the instance and recreate from
snapshot when needed. Deletion protection is on, so that destroy first
needs the steps under "Backups and DR". A stopped instance cannot be
snapshotted, so on the daily cadence a pause past 07:00 UTC
(`aws/scripts/staging-stop.sh`) trips the backup-stale alarm.

## Backups and DR

The database (`qontinui_db` on RDS `qontinui-<env>`) has three backup tiers.
Restores are in qontinui-coord `docs/dr-runbook.md`.

| Tier | Where | Kept | Survives deleting the instance? |
|------|-------|------|---------------------------------|
| RDS automated backups (point-in-time recovery) | RDS, this region | `postgres_backup_retention_days` (7) | Only a `terraform destroy` keeps them (`delete_automated_backups = false`), until that retention runs out. A console, CLI or API delete removes them unless told otherwise |
| AWS Backup snapshot (`modules/backup`), 07:00 UTC daily | vault `qontinui-<env>-rds`, this region | `postgres_offinstance_backup_retention_days` (35; at least 30) | Yes |
| Copy of each snapshot | vault `qontinui-<env>-rds-dr`, `postgres_offinstance_backup_dr_region` (us-east-2) | `postgres_offinstance_backup_dr_retention_days` (35; at least 30) | Yes, and survives the loss of this region |

A `terraform destroy` also takes a final snapshot, `qontinui-<env>-final`
(`skip_final_snapshot = false`). The CLI takes one only when given
`--final-db-snapshot-identifier`.

**Deletion protection** is on (`modules/postgres` `deletion_protection`,
default true). It is the only barrier that holds for every caller: RDS refuses
to delete the instance from Terraform, the console, the CLI and the API alike.
To tear an environment down on purpose:

1. Set `deletion_protection = false` on the module and apply.
2. Destroy. The destroy takes the final snapshot; delete it yourself if you
   do not want it.
3. The backup vaults refuse destruction while they hold recovery points
   (`force_destroy = false`). Delete the points first.

**What a snapshot costs the instance.** The instance is single-AZ, so each
snapshot starts with a brief I/O suspension, "a few seconds to a few
minutes" (RDS, "Creating a DB snapshot"). The snapshot then completes in the
background; the automated backups measure 27-40 min end to end. The AWS
Backup job at 07:00 UTC adds one such suspension a day, clear of the
03:00-04:00 backup window and the Sunday 04:30-05:30 maintenance window.

**Cost and the off-switch.** Off-instance backups cost money every month, so
they have an off-switch: `postgres_offinstance_backup_enabled` (default
true). Turning it off stops new backups and copies, and existing ones expire
on their own schedule.

- RDS bills these snapshots as backup storage, at $0.095/GB-month in both
  regions, plus $0.01/GB to transfer each copy from us-east-1 to us-east-2.
- Daily (the default) costs about `$11.9 + $6.19 × d` per month, where `d` is
  the GB of distinct blocks changed per day. That is $34-142/month at the two
  readings of `d` (3.5 and 21 GB/day), with a hard ceiling of about $210/month
  at today's churn.
- Weekly (`cron(0 7 ? * MON *)`) is the cheaper setting that still keeps 35
  days: `$11.9 + $0.80 × w`, where `w` is the distinct blocks changed per week.
  That is $15-130/month, ceiling about $192.

**Alarms**, on the budget topic, are keyed to the schedule's cadence.

- **Daily:** two staleness alarms, which fire when nothing has completed for 26
  hours.
  - `qontinui-<env>-rds-backup-stale`: no backup job completed.
  - `qontinui-<env>-rds-backup-copy-stale`: no copy job completed into the DR
    vault.
  - Both sit in ALARM from apply until the first scheduled run completes.
- **Weekly:** CloudWatch caps an alarm window at 7 days, so no staleness alarm
  can cover a weekly gap. Three event alarms fire instead:
  - `qontinui-<env>-rds-backup-failed`
  - `qontinui-<env>-rds-backup-expired`
  - `qontinui-<env>-rds-backup-copy-failed`

Changing the schedule changes the alarm set in the same apply.

## Why not Aurora / Neon / managed-anything-fancier

* **Aurora Serverless v2** scales to 0.5 ACU minimum (~$45/mo idle),
  comparable to db.t4g.medium but with less predictable cost under spikes.
* **Neon** is the right answer for a *managed cloud product* (per
  business-model plan §1) but this environment is "user's own AWS" by
  design — Neon would mean the user pays a third party for what's already
  provisioned in their AWS account.
* **RDS db.t4g.medium** is cheap, fully-managed Postgres (upsized from micro
  2026-06-09 after chronic 1GiB OOM). Single-AZ currently; flip to Multi-AZ
  when HA is needed (note: Multi-AZ is for availability, not the OOM fix).

When `qontinui.cloud` (the managed cloud product) launches, it runs on
Neon + Upstash + Cloudflare Workers — not on this Terraform. This
directory targets self-hosters who want to deploy to their own AWS.

## What this configuration intentionally omits

* Multi-AZ. Single AZ currently; flip the variable when HA is needed.
* WAF rules.
* Bastion host (use SSM Session Manager when you need shell into a task).
* Custom DB roles + RLS. Auth/RLS land with topology plan Phase 7.

Each omission is a follow-up plan, not a scope creep against this skeleton.
