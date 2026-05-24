# Cold-tier object storage for opt-in PTY output (coord-native sessions Phase 8).
#
# Replaces the dev MinIO `qontinui-session-output-cold` bucket when running on
# AWS. This is the COLD tier of the three-tier PTY-output retention model:
#
#   Hot  — JetStream replay buffer (last 64 KB, session-active only).  In-NATS;
#          no terraform.
#   Warm — coord.session_output (Postgres), 10 MB/session FIFO, 7-day post-close
#          TTL.  Created by the canonical alembic chain; no terraform.
#   Cold — THIS bucket.  One immutable object per session, 90-day TTL.
#
# Per-session object key layout (single source of truth — coord's S3 writer
# MUST produce exactly this):
#
#   tenant/<tenant_id>/session/<session_id>.log
#
# Why `tenant/<tenant_id>/...` is the leading prefix:
#   - Per-tenant prefixes let coord do quota accounting with a single
#     `ListObjectsV2(prefix="tenant/<tenant_id>/")` + summed `Size` (the
#     10 GB-cold-per-tenant default monetization knob — enforced coord-side,
#     not in S3; see the design note for where).
#   - A future per-tenant lifecycle / retention override is expressible as a
#     prefix-scoped lifecycle rule without touching other tenants' objects.
#   - `s3:ListBucket` is granted with a prefix condition so coord can only
#     enumerate within the bucket it owns (see the IAM section in the coord
#     module — this module only owns the bucket; the writer/reader policy is
#     attached to coord's task role).
#
# Objects are IMMUTABLE per session (written once at session close, never
# mutated), so bucket versioning is intentionally disabled — versioning would
# only accrue cost with no rollback value.
#
# NOTE: `terraform apply` for this module is an OPERATOR step (spaceship / CI
# with AWS creds). Nothing here provisions anything on its own.

variable "environment" { type = string }

variable "cold_ttl_days" {
  description = "Days after which a cold-tier session-output object expires. Phase 8 default is 90."
  type        = number
  default     = 90
}

resource "random_id" "suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "cold" {
  bucket = "qontinui-${var.environment}-session-output-cold-${random_id.suffix.hex}"

  tags = { Name = "qontinui-${var.environment}-session-output-cold" }
}

# Public access fully blocked — PTY output may contain redacted-but-sensitive
# operational data; the cold tier is never directly reachable. Reads go through
# coord (presigned URLs or proxied), never anonymous S3.
resource "aws_s3_bucket_public_access_block" "cold" {
  bucket = aws_s3_bucket.cold.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE at rest. AES256 (SSE-S3) matches the existing `blob` module; bump to
# aws:kms with a CMK in prod if per-tenant key isolation becomes a requirement.
resource "aws_s3_bucket_server_side_encryption_configuration" "cold" {
  bucket = aws_s3_bucket.cold.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Versioning DISABLED — one immutable object per session; no rollback value.
resource "aws_s3_bucket_versioning" "cold" {
  bucket = aws_s3_bucket.cold.id

  versioning_configuration {
    status = "Disabled"
  }
}

# Lifecycle: expire every cold-tier object after `cold_ttl_days` (default 90).
# The rule filters on the `tenant/` prefix that all session-output keys share,
# so it never touches anything an operator might park outside that namespace.
resource "aws_s3_bucket_lifecycle_configuration" "cold" {
  bucket = aws_s3_bucket.cold.id

  rule {
    id     = "expire-session-output-after-ttl"
    status = "Enabled"

    filter {
      prefix = "tenant/"
    }

    expiration {
      days = var.cold_ttl_days
    }
  }

  rule {
    id     = "abort-incomplete-multipart-after-7d"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "bucket_name" { value = aws_s3_bucket.cold.bucket }
output "bucket_arn" { value = aws_s3_bucket.cold.arn }
output "cold_ttl_days" { value = var.cold_ttl_days }

# The fixed key prefix all session-output objects live under. Consumed by the
# coord module to scope `s3:ListBucket` to this prefix (least privilege) and
# echoed to staging outputs so coord's runtime config can be derived from
# terraform rather than hardcoded.
output "key_prefix" { value = "tenant/" }
