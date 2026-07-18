# Remote state in S3 with DynamoDB lock. Bucket + table must exist before
# `terraform init` (created out-of-band — chicken-and-egg of bootstrapping
# state storage from a config that needs state storage). One-time setup
# script: `qontinui-stack/aws/scripts/bootstrap-tf-backend.sh`.
#
# NOTE: the state bucket (qontinui-tfstate) + lock table stay in us-east-1
# BY DESIGN — the backend region is independent of the region the resources
# live in. This eu-central-1 env provisions resources in eu-central-1 (via
# var.region → provider) but stores its state alongside the us-east-1 env in
# the same shared us-east-1 bucket, distinguished only by the `key` below.

terraform {
  backend "s3" {
    bucket         = "qontinui-tfstate"
    key            = "qontinui-stack/eu-central-1/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "qontinui-tfstate-lock"
    encrypt        = true
  }
}
