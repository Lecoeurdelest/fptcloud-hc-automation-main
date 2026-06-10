output "id" {
  description = "Bucket resource ID."
  value       = fptcloud_object_storage_bucket.this.id
}

output "bucket_name" {
  description = "Bucket name."
  value       = fptcloud_object_storage_bucket.this.name
}

output "region_name" {
  description = "Region the bucket was created in."
  value       = fptcloud_object_storage_bucket.this.region_name
}
