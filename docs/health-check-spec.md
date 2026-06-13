# Daily Health-Check Specification

This document defines the source-of-truth behavior for the FPT Cloud daily
health-check automation. Implementation must follow `specs/health-check.json`.
Behavior changes start in the spec before code changes.

## Execution Rules

- Only stages present in `specs/health-check.json` may run.
- Only stages with `automation_status: automated` may run automatically.
- A stage must also have `safe_for_daily_run: true`.
- Missing required inputs are skipped with a clear reason.
- Failed dependency stages block dependent stages; blocked stages are skipped,
  not marked as root failures.
- `HC_VPC_ID` is the explicit VPC ID override. If it is empty,
  `compute.discover-vpc` must use the official `data.fptcloud_vpc` data source
  with the configured VPC name/provider lookup key and write
  `data.fptcloud_vpc.this.id` into the run context.
- Dependent stages must use the run context's effective VPC ID. If no effective
  ID can be resolved, dependent stages are skipped with a clear reason.
- Diagnostics must print `vpc_name`, `explicit_vpc_id`, `discovered_vpc_id`,
  `effective_vpc_id`, and `vpc_id_source`.
- No real resources may be created unless cleanup behavior is defined.
- Terraform stdout and stderr must be preserved per stage.
- Cleanup must be idempotent.

## Stage Catalog

