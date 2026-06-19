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
- `paramiko` and `pywinrm` are installed through the Python package metadata
  and are used by the Phase-4 `InVMValidator` for SSH/WinRM probes.

## Quick Start

```powershell
py -3.11 -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Fill in `.env`, then inspect the resolved inputs:

```powershell
hc doctor --json
```

Run one stage:

```powershell
hc live run --stage object-storage.bucket
```

Run every automated stage allowed by the spec:

```powershell
hc live run
```

View a run report:

```powershell
hc live view runs\<run_id>\log.json --filter summary
hc live view runs\<run_id>\log.json --filter failed
```

The latest run artifacts are written to:

- `runs/<run_id>/log.json`
- `runs/<run_id>/error_queue.json`
- `log.html`

## Installable CLI App

The preferred operator entrypoint is the installed CLI:

```powershell
py -3.11 -m pip install -e ".[dev]"
hc --help
fptcloud-hc --help
```

Both `hc` and `fptcloud-hc` point to the same command group. `hc` is shorter
for daily use; `fptcloud-hc` is easier to recognize in scripts and runbooks.

Common commands:

```powershell
hc doctor
hc doctor --json
hc live stages
hc live stages --all
hc live run --stage object-storage.bucket
hc live view runs\<run_id>\log.json --filter summary
hc producer run --checklist checklist.yml --run-id smoke-local --dry-run
```

Command groups:

| Command | Purpose |
|---|---|
| `hc doctor` | Check Python, Terraform, spec loading, TOML loading, and required env presence without creating cloud resources |
| `hc live run` | Run the live spec-gated health-check runner |
| `hc live view` | Render `log.json` as a filtered terminal table |
| `hc live stages` | List stages from `specs/health-check.json` |
| `hc producer` | Load `checklist.yml` and enqueue/dry-run queue tasks |
| `hc queue` | Inspect Redis task queue state |
| `hc dlq` | Inspect and replay dead-lettered queue entries |
| `hc db` | Run Postgres initialization commands |

The historical script still works for compatibility:

```powershell
$env:PYTHONPATH = "src;scripts"
python scripts\run_health_checks.py --stage object-storage.bucket
```

Use the installed CLI for new workflows.

## Build And Install The CLI

Editable install for development:

```powershell
py -3.11 -m pip install -e ".[dev]"
hc --help
```

Build a wheel:

```powershell
py -3.11 -m pip install build
py -3.11 -m build
```

Install the built wheel into another environment:

```powershell
py -3.11 -m pip install dist\fptcloud_hc_automation-0.1.0-py3-none-any.whl
hc doctor
hc live stages
```

The wheel packages both runtime packages:

- `src/hc`: queue/checklist framework and CLI.
- `src/healthcheck`: live spec-gated runner.
- `specs/`, `modules/`, and default `healthcheck.toml` runtime assets.

It also includes the reporter HTML template and the diagnostics helper used by
the live runner, so installed commands do not require `PYTHONPATH=scripts`.
When running from an operator-managed checkout or config directory, set
`HC_PROJECT_ROOT` to point the CLI at that directory. Otherwise the CLI uses the
current working directory when it contains `specs/health-check.json` and
`modules/`, then falls back to packaged assets.

## Running From Code Files

The main operator entrypoint is the installed CLI:

```powershell
hc live run
```

Run a single stage by stage id:

```powershell
hc live run --stage compute.discover-vpc
hc live run --stage object-storage.bucket
```

Run from Python code when embedding the runner in another script:

```python
from healthcheck import runner

runner.run(stage_filter="object-storage.bucket")
```

Use the compatibility facade if older code imports `run_health_checks` from the
`scripts/` directory:

```python
import run_health_checks

