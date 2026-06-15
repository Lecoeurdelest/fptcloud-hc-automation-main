# FPT Cloud Quota Source Investigation

**Date:** 2026-06-15
**Provider:** `registry.terraform.io/fpt-corp/fptcloud` v0.3.50 (`terraform-provider-fptcloud_v0.3.50.exe`, windows_amd64)
**Trigger:** Backend rejects instance creation with
`HttpError {"status":false,"message":["Instance storage exceeds VPC quota. Please check again!"]}`
while Terraform `plan` succeeds and the provider accepts the request.
**Scope:** Read-only investigation. No implementation changed, no `terraform apply` run.

## Method / Evidence sources

Two independent, authoritative sources were used — they agree:

1. **Provider binary string extraction.** Printable string literals were extracted
   from the 19 MB Go binary
   (`modules/vm/.terraform/providers/.../terraform-provider-fptcloud_v0.3.50.exe`).
   This yields every embedded REST endpoint template, JSON struct tag, and schema
   description compiled into the provider.
2. **Provider schema dump.** `terraform -chdir=modules/vm providers schema -json`
   (read-only; no cloud/API call) — the same call the health-check code makes in
   `inspect_provider_quota_capabilities()`. Covers **31 resources + 45 data sources**.

---

## 1. Quota APIs FOUND

| Capability | Endpoint (from binary) | Relevance to VPC storage quota |
|---|---|---|
| **FKE (managed Kubernetes) quota check** | `/v1/xplat/fke/vpc/%s/m-fke/%s/check-quota-resources` | **None.** Scoped to managed-Kubernetes cluster provisioning only. Not wired to any compute/instance/volume resource or data source. |
| Object-storage (S3) quotas | JSON fields `migrate_quota`, `sync_quota`, `rgw_total_nodes` on the S3 service struct | **None.** Object-storage service sizing, not VPC block-storage. |
| Load-balancer connection cap | `connection_limit` attribute on `fptcloud_load_balancer_v2_listener` | **None.** Connection concurrency, not storage. |

**The single endpoint whose path literally contains `quota` is the FKE one.** There is
no compute, instance, VPC, or volume storage-quota endpoint anywhere in the binary.

## 2. Quota APIs MISSING (searched, not present)

No endpoint, struct, or schema attribute exists for any of the following:

- Tenant storage quota / tenant capacity
- **VPC storage quota / VPC capacity** ← this is the limit the backend enforces
- Instance (compute) quota — count, vCPU, or RAM ceilings
- Volume / block-storage quota
- Storage-policy capacity, allocated, or remaining size
- Used storage / consumed storage (aggregate)
- Remaining / available storage

Endpoints that *do* exist for the relevant objects return identity/config only:

| Object | Endpoint(s) | Fields returned (no capacity) |
|---|---|---|
| Storage policy | `/v1/internal/vpc/%s/find_storage_policy`, `/v2/vpc/%s/storage-policies` | `id`, `name` only |
| VPC | `/v1/vmware/org/%s/user/%s/list/vpc?regionId=%s`, `/v2/org/%s/vpc` | `id`, `name`, `status` |
| Instance | `/v2/vpc/%s/instance` (create), Find (single), `change-status`, `rename`, `reconfigure-vm`, `tags` | per-instance `storage_size_gb` only |
| Storage (volume) | `/v2/vpc/%s/storage`, `/v2/vpc/%s/storage/%s/update-attached`, `/tags` | per-volume `size_gb` only |

## 3. Can used / remaining / limit storage be calculated from the provider? — NO

Provider schema (`terraform providers schema -json`), authoritative attribute lists:

```
fptcloud_storage_policy : id, storage_policies[ {id, name} ], vpc_id
fptcloud_vpc            : id, name, status
fptcloud_instance       : cpu_number, created_at, flavor_name, guest_os, host_name,
                          id, instance_group_id, memory_mb, name, private_ip,
                          public_ip, security_group_ids, status, storage_policy,
                          storage_size_gb, subnet_id, tag_ids, vpc_id
fptcloud_storage        : created_at, id, instance_id, name, size_gb,
                          storage_policy, storage_policy_id, tag_ids, type, vpc_id
fptcloud_flavor         : flavors, id, vpc_id
```

Across **all 31 resources + 45 data sources**, the only attribute matching
`quota|capacity|usage|limit|used|remaining|available|total` is
`connection_limit` (load-balancer listener) — irrelevant.

