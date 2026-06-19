# 00 — Architecture

## 1. Bird's-eye view

The system is a **closed-loop pipeline** that consumes a declarative QA
checklist, executes each checkpoint as an idempotent infrastructure task on
FPT Cloud, and emits a verifiable pass/fail verdict.

```
 ┌──────────────┐     ┌───────────┐     ┌──────────────┐     ┌──────────────┐
 │ checklist.yml│ ──► │ Producer  │ ──► │ Redis Stream │ ──► │ Worker Pool  │
 └──────────────┘     │ (dedup)   │     │  hc:tasks    │     │  (N workers) │
                      └───────────┘     └──────────────┘     └──────┬───────┘
                                              ▲                     │
                                              │ XCLAIM              ▼
                                        ┌─────┴────┐         ┌──────────────┐
                                        │  Reaper  │         │  Terraform   │
                                        │ (idle    │         │  Executor    │
                                        │  claim)  │         └──────┬───────┘
                                        └──────────┘                │
                                                                    ▼
                                                            ┌──────────────┐
                                                            │  FPT Cloud   │
                                                            │   tenant     │
                                                            └──────┬───────┘
                                                                   │
                                                                   ▼
                                                            ┌──────────────┐
                                                            │  Validator   │
                                                            │ (TF state +  │
                                                            │  in-VM/API)  │
                                                            └──────┬───────┘
                                                                   │
                                              ┌────────────────────┴──────┐
                                              ▼                           ▼
                                       ┌─────────────┐            ┌──────────────┐
                                       │  Postgres   │            │   hc:dlq     │
                                       │  results    │            │ (poison msg) │
                                       └──────┬──────┘            └──────────────┘
                                              ▼
                                       ┌─────────────┐
                                       │  Reporter   │
                                       │  (md+html)  │
                                       └─────────────┘
```

## 2. Components

### 2.1 Checklist Loader

Reads `checklist.yml` (the QA spec is the source of truth — see schema below),
validates against JSON Schema, normalizes test case IDs (`TC-XXX`), and emits a
stream of `TaskSpec` records. The current QA checklist (subnet → VM → disk →
schedule → snapshot → networking → backup → object-storage) maps 1:1 to
entries in this file.

### 2.2 Producer

Computes a deterministic `task_id = sha256(run_id || tc_id || tenant_id ||
spec_hash)`, performs a `ZADD NX hc:dedup <ts> <task_id>`. If the add returns
`0`, the task already exists for this run and the enqueue is skipped (the
unique constraint). Otherwise, `XADD hc:tasks * payload <json>` pushes the
task onto the stream.

### 2.3 Redis Stream `hc:tasks` + Consumer Group `hc-workers`

Redis Streams give us at-least-once delivery, per-consumer pending entries
list (PEL), and replay via `XREADGROUP`. We chose Streams over RabbitMQ
because the dedup ZSET, the queue, and the DLQ all live in one Redis
deployment — one dependency, one ops surface.

### 2.4 Worker

Long-running Python 3.11 process. Each worker:

1. `XREADGROUP GROUP hc-workers <worker-id> COUNT 1 BLOCK 5000 STREAMS hc:tasks >`
2. Deserialize → `TaskSpec`.
3. Acquire a per-resource lock in Redis (`SET NX EX` on `lock:<resource_key>`)
   to prevent two workers from mutating the same VM concurrently.
4. Hand the task to the **Terraform Executor**.
5. On success → `XACK hc:tasks hc-workers <entry-id>` and write result row.
6. On failure → increment `attempt`, requeue with backoff, or move to DLQ
   when `attempt > max_attempts`.

If the worker process dies between step 1 and 5, the entry stays in the PEL
and the **Reaper** reclaims it.

### 2.5 Terraform Executor

Thin wrapper around `python-terraform` (≥ `0.10`). For each task it:

1. Renders a per-task workspace under `./runs/<run_id>/<task_id>/`.
2. Writes a `main.tf` that uses **module references** (`source =
   "../../../modules/<kind>"`) so the bulk of HCL lives in versioned modules,
   not generated per-task.
3. `terraform init -backend-config=...` (remote state in Postgres or local
   for dev — see `02-INFRASTRUCTURE.md`).
4. `terraform plan -out=tfplan -detailed-exitcode` — exit code 2 means
   "changes pending" which is the expected state on first apply.
5. `terraform apply -auto-approve tfplan`.
6. Capture `terraform show -json` → parse into a Python dict for the
   Validator.

