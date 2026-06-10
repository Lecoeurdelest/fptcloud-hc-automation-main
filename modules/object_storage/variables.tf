variable "bucket_name" {
  description = "Object storage bucket name (globally unique)."
  type        = string
  validation {
    condition     = length(var.bucket_name) >= 3 && length(var.bucket_name) <= 63
    error_message = "bucket_name must be between 3 and 63 characters."
  }
}

variable "region_name" {
  description = "Object storage region name, e.g. HCM-01, HCM-02, HN-01, or HN-02."
  type        = string
}

variable "vpc_id" {
  description = "VPC UUID (required by FPT Cloud object storage)."
  type        = string
}

variable "acl" {
  description = "Bucket ACL."
  type        = string
  default     = null
}

variable "versioning" {
  description = "Bucket versioning status."
  type        = string
  default     = null
}