run_health_checks.main()
```

When running from an editable source checkout without installing the package,
set `PYTHONPATH=src;scripts`. Installed CLI commands do not need `PYTHONPATH`.

## Checking Results From Log Files

Every live run creates a directory under `runs/`, for example:

```text
runs/hc-20260619-101559/
```

Important files:

| File | Purpose |
|---|---|
| `runs/<run_id>/log.json` | Ordered event log for every stage |
| `runs/<run_id>/error_queue.json` | Structured failures queued during the run |
| `runs/<run_id>/input_diagnostics.json` | Effective input/provider diagnostics |
| `log.html` | Browser-friendly report rendered from the latest run |

Render a summary from a log file:

```powershell
hc live view runs\<run_id>\log.json --filter summary
```

Show only failed/blocked/queued resources:

```powershell
hc live view runs\<run_id>\log.json --filter failed
hc live view runs\<run_id>\log.json --filter blocked
hc live view runs\<run_id>\log.json --filter queued
```

Inspect created or retained resources:

```powershell
hc live view runs\<run_id>\log.json --filter created_resources
hc live view runs\<run_id>\log.json --filter retained_resources
```

Quick raw JSON checks:

```powershell
python -c "import json; p='runs/<run_id>/log.json'; print(len(json.load(open(p, encoding='utf-8'))))"
python -c "import json; p='runs/<run_id>/error_queue.json'; print(len(json.load(open(p, encoding='utf-8'))))"
```

For a successful object-storage run, `log.json` should include:

- `object-storage.bucket` with `passed`
- `object-storage.connect-s3` with `passed`
- `object-storage.upload-file` with `passed`
- `object-storage.delete-file` with `passed`
- `object-storage.delete-bucket` with `destroyed`
- `error_queue.json` with `0` entries

## QA Checklist Coverage

The operator checklist currently contains 28 user-facing cases. The current
runner/spec set documents all 28 cases, but the automation level differs by
case.

Coverage summary:

| Level | Count | Meaning |
|---|---:|---|
| Fully automated | 7 | Runner can create/probe/cleanup the case end to end from code |
| Partially automated | 11 | Runner automates provisioning or the main mutation, but still needs manual OS/browser validation or future validator work |
| Unsupported/manual/future | 10 | Case is documented in specs but provider/API support or safe automation is not implemented yet |
| Total documented | 28 | All rows from the supplied QA checklist are represented |

Detailed mapping:

| Area | Case | Current coverage | Stage/spec mapping |
|---|---|---|---|
| Compute | Create VM subnet `172.26.221.0/24` | Fully automated | `compute.create-subnet` |
| Compute | Create Windows Server 2012 VM, 2vCPU/2GB/40GB | Partially automated | `compute.create-instance`; OS login validation is not fully automated |
| Compute | Create Windows Server 2016 VM, 2vCPU/2GB/40GB | Partially automated | `compute.create-instance`; OS login validation is not fully automated |
| Compute | Create Windows Server 2019 VM, 2vCPU/2GB/40GB | Partially automated | `compute.create-instance`; OS login validation is not fully automated |
| Compute | Create Windows Server 2022 VM, 2vCPU/2GB/40GB | Partially automated | `compute.create-instance`; OS login validation is not fully automated |
| Compute | Create Ubuntu 16.04 VM, 2vCPU/2GB/40GB | Partially automated | `compute.create-instance`; image may be unavailable; OS login validation is not fully automated |
| Compute | Create Ubuntu 18.04 VM, 2vCPU/2GB/40GB | Partially automated | `compute.create-instance`; image may be unavailable; OS login validation is not fully automated |
| Compute | Create Ubuntu 20.04 VM, 2vCPU/2GB/40GB | Partially automated | `compute.create-instance`; OS login validation is not fully automated |
| Compute | Create Ubuntu 22.04 VM, 2vCPU/2GB/40GB | Partially automated | `compute.create-instance`; OS login validation is not fully automated |
| Compute | Resize one VM to 4vCPU/4GB | Partially automated | `compute.resize-vm` |
| Compute | Resize OS disk from 40GB to 80GB | Unsupported/manual/future | `compute.resize-os-disk` |
| Compute | Add and attach 40GB disk | Partially automated | `compute.add-disk` |
| Compute | Delete VM and retain attached disk | Unsupported/manual/future | `compute.delete-vm-retain-disk` |
| Compute | Schedule VM power on/off | Unsupported/manual/future | `compute.schedule-power` |
| Compute | Create VM snapshot | Unsupported/manual/future | `compute.snapshot-create` |
| Compute | Revert VM snapshot | Unsupported/manual/future | `compute.snapshot-revert` |
| Networking | Assign public IP to VM | Unsupported/manual/future | `network.assign-public-ip` |
| Networking | Create security group for RDP/SSH only | Partially automated | `network.security-group`; connectivity and blocked-port validation remain manual |
| Networking | Add outbound HTTP/HTTPS rules | Unsupported/manual/future | `network.outbound-http-https` |
| Networking | Create additional subnet `10.136.10.0/24` or next non-overlapping candidate | Fully automated | `network.additional-subnet` |
| Networking | Add additional NIC to VM | Unsupported/manual/future | `network.additional-nic` |
| Backup & Recovery | Create backup and run backup job | Unsupported/manual/future | `backup.vm-backup-restore` |
| Backup & Recovery | Restore VM and verify test file | Unsupported/manual/future | `backup.vm-backup-restore` |
| Object storage | Create bucket | Fully automated | `object-storage.bucket` |
| Object storage | Upload file/object | Fully automated | `object-storage.upload-file` |
| Object storage | Connect through S3 endpoint | Fully automated | `object-storage.connect-s3` |
| Object storage | Delete uploaded file/object | Fully automated | `object-storage.delete-file` |
| Object storage | Delete bucket | Fully automated | `object-storage.delete-bucket` |

Important notes:

- "Partially automated" is intentionally not reported as full PASS for the
  original manual expectation when the case requires proof inside the guest OS
  or a browser.
- VM create runs now emit provider-observable `vpc.selected` and
  `instance.validated` events. Guest OS login remains manual verification.
- Object-storage is the most complete current end-to-end workflow: create
  bucket, S3 HEAD bucket, PUT object, HEAD object, DELETE object, and destroy
  bucket.
- Future work should convert unsupported/manual rows only after the spec defines
  safe inputs, cleanup behavior, validation evidence, and failure
  classification.

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
| `HC_ADDITIONAL_SUBNET_CIDR`, `HC_ADDITIONAL_SUBNET_GATEWAY` | Optional overrides for additional subnet inputs; prefer TOML |
| `HC_EXISTING_SUBNET_CIDRS` | Optional override for known existing CIDRs; prefer TOML |

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

Example additional-subnet settings:

```toml
[phases."network.additional-subnet"]
cidr = "10.136.20.0/24"
gateway_ip = "10.136.20.1"
existing_subnet_cidrs = [
  "10.136.10.0/24",
  "10.136.20.0/24",
]
```

The runner uses `cidr` as the first candidate. If it overlaps a known existing
CIDR, it deterministically advances to the next same-size network block and
keeps the gateway host offset. Environment variables
`HC_ADDITIONAL_SUBNET_CIDR`, `HC_ADDITIONAL_SUBNET_GATEWAY`, and
`HC_EXISTING_SUBNET_CIDRS` still override this TOML block for quick local tests.

## Common Commands

Run object-storage end to end:

```powershell
hc live run --stage object-storage.bucket
```

This stage creates a temporary bucket with Terraform, checks the S3 endpoint,
uploads an object, validates the object with HEAD, deletes the object, and
destroys the bucket.

Compile check:

```powershell
python -m compileall src scripts\run_health_checks.py scripts\diagnose_health_inputs.py
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

