# fptcloud-hc-automation

This README is **non-authoritative** operational guidance. If anything here
conflicts with the specs, the files under `specs/` win, especially
`specs/health-check.json`, `specs/03-TASKS.md`, `specs/04-TESTS.md`, and
`specs/05-SPEC-GOVERNANCE.md`.

This project runs FPT Cloud tenant health checks from the spec catalog. The
live runner loads `.env`, reads per-phase runtime settings from
`healthcheck.toml`, assembles runnable stages from `specs/health-check.json`,
uses Terraform modules when a stage creates cloud resources, runs API/S3 probes
for validation, and writes results to `runs/<run_id>/log.json` and `log.html`.

## Requirements

- Python `>=3.11,<3.12`, as declared in `pyproject.toml`.
- Terraform CLI. The live `scripts/run_health_checks.py` path currently uses
  modules under `modules/` and the `fpt-corp/fptcloud` provider.
- Redis/Postgres are only required for the queue worker/producer CLI path; they
  are not required for the live health-check runner.
- Valid FPT Cloud credentials in `.env`.
- S3 credentials when running object-storage checks.

## Quick Start

```powershell
py -3.11 -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Fill in `.env`, then inspect the resolved inputs:

```powershell
$env:PYTHONPATH = "src;scripts"
python scripts\diagnose_health_inputs.py --json
```

Run one stage:

```powershell
$env:PYTHONPATH = "src;scripts"
python scripts\run_health_checks.py --stage object-storage.bucket
```

Run every automated stage allowed by the spec:

```powershell
$env:PYTHONPATH = "src;scripts"
python scripts\run_health_checks.py
```

View a run report:

```powershell
python scripts\run_health_checks.py --view runs\<run_id>\log.json --filter summary
python scripts\run_health_checks.py --view runs\<run_id>\log.json --filter failed
```

The latest run artifacts are written to:

- `runs/<run_id>/log.json`
- `runs/<run_id>/error_queue.json`
- `log.html`

## `.env` Configuration

Copy `.env.example` to `.env`. Never commit real `.env` files.

Required provider settings:

| Variable | Description |
|---|---|
| `FPTCLOUD_TOKEN` | Token used by the FPT Cloud provider/API |
| `FPTCLOUD_API_URL` | API base URL, usually `https://api.fptcloud.com` |
| `FPTCLOUD_REGION` | Provider region, for example `VN/HAN` |
| `FPTCLOUD_TENANT_NAME` | Tenant name |

VPC settings:

| Variable | Description |
|---|---|
| `HC_VPC_NAME` | Recommended lookup value; the runner resolves it to the provider VPC ID |
| `HC_VPC_ID` | Set only when you already know the real provider UUID/ID |
| `VPC_NAME`, `VPC_ID`, `VPC_IDS` | Legacy lookup fallbacks; prefer `[targets].vpcs` in `healthcheck.toml` for the ordered target list |

Common compute/network settings:

| Variable | Description |
|---|---|
| `HC_STORAGE_POLICY_ID` | Explicit storage policy, when discovery should be bypassed |
| `HC_SUBNET_ID` | Explicit subnet for VM/security-group stages |
| `HC_FLAVOR_NAME` | Target VM flavor |
| `HC_*_IMAGE_NAME` | Optional OS image name overrides |
| `HC_SUBNET_CIDR`, `HC_SUBNET_GATEWAY` | Subnet creation inputs |
| `HC_ADDITIONAL_SUBNET_CIDR`, `HC_ADDITIONAL_SUBNET_GATEWAY` | Additional subnet creation inputs |
| `HC_EXISTING_SUBNET_CIDRS` | Known existing CIDRs used to preflight-skip overlaps |

S3/object-storage settings:

| Variable | Description |
|---|---|
| `S3_ENDPOINT` | S3-compatible endpoint |
| `S3_REGION` | Region used for SigV4 request signing, not Terraform bucket creation |
| `S3_ACCESS_KEY` | Access key |
| `S3_SECRET_KEY` | Secret key |
| `HC_OBJECT_REGION` | Object-storage region used for bucket creation, for example `HN-01` |
| `HC_ENABLED_OBJECT_REGIONS` | Comma-separated bucket creation regions |
| `HC_OBJECT_BUCKET_PREFIX` | Temporary bucket prefix |
| `HC_OBJECT_TEST_KEY` | Object key used for upload/delete validation |
| `HC_OBJECT_TEST_BODY` | Test object body |

Object-storage bucket region precedence:

