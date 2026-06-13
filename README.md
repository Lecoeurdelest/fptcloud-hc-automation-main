# fptcloud-hc-automation

Spec-driven automation framework for FPT Cloud tenant health-check.

Each checkpoint in the QA checklist is enqueued as a unique, retryable task.
Workers materialize the checkpoint via Terraform (`fpt-corp/fptcloud` provider),
assert the expected state, and emit a structured result. Failed dequeues are
auto-recovered through Redis Streams consumer-group semantics with a Dead
Letter Queue (DLQ) for poison messages.

The live health-check runner uses Terraform modules under `modules/` and writes
its current report to `log.html`.

## Stack

- Python 3.11 (CPython, asyncio + threading hybrid worker pool)
- Terraform >= 1.6 CLI, invoked through `python-terraform` SDK
- `fpt-corp/fptcloud` Terraform provider (Registry)
- Redis 7 — Streams for queue, ZSET for unique-key index, Hash for DLQ payload
- Postgres 16 — durable result store (optional in dev, required in CI)
- Docker / Docker Compose for local topology
- GitHub Actions for CI execution

## Project layout

```
fptcloud-hc-automation/
├── README.md                  ← you are here
└── specs/
    ├── 00-ARCHITECTURE.md     ← components, data flow, queue design
    ├── 01-REQUIREMENTS.md     ← functional, non-functional, constraints
    ├── 02-INFRASTRUCTURE.md   ← how to stand up the runtime
    └── 03-TASKS.md            ← phased subtask backlog with DoD
```

## How to read this repo

Read in order:

1. `01-REQUIREMENTS.md` — what we are building and the rules of the game.
2. `00-ARCHITECTURE.md` — how the pieces fit.
3. `02-INFRASTRUCTURE.md` — how to stand it up.
4. `03-TASKS.md` — what to build, in what order.

Each phase in `03-TASKS.md` is independently shippable. Do **not** begin phase
N+1 until phase N's *Definition of Done* is green. Each phase ends with a
review gate; the evaluator signs off before the next phase opens.

## Effective IDs and diagnostics

Daily behavior is defined first in `specs/health-check.json` and documented in
`docs/health-check-spec.md`. The runner only executes stages that are present in
the spec, have `automation_status: automated`, have all required inputs, have
passed dependencies, and are marked `safe_for_daily_run: true`.

The FPT Cloud provider is configured with `FPTCLOUD_REGION` and
`FPTCLOUD_TENANT_NAME`. VPC resolution now happens before dependent stages:
`compute.discover-vpc` uses the official `data.fptcloud_vpc` data source with
the configured VPC name/provider lookup key, then passes `data.fptcloud_vpc.this.id`
to subnet, storage-policy, security-group, and additional-subnet stages.

Use `HC_VPC_ID` only when you already know the real provider VPC ID:

```powershell
$env:HC_VPC_ID="<vpc-uuid>"
$env:HC_SUBNET_ID="<subnet-id>"
$env:HC_STORAGE_POLICY_ID="<storage-policy-id>"
```

If `HC_STORAGE_POLICY_ID` is set, the runner skips
`data.fptcloud_storage_policy.this`. If `HC_SUBNET_ID` is set, it skips
`data.fptcloud_subnet.this`. If `HC_VPC_ID` is set, it is the effective VPC ID.
Otherwise the runner looks up the ID with `data.fptcloud_vpc.this` using
`HC_VPC_NAME`, `VPC_NAME`, `VPC_ID`, or `VPC_IDS[0]` as the VPC name/provider
lookup key. If explicit and discovered IDs both exist but differ, the report
logs a warning and keeps using `HC_VPC_ID`.

Render the sanitized resolved configuration without creating resources:

```powershell
py -3.11 scripts\print-effective-config.py --json --with-warnings
py -3.11 scripts\diagnose_health_inputs.py --json
```

Validate the formal health-check spec:

```powershell
py -3.11 scripts\validate_health_check_spec.py
```

Run the safe daily health checks after spec validation and preflight pass:

```powershell
py -3.11 scripts\run_health_checks.py
```

Run one spec stage for debugging:

