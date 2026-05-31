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

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "qontinui"
      Environment = "staging"
      ManagedBy   = "terraform"
      Stack       = "qontinui-stack"
    }
  }
}
