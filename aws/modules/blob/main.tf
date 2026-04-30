# S3 bucket for snapshots / screenshots / recordings / large embeddings.
# Replaces MinIO when running on AWS.

variable "environment" { type = string }

resource "random_id" "suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "blob" {
  bucket = "qontinui-${var.environment}-blob-${random_id.suffix.hex}"

  tags = { Name = "qontinui-${var.environment}-blob" }
}

resource "aws_s3_bucket_public_access_block" "blob" {
  bucket = aws_s3_bucket.blob.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "blob" {
  bucket = aws_s3_bucket.blob.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "blob" {
  bucket = aws_s3_bucket.blob.id

  versioning_configuration {
    status = "Suspended" # staging — flip to Enabled in prod
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "blob" {
  bucket = aws_s3_bucket.blob.id

  rule {
    id     = "expire-screenshots-after-90d"
    status = "Enabled"

    filter {
      prefix = "screenshots/"
    }

    expiration {
      days = 90
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

output "bucket_name" { value = aws_s3_bucket.blob.bucket }
output "bucket_arn"  { value = aws_s3_bucket.blob.arn }
