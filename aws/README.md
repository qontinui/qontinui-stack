# qontinui-stack on AWS

Identical architecture to `qontinui-stack/docker-compose.yml`, deployed on
managed AWS services. **Currently paved but unused** — per topology plan
§9 Phase 6, this directory exists so flipping `dev` → `staging` is one
`terraform apply`, not a sprint.

| Service          | Local (compose) | AWS staging                      |
|------------------|-----------------|----------------------------------|
| Postgres 16      | container       | RDS db.t4g.micro                 |
| Redis 7          | container       | ElastiCache cache.t4g.micro      |
| MinIO / S3       | MinIO container | S3 bucket                        |
| qontinui-coord   | container       | ECS Fargate (1 task, 0.25 vCPU)  |
| Cloudflare Tunnel| host daemon     | ALB → ACM cert → Route53 record  |

The runner switches environments via the profile file
(`~/.qontinui/profiles.json`) — see topology plan §3. No code changes;
just a different `DATABASE_URL` / `REDIS_URL` / `coord_url`.

## Cost estimate (us-east-1, monthly, on)

| Item                           | Approx. |
|--------------------------------|---------|
| RDS db.t4g.micro (single AZ)   | ~$13    |
| ElastiCache cache.t4g.micro    | ~$12    |
| S3 (10 GB + minimal traffic)   | ~$1     |
| ECS Fargate (0.25 vCPU, 24/7)  | ~$9     |
| ALB                            | ~$16    |
| CloudWatch logs                | ~$2     |
| **Total when running**         | **~$53/mo** |

When stopped (RDS + ElastiCache stopped, ECS scaled to 0): ~$3/mo for
storage + ALB. The on/off ritual lives in `scripts/staging-stop.sh` and
`staging-start.sh`.

## Layout

```
aws/
├─ README.md                         (this file)
├─ staging/                          (one environment per directory)
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

`prod/` later is `cp -r staging/ prod/` + size bumps + multi-AZ flags.
Modules don't change.

## Prerequisites

* Terraform 1.7+ installed.
* AWS CLI configured (`aws configure`) with credentials that can create
  VPC/RDS/ElastiCache/ECS/S3/IAM/ACM/Route53 resources.
* A Route53 hosted zone for the domain you'll attach (e.g. `qontinui.io`).
* The `qontinui-canonical-coord` Docker image pushed to ECR (the module
  expects an `image_uri` variable; `staging/main.tf` defaults to ECR
  `qontinui-coord:staging`). Push command in `scripts/push-coord-image.sh`.

## First apply

```bash
cd qontinui-stack/aws/staging
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars — domain name, hosted zone id, image tag
terraform init
terraform plan
terraform apply
```

Output includes the staging `database_url`, `redis_url`, and `coord_url`.
Stash them into a new `staging` profile in `~/.qontinui/profiles.json`,
then `qontinui_profile use staging` to switch.

## On/off ritual (cost control)

```bash
# Stop everything billable. RDS + ElastiCache go to "stopped";
# ECS service scales to 0. Storage stays (cheap).
bash scripts/staging-stop.sh

# Start everything back up.
bash scripts/staging-start.sh
```

Note: RDS supports stopping for up to 7 days, then auto-restarts. For
longer pauses, snapshot + destroy the instance and recreate from
snapshot when needed.

## Why not Aurora / Neon / managed-anything-fancier

* **Aurora Serverless v2** scales to 0.5 ACU minimum (~$45/mo idle), more
  expensive than db.t4g.micro for a staging that's mostly off.
* **Neon** is the right answer for a *managed cloud product* (per
  business-model plan §1) but staging is "user's own AWS" by design —
  Neon would mean the user pays a third party for what's already
  provisioned in their AWS account.
* **RDS db.t4g.micro** is the cheapest fully-managed Postgres. Single-AZ
  is fine for staging; prod's `prod/` flips to Multi-AZ.

When `qontinui.cloud` (the managed cloud product) launches, it runs on
Neon + Upstash + Cloudflare Workers — not on this Terraform. This
directory targets self-hosters who want to deploy to their own AWS.

## What this skeleton intentionally omits

* Multi-AZ. Single AZ for staging; flip the variable for prod.
* Backups beyond RDS's 7-day retention.
* WAF rules.
* Bastion host (use SSM Session Manager when you need shell into a task).
* Custom DB roles + RLS. Auth/RLS land with topology plan Phase 7.

Each omission is a follow-up plan, not a scope creep against this skeleton.