All `FPTCLOUD_*` env vars (`FPTCLOUD_API_URL`, `FPTCLOUD_REGION`,
`FPTCLOUD_TENANT_NAME`, `FPTCLOUD_TOKEN`, `VPC_ID`) are injected per
subprocess — never written to disk.

### 2.5.1 Template Renderer

Sits between the ChecklistLoader and the TerraformExecutor. Resolves
dynamic values in `TaskSpec.vars` before handing them to Terraform.

Three progressive complexity levels:

1. **Static** (Phase 2): vars from `checklist.yml` passed through
   unchanged. The renderer is a no-op identity function.
2. **Interpolated** (Phase 3): `checklist.yml` vars may contain
   template references (e.g. `${context.vpc_id}`,
   `${context.latest_image.ubuntu_2204}`). The renderer resolves them
   against a context dict built from environment variables and config.
3. **Plugin-driven** (Phase 7): context plugins fetch runtime data
   (quota headroom, available images, existing resources) from FPT
   Cloud data sources before rendering. Plugins are registered in
   `config/context_plugins.yml`.

Data flow:

```
ChecklistLoader → TaskSpec(raw_vars)
                      │
                      ▼
               TemplateRenderer
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
    static vars            context plugins
    (identity)            (HTTP, file, env)
          │                       │
          └───────────┬───────────┘
                      ▼
               TaskSpec(resolved_vars)
                      │
                      ▼
             TerraformExecutor
```

The renderer MUST be deterministic for a given (TaskSpec, context)
tuple — same inputs produce same `resolved_vars` and therefore same
`spec_hash`. Non-deterministic context (e.g. timestamp) is forbidden
in vars.

### 2.5.2 Runtime Phase Config

The live health-check runner reads optional user-authored runtime configuration
from `healthcheck.toml` (or `HC_CONFIG_TOML`) before assembling runnable stages.
This file is not a generated artifact and is not a secret store. It lets an
operator configure per-stage behavior while keeping implementation code stable.

Configuration is keyed by stage id:

```toml
[phases."compute.create-instance"]
delete_after_create = false
instances_per_apply = 1
attach_subnet = true
assign_floating_ip = false
resize_after_create = false
create_snapshot = false
add_nic = false
```

Precedence is:

1. Environment variables.
2. `healthcheck.toml` phase configuration.
3. Defaults in `specs/health-check.json`.

TOML constraints are structured data (`key`, `op`, `value`, optional
`message`). The runner evaluates them before Terraform apply. If TOML requests
behavior that is not implemented by the current module (for example floating IP
assignment, resize, snapshot, or additional NIC in the current instance-create
phase), validation fails or skips before mutation and records the reason in the
run log.

### 2.6 Validator

Each `TaskSpec` carries an `expected` block. Validators are pluggable:

- `tf_state` — assert a JSON-path on `terraform show -json` (e.g. VM power
  state, IP assignment, disk size).
- `in_vm` — open SSH/WinRM via a jump host, run a probe command, match
  output (e.g. `lsblk` reports the new disk; the `testbackup-*.txt` exists
  after restore).
- `api_probe` — direct HTTP call to FPT Cloud or to the workload itself
  (e.g. `curl https://<public_ip>` after opening port 443).

Validator outcome (`PASS` / `FAIL` / `INCONCLUSIVE`) is the authoritative
verdict — Terraform apply success is necessary but not sufficient.

### 2.7 Reaper

Background task (one per cluster, leader-elected via Redis `SET NX`). Every
60 s runs `XPENDING hc:tasks hc-workers IDLE 300000 - + 100`. Any entry idle
> 5 min is `XCLAIM`-ed to a fresh consumer, with the `attempt` field bumped.

### 2.8 DLQ `hc:dlq`

Plain Redis Stream. When `attempt > max_attempts`, the worker `XADD`s the
full failed payload (with last error, stack trace, terraform output) to
`hc:dlq` and `XACK`s the original. Operators triage DLQ manually; a CLI
command `hc dlq replay <entry-id>` reinjects after fix.

### 2.9 Result Store

Postgres table (see schema in `02-INFRASTRUCTURE.md` §5). One row per task
attempt. The latest attempt per `task_id` is the verdict. Redis is transport
only; Postgres is authoritative and the system is resumable from it alone — see
C-015 for the durability contract.

### 2.10 Reporter

Reads result store, renders:

- `report.md` — checklist with PASS/FAIL columns mirroring the QA template.
- `report.html` — same content + collapsible terraform/log panes.
- `report.json` — machine-readable for downstream pipelines.