```powershell
py -3.11 scripts\run_health_checks.py --stage compute.validate-subnet-inputs
py -3.11 scripts\run_health_checks.py --stage compute.discover-vpc
py -3.11 scripts\run_health_checks.py --stage compute.discover-subnet
py -3.11 scripts\run_health_checks.py --stage compute.create-subnet
```

## Choosing subnet CIDR and gateway

`compute.validate-subnet-inputs` is a dry-run stage. It creates no resources and
prints the exact `module.subnet` variables that would be used: subnet name,
CIDR, gateway, type, VPC ID, region, and tenant.

For subnet creation, set either the explicit VPC ID or a lookup key:

```powershell
$env:HC_VPC_NAME="<vpc-name>"
# or, if already known:
$env:HC_VPC_ID="<vpc-uuid-or-provider-required-vpc-id>"
$env:HC_SUBNET_CIDR="172.26.222.0/24"
$env:HC_SUBNET_GATEWAY="172.26.222.1"
```

The gateway must be an IP address inside `HC_SUBNET_CIDR` and must not be the
network or broadcast address. The CIDR should be inside the VPC CIDR and must
not overlap an existing subnet. This repository cannot prove those two facts
unless FPT Cloud exposes the VPC/subnet inventory successfully; set
`HC_VPC_CIDR` when known so local validation can check containment.

For `network.additional-subnet`, set an unused CIDR explicitly. Get subnet
inventory from the FPT Cloud portal or API first when possible. Known conflicts
in the current `FCI-L1-HAN-VPC` environment:

- `10.136.10.0/24` conflicts with `Dungnt416Network`.
- `10.136.20.0/24` conflicts with `subnet-testmnt-zla1k9l4`.

```powershell
$env:HC_VPC_CIDR="<vpc-cidr>"
$env:HC_EXISTING_SUBNET_CIDRS="10.136.10.0/24,10.136.20.0/24"
$env:HC_ADDITIONAL_SUBNET_CIDR="<unused-cidr>"
$env:HC_ADDITIONAL_SUBNET_GATEWAY="<first-usable-ip>"
```

Pick a CIDR inside the VPC range that does not overlap any existing subnet, and
use the first usable IP in that CIDR as the gateway unless your network design
requires a different valid host IP. If `HC_EXISTING_SUBNET_CIDRS` is configured,
the runner uses deterministic candidate selection before Terraform apply.

The selection starts with `HC_ADDITIONAL_SUBNET_CIDR`. On overlap, it records
the rejected CIDR and increments by ten same-size network blocks, preserving the
gateway host offset. For example:

```text
10.136.10.0/24 -> 10.136.20.0/24 -> 10.136.30.0/24
```

The attempt limit is the spec constant `MAX_SUBNET_CANDIDATE_ATTEMPTS`, currently
`100` in `specs/health-check.json`. If no non-overlapping CIDR is found before
that limit, the runner skips Terraform apply with `subnet_cidr_exhausted` and
reports every rejected CIDR. The runner does not use random CIDRs.

`HC_EXISTING_SUBNET_CIDRS` is best-effort inventory. If it misses an existing
subnet and the provider returns explicit overlap `error_code=804007`, the runner
adds that attempted CIDR to the runtime conflict list with
`conflict_source=provider_error`, records the conflicting subnet name when the
message includes it, and tries the next deterministic candidate. It retries only
subnet overlap errors; unknown errors and provider/backend system errors are not
retried.

Keep `FPTCLOUD_REGION` and `FPTCLOUD_TENANT_NAME` aligned with the VPC. Current
provider schema documents `FPTCLOUD_REGION` values as `VN/HAN`, `VN/SGN`, and
`JP/JCSI2`, but it does not clarify whether `vpc_id` means VPC UUID, IaaS
network ID, cloud ID, or another internal network identifier.

Diagnostics print `vpc_name`, `explicit_vpc_id`, `discovered_vpc_id`,
`effective_vpc_id`, and `vpc_id_source` (`explicit`, `discovered`, or
`unresolved`) so reports show whether later stages used a manually supplied ID
or the value returned by the official VPC data source.

## Naming convention

- `FR-NNN` — functional requirement
- `NFR-NNN` — non-functional requirement
- `C-NNN` — constraint
- `TC-NNN` — test case (from QA checklist)
- `P{N}.T{M}` — phase N, task M (e.g. `P2.T3`)
