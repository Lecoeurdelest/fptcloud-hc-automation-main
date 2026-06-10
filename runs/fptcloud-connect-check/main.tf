terraform {
  required_version = ">= 1.6"
  required_providers {
    fptcloud = {
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }
  }
}

provider "fptcloud" {
  api_endpoint = var.api_endpoint
  region       = var.region
  tenant_name  = var.tenant_name
}

variable "api_endpoint" {
  type    = string
  default = "https://console-api.fptcloud.com/api"
}

variable "region" {
  type = string
}

variable "tenant_name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_name" {
  type    = string
  default = null
}

variable "name" {
  type = string
}

variable "cidr" {
  type = string
}

variable "gateway_ip" {
  type = string
}

variable "type" {
  type    = string
  default = "NAT_ROUTED"
}

data "fptcloud_vpc" "selected" {
  name = coalesce(var.vpc_name, var.vpc_id)
}

module "connect_check" {
  source = "../../modules/subnet"

  vpc_id     = var.vpc_id
  name       = var.name
  cidr       = var.cidr
  gateway_ip = var.gateway_ip
  type       = var.type
}

output "connect_check_subnet_id" {
  value = module.connect_check.id
}

output "selected_vpc_id" {
  value = data.fptcloud_vpc.selected.id
}

output "selected_vpc_status" {
  value = data.fptcloud_vpc.selected.status
}