Validator-focused checks:

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m pytest tests\unit\test_validator.py -q
```

The project target runtime is still Python `>=3.11,<3.12`. A small compatibility
shim exists for local Python 3.9 test runs, so the validator suite can also be
run with the system `python` when the dependencies are already installed:

```powershell
$env:PYTHONPATH = "src"
python -m pytest tests\unit\test_validator.py -q
```

Latest local validator run:

```text
20 passed, 1 warning
```

The warning may appear on older pytest environments that do not recognize the
`asyncio_mode` option; it does not affect the validator unit suite.

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

## Validation Layer

The queue/checklist path has a reusable Phase-4 validator layer under
`src/hc/validator/`. It is separate from the live-runner service-specific
validation in `src/healthcheck/`.

Current validator capabilities:

| Validator | Status | Notes |
|---|---|---|
| `TFStateValidator` | Implemented | Supports resource dot paths, basic JSONPath-style paths, `equals`, `contains`, `regex_match`, `present`, and `absent` |
| `ManualValidator` | Implemented | Returns `INCONCLUSIVE` with the checklist note |
| `InVMValidator` | Implemented | Runs SSH probes through `paramiko` and Windows probes through `pywinrm`; supports `probe`/`command`, `exit_code`, `stdout_contains`, and `file_exists` |
| `APIProbeValidator` | Implemented | Supports HTTP/HTTPS status/body checks, retry, timeout, and TLS verification configuration |
| `CompositeValidator` | Implemented | Supports AND, OR, and NOT evaluation of multiple assertions |

Example:

```python
from hc.executor.models import TFState
from hc.models import ExpectedAssertion, TaskSpec
from hc.validator import TFStateValidator

