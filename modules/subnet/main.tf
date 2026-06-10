resource "fptcloud_subnet" "this" {
  vpc_id     = var.vpc_id
  name       = var.name
  cidr       = var.cidr
  gateway_ip = var.gateway_ip
  type       = var.type
}
