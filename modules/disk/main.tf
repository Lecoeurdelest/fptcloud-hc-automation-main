resource "fptcloud_storage" "this" {
  vpc_id            = var.vpc_id
  name              = var.name
  size_gb           = var.size_gb
  storage_policy_id = var.storage_policy_id
  type              = var.type
  instance_id       = var.instance_id
}