1. `HC_ENABLED_OBJECT_REGIONS`
2. `HC_OBJECT_REGION`
3. `[phases."object-storage.bucket"]` in `healthcheck.toml`

`S3_REGION` is only used for S3 request signing. Do not rely on
`S3_REGION=fpt` as the Terraform bucket region.

## `healthcheck.toml` Configuration

`healthcheck.toml` is runtime configuration by stage/phase. Override its path
with `HC_CONFIG_TOML`. Precedence is:

1. Environment variables
2. `healthcheck.toml`
3. Spec/code defaults

Target VPC coverage should be configured as a list:

```toml
[targets]
vpcs = [
  "FCI-L1-HAN-VPC",
  "FCI-L1-OPS-HAN",
]
```

Today, the live runner resolves the first target for the current single-VPC
path. The full list is intentionally stored in TOML so future
`compute.select-vpc` work can iterate all target infrastructure without moving
values out of `.env` again. If `VPC_IDS` or `VPC_ID` are set in the environment,
they override `[targets].vpcs` for compatibility.

Example compute settings:

```toml
[phases."compute.create-instance"]
delete_after_create = false
cleanup_on_quota_exceeded = false
instances_per_apply = 1
stop_on_quota_exceeded = true
disk_gb = 40
attach_subnet = true
attach_security_group = false
security_group_ids = []
assign_floating_ip = false
resize_after_create = false
create_snapshot = false
add_nic = false
```

Unsupported toggles such as `assign_floating_ip`, `resize_after_create`,
`create_snapshot`, and `add_nic` must remain `false`. If enabled, the runner
fails or skips before Terraform apply according to the configured constraints.

Example object-storage settings:

```toml
[phases."object-storage.bucket"]
region = "HN-01"
bucket_prefix = "hc-object"
test_key = "testfile.txt"
test_body = "fptcloud health-check object storage probe"
```

Multiple bucket regions:

```toml
[phases."object-storage.bucket"]
enabled_regions = ["HN-01", "HCM-01"]
```

## Common Commands

Run object-storage end to end:

```powershell
$env:PYTHONPATH = "src;scripts"
python scripts\run_health_checks.py --stage object-storage.bucket
```

This stage creates a temporary bucket with Terraform, checks the S3 endpoint,
uploads an object, validates the object with HEAD, deletes the object, and
destroys the bucket.

Compile check:

```powershell
$env:PYTHONPATH = "src;scripts"
python -m compileall src\healthcheck scripts\run_health_checks.py scripts\diagnose_health_inputs.py
```

Validate the spec:

```powershell
$env:PYTHONPATH = "src;scripts"
python scripts\validate_health_check_spec.py
```

Run tests:

```powershell
py -3.11 -m pytest -q
```

If dependencies such as `structlog` are missing, reinstall the editable package:

```powershell
py -3.11 -m pip install -e ".[dev]"
```

## Queue/Producer CLI

The `hc` CLI remains available for the queue/checklist path:

```powershell
$env:HC_USE_FAKEREDIS = "1"
hc producer run --checklist checklist.yml --run-id smoke-001 --dry-run
hc producer run --checklist checklist.yml --run-id smoke-001
hc queue stats
hc queue peek
```

With real Redis/Postgres:

```powershell
docker compose up -d redis postgres
$env:DATABASE_URL = "postgresql://hc:hc@localhost:5432/hc"
hc db migrate
```

## Extending Scenarios

Follow the project rule: **spec first, implementation second**. A new health
check scenario should be added in this order.

1. Add or update the spec.

   Define the new stage in `specs/health-check.json` with:

   - `id`: stable stage id, for example `object-storage.rotate-key`
   - `automation_status`: usually `automated` only after implementation exists
   - `required_inputs`: env/TOML/discovery values needed before execution
   - `required_cloud_resources`: resources the stage may create or touch
   - `validation_method`: how PASS/FAIL is proven
   - `cleanup_behavior`: how temporary resources are destroyed or retained
   - `dependency_stages`: stages that must pass first
   - `failure_classification`: stable error category
   - `safe_for_daily_run`: `true` only when cleanup and blast radius are clear

   Also update `specs/03-TASKS.md`, `specs/04-TESTS.md`, and
   `specs/CHANGELOG.md` when the change introduces a new behavior, new test, or
   new operator-facing contract.

2. Add runtime configuration.

   Put non-secret, per-phase options in `healthcheck.toml`:

   ```toml
   [phases."my-new-stage"]
   enabled = true
   delete_after_create = true
   concurrency = 1
   ```

   Keep secrets in `.env` only. If the setting is useful across environments,
   document it in `.env.example` with an empty value and a short comment.

   Precedence should remain:

   1. Environment variable
   2. `healthcheck.toml`
   3. Spec/code default

