# Remote state in S3 with DynamoDB lock. Bucket + table must exist before
# `terraform init` (created out-of-band — chicken-and-egg of bootstrapping
# state storage from a config that needs state storage). One-time setup
# script: `qontinui-stack/aws/scripts/bootstrap-tf-backend.sh`.

terraform {
  backend "s3" {
    bucket         = "qontinui-tfstate"
    key            = "qontinui-stack/staging/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "qontinui-tfstate-lock"
    encrypt        = true
  }
}
