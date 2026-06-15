variable "name" {
  description = "VM display name."
  type        = string
}

variable "image_name" {
  description = "OS image name (e.g. 'windows-2012', 'ubuntu-20.04')."
  type        = string
}

variable "cpu" {
  description = "Number of vCPUs."
  type        = number
  default     = 2
  validation {
    condition     = var.cpu >= 1 && var.cpu <= 64
    error_message = "cpu must be between 1 and 64."
  }
}

variable "ram_gb" {
  description = "RAM in gigabytes."
  type        = number
  default     = 2
  validation {
    condition     = var.ram_gb >= 1
    error_message = "ram_gb must be at least 1."
  }
}

variable "disk_gb" {
  description = "Root (OS) disk size in gigabytes."
  type        = number
  default     = 40
  validation {
    condition     = var.disk_gb >= 10
    error_message = "disk_gb must be at least 10."
  }
}

variable "flavor_name" {
  description = "Flavor name for the instance."
  type        = string
}

variable "storage_policy_id" {
  description = "Storage policy ID for the instance root disk."
  type        = string
}

variable "ssh_key" {
  description = "SSH key name or ID used to provision the instance."
  type        = string
  default     = null
}

variable "password" {
  description = "Password used to provision the instance when SSH key auth is not used."
  type        = string
  default     = null
  sensitive   = true
}

variable "subnet_id" {
  description = "Subnet ID to attach the primary NIC."
  type        = string
}

variable "vpc_id" {
  description = "VPC UUID."
  type        = string
}

variable "status" {
  description = "Desired VM power status."
  type        = string
  default     = "POWERED_ON"
}

variable "security_group_ids" {
  description = "Security group IDs to associate with the VM."
  type        = set(string)
  default     = []
}

variable "tags" {
  description = "Key-value tags to apply to the instance via fptcloud_tagging resources."
  type        = map(string)
  default     = {}
}