3. Wire the runner plan.

   If the stage is Terraform-backed, add it to
   `runner_plan.terraform_checks` in `specs/health-check.json`:

   ```json
   {
     "stage": "my-new-stage",
     "module": "my_module",
     "vars": "my_vars_source",
     "required_vars": ["name"],
     "per_region": false
   }
   ```

   Then add the corresponding variable builder in
   `src/healthcheck/stage_plan.py`.

4. Add implementation.

   Choose the smallest implementation shape that matches the scenario:

   - Terraform-only resource lifecycle: add or reuse a module under `modules/`.
   - Terraform plus API/S3 validation: add a focused runner module under
     `src/healthcheck/`, similar to `object_storage_runner.py`.
   - Pure validation/discovery: implement a local validator/discovery stage and
     avoid creating resources.

   Keep cleanup explicit. If the stage creates resources, it should destroy
   them on success and attempt best-effort cleanup on failure unless the spec
   says to retain them.

5. Register checklist/action behavior when needed.

   For queue/checklist workflows, add the action to
   `config/action_registry.yml`, then reference it from `checklist.yml`.
   The live runner is driven by `specs/health-check.json`; the queue producer is
   driven by `checklist.yml` plus the action registry.

6. Verify.

   Run:

   ```powershell
   $env:PYTHONPATH = "src;scripts"
   python scripts\validate_health_check_spec.py
   python -m compileall src\healthcheck scripts\run_health_checks.py scripts\diagnose_health_inputs.py
   python scripts\run_health_checks.py --stage my-new-stage
   ```

   If the stage touches real cloud resources, check
   `runs/<run_id>/error_queue.json` and confirm the resource was destroyed or
   intentionally retained with a logged reason.

## Scaling Configuration

Scale by adding config dimensions before adding code. Keep each dimension
bounded and visible in logs.

Common scale controls:

| Need | Preferred config |
|---|---|
| Define target VPC coverage | `[targets].vpcs = ["FCI-L1-HAN-VPC", "FCI-L1-OPS-HAN"]` |
| Run object storage in many regions | `HC_ENABLED_OBJECT_REGIONS` or `enabled_regions` in `[phases."object-storage.bucket"]` |
| Change temporary bucket naming | `HC_OBJECT_BUCKET_PREFIX` or `bucket_prefix` |
| Create more than one VM per apply later | `instances_per_apply`, after the spec and module support it |
| Keep or delete VM after validation | `delete_after_create` in `[phases."compute.create-instance"]`, or `HC_KEEP_INSTANCE` |
| Add optional VM behavior | Add a TOML toggle plus a constraint; keep unsupported toggles fail-closed |
| Select which OS image round runs first | `selection_order` in `[phases."compute.create-instance"]` |
| Avoid subnet overlap | `HC_EXISTING_SUBNET_CIDRS` |

When adding a scalable scenario, prefer this pattern:

```toml
[phases."my-new-stage"]
enabled = true
max_parallel = 1
delete_after_create = true
targets = ["target-a", "target-b"]

[[phases."my-new-stage".constraints]]
key = "max_parallel"
op = "<="
value = 1
message = "This stage is serialized until quota-aware parallel execution is implemented."
```

Guidelines:

- Default to `max_parallel = 1` for live cloud mutations.
- Add concurrency only after quota, cleanup, and retry behavior are specified.
- Log the effective config before Terraform/API mutation.
- Never put provider tokens, S3 secret keys, generated passwords, or private
  keys in TOML.
- Prefer a new phase config key over hard-coding an operator choice.
- Prefer a new stage id over changing the meaning of an existing stage.

## Relevant Paths

| Path | Role |
|---|---|
| `specs/health-check.json` | Stage catalog, runner plan, policy |
| `healthcheck.toml` | Runtime phase config; not a secret store |
| `.env.example` | Environment template |
| `modules/` | Terraform modules |
| `src/healthcheck/runner.py` | Live-run orchestrator |
| `src/healthcheck/object_storage_runner.py` | Object-storage workflow |
| `src/healthcheck/s3_client.py` | S3-compatible SigV4 probe client |
| `runs/` | Per-run logs and evidence |

## Safety Rules

- Do not commit `.env`, tokens, access keys, secret keys, or passwords.
- `healthcheck.toml` is not a secret store.
- Every behavior change must be covered by the specs before implementation.
- Resources created by health checks must have explicit cleanup behavior or a
  clear retain reason in the logs.
