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
attempt. The latest attempt per `task_id` is the verdict.

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
      module: subnet
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
      module: vm
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

The runtime `TaskSpec` (post-validation) adds `task_id`, `attempt`,
`enqueued_at`, `parent_task_id` (for dependencies), and a normalized
`retry_policy`.

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
