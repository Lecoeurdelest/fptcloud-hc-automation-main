resource "fptcloud_instance" "this" {
  vpc_id            = var.vpc_id
  name              = var.name
  image_name        = var.image_name
  flavor_name       = var.flavor_name
  storage_policy_id = var.storage_policy_id
  storage_size_gb   = var.disk_gb
  subnet_id         = var.subnet_id
  status            = var.status
  ssh_key           = var.ssh_key
  password          = var.password

  security_group_ids = var.security_group_ids
}
