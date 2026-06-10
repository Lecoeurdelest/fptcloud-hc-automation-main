terraform {
  required_version = ">= 1.6"
  required_providers {
    fptcloud = {
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }
  }
}
