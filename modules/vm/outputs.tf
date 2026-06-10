output "id" {
  description = "Instance resource ID."
  value       = fptcloud_instance.this.id
}

output "name" {
  description = "Instance name."
  value       = fptcloud_instance.this.name
}

output "private_ip" {
  description = "Primary private IP address."
  value       = fptcloud_instance.this.private_ip
}

output "public_ip" {
  description = "Public IP address (null if none assigned at create time)."
  value       = try(fptcloud_instance.this.public_ip, null)
}

output "power_state" {
  description = "Current power state of the instance (e.g. 'running', 'stopped')."
  value       = fptcloud_instance.this.status
}

output "status" {
  description = "Provisioning status."
  value       = fptcloud_instance.this.status
}
