output "id" {
  description = "Security group resource ID."
  value       = fptcloud_security_group.this.id
}

output "name" {
  description = "Security group name."
  value       = fptcloud_security_group.this.name
}

output "rule_ids" {
  description = "Map of rule index → rule resource ID."
  value       = { for k, r in fptcloud_security_group_rule.rules : k => r.id }
}
