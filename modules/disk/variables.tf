variable "name" {
  description = "Volume display name."
  type        = string
}

variable "size_gb" {
  description = "Volume size in gigabytes."
  type        = number
  validation {
    condition     = var.size_gb >= 1
    error_message = "size_gb must be at least 1."
  }
}

variable "vpc_id" {
  description = "VPC UUID."
  type        = string
}

variable "storage_policy_id" {
  description = "Storage policy ID for the volume."
  type        = string
}

variable "type" {
  description = "Storage type required by the FPT Cloud provider."
  type        = string
  default     = "HDD"
}

variable "instance_id" {
  description = "Instance ID to attach the volume to (optional — omit to create unattached)."
  type        = string
  default     = null
}