## 3. Data flow (one task lifecycle)

```
checklist.yml entry
       │
       ▼
TaskSpec  ── dedup check ──► dropped (already enqueued)
       │
       │ unique
       ▼
XADD hc:tasks  ────────►  worker XREADGROUP
                                │
                                ▼
                        lock:<resource_key>
                                │
                                ▼
                        template render (resolve vars)
                                │
                                ▼
                        terraform init/plan/apply
                                │
                                ▼
                            validator
                                │
                  ┌─────────────┴─────────────┐
                  ▼                           ▼
                PASS                         FAIL
                  │                           │
                  ▼                           ▼
            insert result                 attempt < max?
            XACK entry                     │     │
                                          yes    no
                                           │     │
                                           ▼     ▼
                                     requeue   XADD hc:dlq
                                     w/ delay  XACK entry
```

## 4. Queue design — unique + retry contract

### 4.1 Uniqueness

`task_id` is **deterministic and content-addressable**:

```
task_id = sha256(run_id || "/" || tc_id || "/" || tenant_id || "/" || spec_hash)
spec_hash = sha256(canonical_json(task.spec))
```

Properties:

- Same checklist + same run → same task_id. A producer retry is a no-op.
- Changing any spec field → new task_id → a new task (intentional, used
  for re-runs after fixing inputs).
- `ZADD NX hc:dedup <ts> task_id` is the *only* place uniqueness is
  enforced. Workers do not re-check; they trust the producer.

### 4.2 At-least-once with idempotency

Redis Streams guarantee at-least-once delivery. Idempotency is the worker's
responsibility:

- Terraform itself is idempotent (plan diff → apply only changes).
- Per-resource Redis lock prevents concurrent mutation.
- Validator is read-only.

### 4.3 Visibility timeout (dequeue-error handling)

Once `XREADGROUP` delivers an entry to a worker, the entry sits in that
worker's PEL until `XACK`. If the worker dies, the entry's `idle_ms` keeps
growing. The Reaper reclaims any entry with `idle_ms > 300000` (5 min, tunable
per task type), reassigns it via `XCLAIM`, and increments `attempt`.

### 4.4 Retry policy

```yaml
retry_policy:
  max_attempts: 3
  backoff: exponential
  base_seconds: 30
  max_seconds: 600
  jitter: 0.2
```

A failed attempt schedules a re-enqueue at `now + backoff(attempt)` via a
Redis ZSET `hc:scheduled` (score = wake time). A separate `Scheduler`
goroutine — actually a Python coroutine — moves due entries from
`hc:scheduled` back into `hc:tasks`.

### 4.5 Poison handling — DLQ

When `attempt > max_attempts`:

```
XADD hc:dlq * task_id <id> payload <json> last_error <text> failed_at <ts>
XACK hc:tasks hc-workers <entry-id>
```

DLQ never auto-replays. Operators inspect, fix root cause, then
`hc dlq replay <id>` produces a new entry on `hc:tasks` with `attempt=0`.

## 5. Task schema

`checklist.yml` (excerpt, mapping the QA spec verbatim):

```yaml
run_id: smoke-2026-05-22
tenant_id: tenant-foo
defaults:
  retry_policy: { max_attempts: 3, base_seconds: 30 }
test_cases:
  - id: TC-001
    category: compute
    description: "Khởi tạo network cho VM"
    spec:
      action: create_subnet
      vars:
        cidr: 172.26.221.0/24
    expected:
      - type: tf_state
        path: "fptcloud_subnet.this.cidr"
        equals: "172.26.221.0/24"
  - id: TC-002
    category: compute
    description: "VM Windows Server 2012, 2vCPU/2GB/40GB"
    spec:
      action: create_vm
      vars: { os: windows-2012, cpu: 2, ram_gb: 2, disk_gb: 40 }
    expected:
      - type: tf_state
        path: "fptcloud_instance.this.power_state"
        equals: "running"
      - type: in_vm
        probe: "echo ok"
        contains: "ok"
  # ... TC-003 … TC-024 (full QA list)
```

`spec.module` is **not** authored in the checklist. The ChecklistLoader infers
the Terraform module from `spec.action` via the action registry
(`config/action_registry.yml`, see §5.1) at load time, and infers dependency
wiring from resource references unless an explicit `depends_on` override is
given (C-016).

The runtime `TaskSpec` (post-validation) adds `task_id`, `attempt`,
`enqueued_at`, `parent_task_id` (for dependencies), and a normalized
`retry_policy`.

