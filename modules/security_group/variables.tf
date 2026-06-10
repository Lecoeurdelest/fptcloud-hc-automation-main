variable "name" {
  description = "Security group name."
  type        = string
}

variable "vpc_id" {
  description = "VPC UUID."
  type        = string
}

variable "type" {
  description = "Security group type: ACL or DFW."
  type        = string
  default     = "ACL"
}

variable "apply_to" {
  description = "List of IPs/resources the security group applies to."
  type        = list(string)
  default     = []
}

variable "rules" {
  description = <<-EOT
    List of security group rule objects. Each object must contain:
      direction        - "ingress" or "egress"
      protocol         - "tcp", "udp", "icmp", or "-1" (all)
      port_range       - port range string (e.g. "80" or "80-443")
      sources          - CIDR/source string (e.g. "0.0.0.0/0")
      action           - provider action, commonly "ALLOW" or "DENY"
  EOT
  type = list(object({
    direction   = string
    protocol    = string
    port_range  = string
    sources     = string
    action      = string
    description = optional(string)
  }))
  default = []
}