task = TaskSpec(run_id="smoke", tc_id="TC-001", tenant_id="tenant", spec_hash="hash")
state = TFState.from_json({
    "resources": [{
        "type": "fptcloud_subnet",
        "name": "this",
        "instances": [{"attributes": {"cidr": "172.26.221.0/24"}}],
    }],
})
assertion = ExpectedAssertion(
    type="tf_state",
    path="fptcloud_subnet.this.cidr",
    equals="172.26.221.0/24",
)

result = TFStateValidator().evaluate(task, state, assertion)
print(result.verdict)
```

Example in-VM assertions:

```yaml
expected:
  - type: in_vm
    transport: winrm
    probe: "echo ok"
    contains: "ok"
    exit_code: 0

  - type: in_vm
    transport: ssh
    probe: "lsblk -b -o SIZE -d -n /dev/vda"
    contains: "85899345920"
    exit_code: 0

  - type: in_vm
    transport: ssh
    file_exists: "/home/ubuntu/Desktop/testbackup-2026-06-19.txt"
```

Connection fields can be provided directly on an assertion (`host`,
`host_path`, `port`, `username`, `password`, `private_key_path`) or derived
from Terraform state when available. For Windows, unreachable WinRM fails the
boot/login DoD path; for Linux SSH command probes, connection timeout is treated
as `INCONCLUSIVE` so the report can distinguish missing evidence from a
confirmed failed assertion.

`ExpectedAssertion` preserves checklist-specific probe fields such as `check`,
`bucket`, `key`, `url`, `note`, `method`, `timeout_seconds`, `retries`, and
`tls_verify`, so action-specific validators can evolve without losing data
during checklist loading.

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
   python scripts\validate_health_check_spec.py
   python -m compileall src scripts\run_health_checks.py scripts\diagnose_health_inputs.py
   hc live run --stage my-new-stage
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
| Avoid subnet overlap | `existing_subnet_cidrs` in `[phases."network.additional-subnet"]` |

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
| `src/hc/cli/main.py` | Installed `hc` / `fptcloud-hc` CLI |
| `src/healthcheck/runner.py` | Live-run orchestrator |
| `src/healthcheck/object_storage_runner.py` | Object-storage workflow |
| `src/healthcheck/s3_client.py` | S3-compatible SigV4 probe client |
| `src/hc/compat.py` | Python-version compatibility helpers for local test runs |
| `src/hc/validator/core.py` | Queue/checklist Phase-4 validator layer |
| `tests/unit/test_validator.py` | Validator unit tests |
| `runs/` | Per-run logs and evidence |

## Safety Rules

- Do not commit `.env`, tokens, access keys, secret keys, or passwords.
- `healthcheck.toml` is not a secret store.
- Every behavior change must be covered by the specs before implementation.
- Resources created by health checks must have explicit cleanup behavior or a
  clear retain reason in the logs.