### 5.1 Action Registry

`config/action_registry.yml` maps action names to Terraform modules and default
validators. This file is the ONLY place where new resource types are wired into
the system — no Python code changes required.

```yaml
actions:
  create_subnet:
    module: subnet
    validators: [tf_state]
    resource_key_template: "subnet:${vars.cidr}"

  create_vm:
    module: vm
    validators: [tf_state, in_vm]
    resource_key_template: "vm:${vars.os}-${vars.cpu}cpu"
    default_depends_on_actions: [create_subnet]

  resize_vm:
    module: vm
    validators: [tf_state, in_vm]
    resource_key_template: "vm:${parent.resource_key}"
    requires_existing: true

  attach_disk:
    module: disk
    validators: [tf_state, in_vm]
    resource_key_template: "disk:${vars.size_gb}gb-${parent.resource_key}"
    default_depends_on_actions: [create_vm]

  create_security_group:
    module: security_group
    validators: [tf_state, api_probe]
    resource_key_template: "sg:${vars.name}"

  assign_floating_ip:
    module: floating_ip
    validators: [tf_state, api_probe]
    resource_key_template: "fip:${parent.resource_key}"
    default_depends_on_actions: [create_vm, create_security_group]

  create_bucket:
    module: object_storage
    validators: [tf_state]
    resource_key_template: "bucket:${vars.name}"

  # Gap items — no module yet, direct API fallback
  create_snapshot:
    module: null
    executor: api_fallback
    validators: [api_probe]
    gap: provider_resource_missing

  schedule_vm_power:
    module: null
    executor: api_fallback
    validators: [api_probe]
    gap: provider_resource_missing
```

Extension protocol: to support a new FPT Cloud resource type, an operator drops
a Terraform module into `modules/<name>/`, adds an entry to
`action_registry.yml`, and adds test cases to `checklist.yml`. Zero Python code
changes. This mirrors the "Sovereign" control-plane pattern: config-driven,
code-stable.

## 6. Concurrency model

- **Worker count** = `N` (configurable, default 4). Each worker pulls one
  task at a time. Workers are stateless beyond the current task.
- **Resource locks** prevent two workers from racing on the same VM or
  subnet. Lock key = `lock:<tenant>:<resource_kind>:<resource_name>`.
- **Per-task workspace** isolates Terraform state files; no shared mutable
  state between concurrent applies.
- **Dependency edges** (e.g. *"add disk"* depends on *"create VM"*) are
  expressed as `depends_on: [TC-002]` in the checklist; the producer enqueues
  a task only when all parents are PASS. A `DependencyResolver` watches the
  result store and unblocks tasks as parents complete.

## 7. Failure modes & recovery

| Failure                              | Detection                       | Recovery                                          |
|--------------------------------------|----------------------------------|---------------------------------------------------|
| Worker process crash                 | PEL idle > 5 min                 | Reaper XCLAIMs to another worker                  |
| Terraform apply timeout              | `python-terraform` timeout       | Kill subprocess, mark attempt fail, retry         |
| FPT Cloud API 5xx                    | Provider error in TF output      | Classify retryable, requeue with backoff          |
| FPT Cloud quota exceeded             | Provider error w/ quota code     | Move to DLQ immediately (no retry)                |
| Validator network unreachable        | SSH/HTTP timeout                 | Mark INCONCLUSIVE, retry once                     |
| Redis outage                         | Connection error                 | Worker pauses, exponential backoff reconnect      |
| Duplicate enqueue (producer retry)   | `ZADD NX` returns 0              | Silent no-op                                      |
| Stuck dependency (parent never PASS) | Resolver age threshold (1 h)     | Emit `INCONCLUSIVE` for child, alert              |

## 8. Observability

- **Structured logs** (`structlog`, JSON output) with `run_id`, `task_id`,
  `tc_id`, `attempt` on every record.
- **Metrics** via Prometheus pull endpoint on each worker:
  `hc_tasks_enqueued_total`, `hc_tasks_completed_total{verdict=}`,
  `hc_task_duration_seconds`, `hc_dlq_depth`, `hc_pel_depth`.
- **Tracing** is out of scope for v1.

## 9. Glossary

- **Checkpoint** — one row in the QA checklist; becomes one task.
- **Run** — one execution of the entire checklist against one tenant.
- **PEL** — Pending Entries List, Redis Streams term for in-flight entries.
- **DLQ** — Dead Letter Queue (`hc:dlq`).
- **Verdict** — final pass/fail/inconclusive for a checkpoint.
