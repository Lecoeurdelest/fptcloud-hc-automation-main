resource "fptcloud_security_group" "this" {
  vpc_id   = var.vpc_id
  name     = var.name
  type     = var.type
  apply_to = var.apply_to
}

resource "fptcloud_security_group_rule" "rules" {
  for_each = { for idx, rule in var.rules : tostring(idx) => rule }

  vpc_id            = var.vpc_id
  security_group_id = fptcloud_security_group.this.id
  direction         = each.value.direction
  protocol          = each.value.protocol
  port_range        = each.value.port_range
  sources           = each.value.sources
  action            = each.value.action
  description       = each.value.description
}