| Stage ID | Manual check item | Status | Safe daily | Required inputs | Dependencies | Cleanup |
|---|---|---:|---:|---|---|---|
| `general.portal-login` | Login web portal | manual_only | false | None | None | None |
| `general.portal-navigation` | Navigate portal tabs/dashboard/Compute Engine | manual_only | false | None | None | None |
| `general.hotline` | Test FPT Cloud hotline / Call hotline 1900 638 399 | manual_only | false | None | None | None |
| `compute.discover-vpc` | Resolve VPC ID from configured VPC name or provider lookup key | automated | true | `FPTCLOUD_TOKEN`, `FPTCLOUD_REGION`, `FPTCLOUD_TENANT_NAME` | None | No resources created |
| `compute.discover-storage-policy` | Resolve storage policy required for VM/disk checks | automated | true | `FPTCLOUD_TOKEN`, `FPTCLOUD_REGION`, `FPTCLOUD_TENANT_NAME` | `compute.discover-vpc` | No resources created |
| `compute.discover-subnet` | Resolve existing subnet required for VM/security group checks | automated | true | `FPTCLOUD_TOKEN`, `FPTCLOUD_REGION`, `FPTCLOUD_TENANT_NAME` | `compute.discover-vpc` | No resources created |
| `compute.validate-subnet-inputs` | Validate subnet creation inputs before provider apply | automated | true | `FPTCLOUD_REGION`, `FPTCLOUD_TENANT_NAME`, `HC_SUBNET_CIDR`, `HC_SUBNET_GATEWAY` | `compute.discover-vpc` | No resources created |
| `compute.create-subnet` | Create subnet for VM | automated | true | `FPTCLOUD_TOKEN`, `FPTCLOUD_REGION`, `FPTCLOUD_TENANT_NAME`, `HC_SUBNET_CIDR`, `HC_SUBNET_GATEWAY` | `compute.discover-vpc`, `compute.validate-subnet-inputs` | Terraform destroy |
| `compute.collect-subnet-create-evidence` | Collect subnet create evidence package for provider/API support | automated | true | `FPTCLOUD_REGION`, `FPTCLOUD_TENANT_NAME`, `HC_SUBNET_CIDR`, `HC_SUBNET_GATEWAY` | `compute.discover-vpc`, `compute.validate-subnet-inputs` | No resources created |
| `compute.vm-windows-2012` | Create Windows VM 2012 | blocked | false | `HC_WINDOWS_2012_IMAGE_NAME`, `HC_FLAVOR_NAME`, `HC_SSH_KEY`, `HC_SUBNET_ID`, `HC_STORAGE_POLICY_ID` | `compute.discover-subnet`, `compute.discover-storage-policy` | Destroy VM when implemented |
| `compute.vm-windows-2016` | Create Windows VM 2016 | blocked | false | `HC_WINDOWS_2016_IMAGE_NAME`, `HC_FLAVOR_NAME`, `HC_SSH_KEY`, `HC_SUBNET_ID`, `HC_STORAGE_POLICY_ID` | `compute.discover-subnet`, `compute.discover-storage-policy` | Destroy VM when implemented |
| `compute.vm-windows-2019` | Create Windows VM 2019 | blocked | false | `HC_WINDOWS_2019_IMAGE_NAME`, `HC_FLAVOR_NAME`, `HC_SSH_KEY`, `HC_SUBNET_ID`, `HC_STORAGE_POLICY_ID` | `compute.discover-subnet`, `compute.discover-storage-policy` | Destroy VM when implemented |
| `compute.vm-windows-2022` | Create Windows VM 2022 | blocked | false | `HC_WINDOWS_2022_IMAGE_NAME`, `HC_FLAVOR_NAME`, `HC_SSH_KEY`, `HC_SUBNET_ID`, `HC_STORAGE_POLICY_ID` | `compute.discover-subnet`, `compute.discover-storage-policy` | Destroy VM when implemented |
| `compute.vm-ubuntu-16.04` | Create Ubuntu VM 16.04 | blocked | false | `HC_UBUNTU_1604_IMAGE_NAME`, `HC_FLAVOR_NAME`, `HC_SSH_KEY`, `HC_SUBNET_ID`, `HC_STORAGE_POLICY_ID` | `compute.discover-subnet`, `compute.discover-storage-policy` | Destroy VM when implemented |
| `compute.vm-ubuntu-18.04` | Create Ubuntu VM 18.04 | blocked | false | `HC_UBUNTU_1804_IMAGE_NAME`, `HC_FLAVOR_NAME`, `HC_SSH_KEY`, `HC_SUBNET_ID`, `HC_STORAGE_POLICY_ID` | `compute.discover-subnet`, `compute.discover-storage-policy` | Destroy VM when implemented |
| `compute.vm-ubuntu-20.04` | Create Ubuntu VM 20.04 | partially_automated | false | `HC_UBUNTU_2004_IMAGE_NAME`, `HC_FLAVOR_NAME`, `HC_SSH_KEY`, `HC_SUBNET_ID`, `HC_STORAGE_POLICY_ID` | `compute.discover-subnet`, `compute.discover-storage-policy` | Destroy VM |
| `compute.vm-ubuntu-22.04` | Create Ubuntu VM 22.04 | partially_automated | false | `HC_UBUNTU_2204_IMAGE_NAME`, `HC_FLAVOR_NAME`, `HC_SSH_KEY`, `HC_SUBNET_ID`, `HC_STORAGE_POLICY_ID` | `compute.discover-subnet`, `compute.discover-storage-policy` | Destroy VM |
| `compute.resize-vm` | Resize VM from 2vCPU/2GB to 4vCPU/4GB | partially_automated | false | `HC_IMAGE_NAME`, `HC_FLAVOR_NAME`, `HC_UPSIZE_FLAVOR_NAME`, `HC_SSH_KEY`, `HC_SUBNET_ID`, `HC_STORAGE_POLICY_ID` | `compute.discover-subnet`, `compute.discover-storage-policy` | Destroy VM |
| `compute.resize-os-disk` | Resize OS disk from 40GB to 80GB | unsupported | false | None | None | Manual |
| `compute.add-disk` | Add 40GB disk and attach to VM | partially_automated | true | `HC_VPC_ID`, `HC_STORAGE_POLICY_ID` | `compute.discover-vpc`, `compute.discover-storage-policy` | Terraform destroy |
| `compute.delete-vm-retain-disk` | Delete VM and verify attached disk is retained | unsupported | false | None | None | Manual |
| `compute.schedule-power` | Schedule VM power on/off | unsupported | false | None | None | Manual |
| `compute.snapshot-create` | Create VM snapshot | unsupported | false | None | None | Manual |
| `compute.snapshot-revert` | Revert VM snapshot | unsupported | false | None | `compute.snapshot-create` | Manual |
| `network.assign-public-ip` | Assign public IP to VM | unsupported | false | None | None | Manual |
| `network.security-group` | Create security group allowing only RDP 3389 and SSH 22 | automated | true | `HC_SUBNET_ID` | `compute.discover-vpc`, `compute.discover-subnet` | Terraform destroy |
| `network.blocked-ports` | Verify blocked ports cannot connect | manual_only | false | Reachable VM network path | `network.security-group` | None |
| `network.outbound-http-https` | Add outbound rules for HTTP/HTTPS 80/443 | blocked | false | `HC_VPC_ID`, `HC_SUBNET_ID` | `network.security-group` | Destroy security group |
| `network.additional-subnet` | Create additional subnet 10.136.10.0/24 | automated | true | None | `compute.discover-vpc` | Terraform destroy |
| `network.additional-nic` | Add additional NIC to VM and verify inside OS | unsupported | false | None | `network.additional-subnet` | Manual |
| `backup.vm-backup-restore` | Create file, backup, restore, verify file | unsupported | false | None | None | Manual |
| `object-storage.bucket` | Create bucket, upload file, create folder, open file, connect S3 endpoint, delete file, delete bucket | partially_automated | true | `HC_ENABLED_OBJECT_REGIONS` | None | Terraform destroy |
| `ticket.support-portal` | Create/update/comment support ticket | manual_only | false | None | None | Manual ticket closure |
| `ticket.zalo` | Request support from Zalo OA using /support | manual_only | false | None | None | None |
| `ticket.email` | Create ticket by sending email to support@fptcloud.com | manual_only | false | None | None | Manual ticket closure |

