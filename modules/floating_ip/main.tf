resource "fptcloud_floating_ip" "this" {
  vpc_id = var.vpc_id
}

resource "fptcloud_floating_ip_association" "this" {
  count = var.instance_id == null ? 0 : 1

  vpc_id         = var.vpc_id
  floating_ip_id = fptcloud_floating_ip.this.id
  instance_id    = var.instance_id
}
