variable "aws_region" {
  description = "AWS region for the library API and its supporting resources."
  type        = string
}

variable "archive_bucket_region" {
  description = "Region where the existing archive bucket is located."
  type        = string
}

variable "aws_profile" {
  description = "Optional local AWS CLI profile. Omit to use the standard AWS credential chain."
  type        = string
  default     = ""
}

variable "archive_bucket_name" {
  description = "Existing private archive bucket. Terraform reads its name but does not manage it."
  type        = string
}

variable "archive_prefix" {
  description = "Optional key prefix configured for the existing archive."
  type        = string
  default     = ""

  validation {
    condition = var.archive_prefix == "" || (
      var.archive_prefix == trim(var.archive_prefix, "/") &&
      alltrue([for part in split("/", var.archive_prefix) : part != "" && part != "." && part != ".."])
    )
    error_message = "archive_prefix must contain nonempty path components without . or ..."
  }
}

variable "cognito_domain_prefix" {
  description = "Globally unique prefix for the Cognito managed sign-in domain."
  type        = string
}

variable "project_name" {
  description = "Short lowercase name used to name presentation resources."
  type        = string
  default     = "family-archive-library"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.project_name))
    error_message = "project_name must be 3-25 lowercase letters, digits, or hyphens and start with a letter."
  }
}