## Current Provider/API Risks

Current diagnostics show provider `registry.terraform.io/fpt-corp/fptcloud`
version `0.3.50`, region `VN/HAN`, tenant `FCI-L1-ORG`, and VPC value
`FCI-L1-HAN-VPC`. The repository examples and subnet module describe `vpc_id`
as a UUID, so this value appears to be a display name being passed where the
provider may expect an internal ID.

The remaining root failures are:

- `data.fptcloud_storage_policy.this`: `404 NOT FOUND`, classified as
  `provider_endpoint_or_datasource_mismatch`.
- `data.fptcloud_subnet.this`: `UnknownError: System error`, classified as
  `provider_or_backend_system_error`.
- `module.this.fptcloud_subnet.this`: `UnknownError: System error`. After
  `compute.validate-subnet-inputs` passes local checks, classify this as
  `provider_or_backend_system_error_after_valid_inputs`.

These require either explicit internal IDs or FPT Cloud provider/API support.

## Subnet Creation Contract

Local provider schema inspection for `fptcloud_subnet` shows the resource
requires `vpc_id`, `name`, `cidr`, `gateway_ip`, and `type`. Optional fields
include DNS IPs, static IP pool, and tags. The provider describes `type` values
`NAT_ROUTED` and `ISOLATED`.

`compute.validate-subnet-inputs` validates only what can be proven locally:
CIDR syntax, gateway IP syntax, gateway membership inside the CIDR, gateway not
being the network or broadcast address, VPC identifier shape, region/tenant
presence, and the exact Terraform variables passed to `module.subnet`.

The following cannot be proven without a working FPT Cloud API response or
support confirmation: whether the subnet CIDR is inside the VPC CIDR unless
`HC_VPC_CIDR` is configured, whether it overlaps existing subnets, whether
`FPTCLOUD_REGION` matches the VPC region, whether `FPTCLOUD_TENANT_NAME` matches
the VPC tenant, and whether the provider expects the VPC UUID, VPC IaaS ID,
cloud ID, or another network identifier.
