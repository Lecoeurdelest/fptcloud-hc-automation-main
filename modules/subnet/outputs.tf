output "id" {
  description = "Subnet resource ID."
  value       = fptcloud_subnet.this.id
}

output "cidr" {
  description = "CIDR block assigned to the subnet."
  value       = fptcloud_subnet.this.cidr
}

output "name" {
  description = "Subnet name."
  value       = fptcloud_subnet.this.name
}

output "vpc_id" {
  description = "Parent VPC ID."
  value       = fptcloud_subnet.this.vpc_id
}
