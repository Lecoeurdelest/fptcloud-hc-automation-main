output "id" {
  description = "Volume resource ID."
  value       = fptcloud_storage.this.id
}

output "size_gb" {
  description = "Volume size in gigabytes."
  value       = fptcloud_storage.this.size_gb
}

output "attached_instance_id" {
  description = "Instance ID this volume is attached to (null if unattached)."
  value       = fptcloud_storage.this.instance_id
}
