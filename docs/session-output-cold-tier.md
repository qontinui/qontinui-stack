# Session-output cold-tier storage (coord-native sessions, Phase 8)

This document describes the **cold tier** of the opt-in PTY-output retention
model introduced in Phase 8 of the coord-native-sessions plan
(`qontinui-dev-notes/plans/2026-05-23-coord-native-sessions-phase-7-10.md`).

This PR ships **only the storage substrate** — the S3 bucket, its IAM grant,
and dev MinIO parity. The runtime streaming/redaction/eviction code lands with
Phase 8 proper (in qontinui-runner + qontinui-coord). Nothing here is provisioned;
`terraform apply` is an operator step (see "What remains for the operator").

## Three-tier retention model

When a session opts in (`share_output: true` in its Intent), the runner
publishes PTY bytes to `qontinui.sessions.<tenant>.<machine>.<session>.output`.
coord persists that stream across three tiers, each with a different
latency/durability/cost trade-off:

| Tier     | Backing store                         | Window / size                         | TTL                | Provisioned by |
|----------|---------------------------------------|---------------------------------------|--------------------|----------------|
| **Hot**  | JetStream replay buffer               | last 64 KB, **session-active only**   | ends at session close | NATS / JetStream config (no terraform) |
| **Warm** | `coord.session_output` (Postgres)     | 10 MB/session FIFO                    | 7 days post-close  | canonical alembic chain (no terraform) |
| **Cold** | **S3 object per session** (this PR)   | one immutable object per session      | **90 days**        | `modules/session-output-cold` (terraform — operator applies) |

- **Hot** is for the live dashboard xterm pane subscribing to the JetStream
  subject — sub-second, ephemeral.
- **Warm** is the recently-closed-session scrollback, queryable from Postgres
  without an S3 round-trip.
- **Cold** is the durable archive: the full session output, written once at
  session close, retained 90 days, then lifecycle-expired.

Eviction is **per-session FIFO** so one noisy session never pushes another
session's history out of the warm tier.

## Cold-tier object layout

One immutable object per session. Key:

```
tenant/<tenant_id>/session/<session_id>.log
```

- **`tenant/<tenant_id>/` leading prefix** is deliberate:
  - **Quota accounting** — coord computes a tenant's cold usage with a single
    `ListObjectsV2(prefix="tenant/<tenant_id>/")` and sums object `Size`.
  - **Per-tenant lifecycle** — a future per-tenant retention override is a
    prefix-scoped lifecycle rule that touches no other tenant's objects.
  - **Least-privilege list** — coord's `s3:ListBucket` is conditioned on the
    `tenant/` prefix, so the role can enumerate session output and nothing else
    that might land in the bucket.
- Objects are **immutable** (written once, never mutated) → bucket
  **versioning is disabled** (versioning would only accrue cost with no
  rollback value). Expiry is owned entirely by the 90-day lifecycle rule, not
  by coord — which is why coord's IAM grant has **no `s3:DeleteObject`**.

## Bucket configuration (`modules/session-output-cold/main.tf`)

- **Name:** `qontinui-${environment}-session-output-cold-${random_id.hex}`
  — follows the existing `blob` module's `qontinui-${env}-<purpose>-<suffix>`
  convention (globally-unique random suffix).
- **SSE at rest:** AES256 (SSE-S3), matching the `blob` module. Bump to
  `aws:kms` + a CMK in prod if per-tenant key isolation is required.
- **Versioning:** disabled (immutable per-session objects).
- **Public access:** fully blocked (all four `block/ignore/restrict` flags).
  Reads go through coord (presigned or proxied), never anonymous S3.
- **Lifecycle:** expire objects under `tenant/` after `cold_ttl_days`
  (default **90**); plus abort-incomplete-multipart after 7 days.

## IAM scoping (`modules/coord/main.tf`)

coord is the **sole writer/reader**. A dedicated least-privilege inline policy
(`aws_iam_role_policy.task_session_output_cold`) is attached to coord's ECS
**task role** — kept separate from the existing `task_blob` policy so the
cold-tier grant stays auditable and minimal:

- **Objects:** `s3:GetObject` + `s3:PutObject` on
  `arn:…:<bucket>/tenant/*` only. **No `s3:DeleteObject`** — the lifecycle rule
  owns expiry.
- **Listing:** `s3:ListBucket` on the bucket ARN, conditioned with
  `StringLike s3:prefix = tenant/*` — coord can enumerate session output for
  quota accounting but cannot list anything else.

The cold-tier bucket ARN + key prefix flow from the staging composition root
(`aws/staging/main.tf`) into the coord module via
`session_output_cold_bucket_arn` / `session_output_cold_key_prefix`, mirroring
how `module.blob.bucket_arn` is already threaded in.

## Per-tenant quota knobs (monetization)

Default aggregate quotas per tenant (Phase 8 spec):

- **Warm:** 1 GB
- **Cold:** 10 GB

These are **enforced coord-side, not in S3** — S3 has no native per-prefix
quota. Enforcement points (runtime code, lands with Phase 8 proper, NOT in this
PR):

- **Warm:** coord's `coord.session_output` writer checks the tenant's summed
  warm bytes before insert; over-quota → FIFO-evict oldest within the tenant.
- **Cold:** coord's S3 writer checks the tenant's summed cold object size
  (`ListObjectsV2(prefix="tenant/<tenant_id>/")`) before `PutObject`;
  over-quota → refuse the write (or evict the oldest session object for that
  tenant, TBD in the runtime PR).

The defaults are a **monetization knob**: paid tiers raise the per-tenant warm
and cold ceilings. They would live in coord's `tenant_policies` (alongside
`session_coordination_enabled` from Phase 10), so they're settable per tenant
without a redeploy.

## Dev parity (MinIO)

The dev stack mirrors the cold tier via a MinIO bucket created by the
idempotent bucket-creation sidecar in `docker-compose.yml`:

- Bucket: `qontinui-session-output-cold` (override via
  `SESSION_OUTPUT_COLD_BUCKET` in `.env`).
- Kept **private** (no `mc anonymous set download`) to mirror staging's full
  public-access block. Same key layout (`tenant/<tenant_id>/session/<id>.log`)
  applies; MinIO has no lifecycle rule, so dev objects don't auto-expire (fine
  for local dev).

## What remains for the operator

This PR provisions **nothing**. Before the cold tier is live in staging:

1. **`terraform apply`** in `aws/staging/` (spaceship / CI with AWS creds).
   `cd qontinui-root/qontinui-stack/aws/staging && terraform init && terraform plan && terraform apply`.
   Creates: the `qontinui-staging-session-output-cold-<suffix>` bucket (with
   public-access block, SSE, versioning-disabled, 90-day lifecycle) + the
   scoped `s3:Get/Put/List` policy on coord's task role.
2. **Wire coord's runtime config** to the bucket — read the
   `session_output_cold` terraform output (`terraform output session_output_cold`)
   and feed `bucket` / `region` / `endpoint` / `key_prefix` into coord's
   environment when the Phase 8 streaming code ships. No new secret is needed
   (coord authenticates to S3 via its task role; no static keys).
3. **No new Secrets Manager entries** are required for the cold tier.

Optional: set `session_output_cold_ttl_days` in `terraform.tfvars` to override
the 90-day default (e.g. for a tenant SLA), and bump SSE to KMS for prod.
