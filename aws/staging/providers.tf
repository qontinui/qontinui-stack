terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    # Used by modules/cross-idp-linking to zip the PreSignUp Lambda source.
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

# One tag set for every provider that CREATES resources, so the second-region
# backup vault is tagged exactly like everything in the primary region.
locals {
  default_tags = {
    Project     = "qontinui"
    Environment = "staging"
    ManagedBy   = "terraform"
    Stack       = "qontinui-stack"
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.default_tags
  }
}

# The second region for the off-instance RDS backup copies (modules/backup,
# plan 2026-10-14-coord-database-is-one-command-from-total-loss). Unlike the
# read-only `aws.ssm` alias in main.tf, this one creates a resource (the copy
# vault), so it carries the default tags too.
provider "aws" {
  alias  = "dr"
  region = var.postgres_offinstance_backup_dr_region

  default_tags {
    tags = local.default_tags
  }
}
