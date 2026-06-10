variable "vpc_id" {
  description = "VPC UUID."
  type        = string
}

variable "instance_id" {
  description = "Instance ID to associate the floating IP with."
  type        = string
  default     = null
}