Why the four sub-values cannot be derived:

- **VPC storage limit** — not exposed. `fptcloud_vpc` returns only `id`, `name`,
  `status`. The VPC/Tenant raw API responses contain no quota field either.
- **Storage-policy limit** — not exposed. The nested `storage_policies` object is
  typed `["list", ["object", {"id":"string","name":"string"}]]`. No size field.
- **Used storage** — not derivable. `fptcloud_instance` (`storage_size_gb`) and
  `fptcloud_storage` (`size_gb`) expose per-record sizes, **but both are
  single-record lookups (Find by id/name)**. The provider exposes **no
  list/enumerate data source** for instances or volumes, so the per-record sizes
  cannot be summed into a tenant/VPC total.
- **Remaining storage** — impossible: it requires both a limit and a used total,
  neither of which is available.

## 4. Underlying REST APIs (used internally by the provider)

- Base host: `console-api.fptcloud.com/api`.
- The compute-quota rule is enforced **server-side only**, inside the instance-create
  handler. The failing call is `POST /v2/vpc/{vpc_id}/instance`; the backend computes
  "instance storage vs VPC quota" itself and returns the rejection message. No
  companion **read** endpoint exposes the limit or the current usage that this check
  evaluates. Confirmed by the run log: the create request reaches the backend
  (plan + provider accept it) and is rejected only at apply.

## 5. Exact reason `quota_status=not_available` appears in the logs

Produced by `scripts/run_health_checks.py`, deterministically:

1. `inspect_provider_quota_capabilities()` (lines ~2256-2287) runs
   `terraform providers schema -json` and scans every resource/data-source attribute
   for the substring `"quota"`. **Zero provider attributes contain `quota`**, so
   `provider_schema_quota_fields = []`. It also marks
   `provider_instance_inventory` / `provider_storage_inventory = "single_lookup_only"`
   because `fptcloud_instance` / `fptcloud_storage` exist but only as single-record
   data sources (cannot enumerate to aggregate).
2. With no provider-derived quota and no operator override, the run falls back to
   `not_available_quota_report()` (lines ~2148-2180), which **hard-codes**
   `quota_source = "unsupported_or_not_found"` and `quota_status = "not_available"`,
   and sets every quota/used/remaining field to `"not_available"`.
3. The only path that would flip this to `quota_status=available` is
   `apply_quota_export()` (lines ~2221-2253): if an operator supplies
   `HC_QUOTA_EXPORT_JSON` with externally-sourced numbers, `quota_source` becomes
   `HC_QUOTA_EXPORT_JSON` and `quota_status` becomes `available`. That env var is not
   set, so the fallback stands.

**In one sentence:** `quota_status=not_available` is correct and expected — the
fpt-corp/fptcloud v0.3.50 provider publishes no quota/capacity/usage attribute or
read endpoint for compute/VPC/volume storage, so the health-check's schema-driven
detector finds nothing and emits the hard-coded `not_available` / `unsupported_or_not_found`
fallback. The VPC storage quota that actually blocked the apply lives only in the
backend and is enforced at `POST /v2/vpc/{vpc_id}/instance`.

## 6. Provider limitations (summary)

- No compute/VPC/volume quota API — read or check (FKE-only `check-quota-resources`).
- No capacity/used/remaining/limit attribute on any compute, VPC, storage-policy, or
  volume object.
- No list/enumerate data source for instances or volumes → cannot aggregate usage
  client-side.
- Quota enforcement is server-side and **fail-at-apply only**; there is no
  pre-flight signal a Terraform/health-check workflow can read.
- Consequence: a preventive (pre-apply) storage-quota gate is **not implementable**
  with this provider. The only options are (a) treat the backend's apply-time
  rejection as authoritative (current behaviour:
  `preflight_decision=allow_apply_provider_authoritative`), or (b) feed external
  quota numbers via `HC_QUOTA_EXPORT_JSON`.

## Appendix — reproduction commands (read-only)

```bash
# Endpoint templates compiled into the provider
grep -aoE '/v[12]/[a-zA-Z0-9_%./?=&{}-]+' <extracted-strings> | sort -u

# Authoritative schema (no cloud call)
terraform -chdir=modules/vm providers schema -json > schema.json
#   -> only quota-ish attribute across 31 res + 45 ds: load_balancer connection_limit
#   -> fptcloud_storage_policy.storage_policies typed ["list",["object",{id,name}]]
```
