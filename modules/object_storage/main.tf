resource "fptcloud_object_storage_bucket" "this" {
  vpc_id      = var.vpc_id
  name        = var.bucket_name
  region_name = var.region_name

  acl        = var.acl
  versioning = var.versioning
}
