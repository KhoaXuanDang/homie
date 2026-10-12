# dev-note: local state, one owner runs apply; move to an S3 backend before a second person applies
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "account_id" {
  description = "AWS account ID for the shared Homie dev stack."
  type        = string
  default     = "474668400409"

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

variable "env" {
  type    = string
  default = "dev"
}

variable "dashboard_urls" {
  description = "Origins allowed to sign in and upload (no trailing slash)."
  type        = list(string)
  default     = ["http://localhost:3000"]
}

variable "alexa_redirect_uris" {
  description = "Account-linking redirect URIs shown by the alexa-ai tooling. The alexa-link client is created once this is set."
  type        = list(string)
  default     = []
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = { project = "homie", env = var.env }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name = "homie-${var.env}"
}
