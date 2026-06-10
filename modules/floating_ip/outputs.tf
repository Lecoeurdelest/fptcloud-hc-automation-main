output "id" {
  description = "Floating IP resource ID."
  value       = fptcloud_floating_ip.this.id
}

output "ip_address" {
  description = "The allocated public IP address."
  value       = fptcloud_floating_ip.this.ip_address
}

output "instance_id" {
  description = "Instance this floating IP is bound to."
  value       = var.instance_id
}
