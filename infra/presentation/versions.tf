terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile == "" ? null : var.aws_profile

  default_tags {
    tags = {
      Application = "family-archive-library"
      ManagedBy   = "Terraform"
    }
  }
}

provider "aws" {
  alias   = "archive"
  region  = var.archive_bucket_region
  profile = var.aws_profile == "" ? null : var.aws_profile

  default_tags {
    tags = {
      Application = "family-archive-library"
      ManagedBy   = "Terraform"
    }
  }
}
