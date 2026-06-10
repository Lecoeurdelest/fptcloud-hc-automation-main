variable "name" {
  description = "Subnet name (must be unique within the VPC)."
  type        = string
}

variable "cidr" {
  description = "CIDR block for the subnet (e.g. 172.26.221.0/24)."
  type        = string
  validation {
    condition     = can(cidrnetmask(var.cidr))
    error_message = "The cidr variable must be a valid CIDR block (e.g. 172.26.221.0/24)."
  }
}

variable "gateway_ip" {
  description = "Gateway IP address for the subnet."
  type        = string
}

variable "type" {
  description = "Subnet type required by the FPT Cloud provider."
  type        = string
  default     = "NAT_ROUTED"
}

variable "vpc_id" {
  description = "VPC UUID that the subnet belongs to."
  type        = string
}
