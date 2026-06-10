# 01 — Requirements & Constraints

## 1. Functional requirements

### FR-001 — Declarative checklist
The system shall accept the QA checklist as a single YAML file conforming to
the schema in `00-ARCHITECTURE.md` §5. No code changes shall be required to
add, remove, or modify a checkpoint.

### FR-002 — Per-checkpoint enqueue
Each checkpoint in the checklist shall be enqueued as exactly one task per
run. Re-enqueuing the same `(run_id, tc_id, tenant_id, spec_hash)` shall be a
no-op (uniqueness guarantee).

### FR-003 — Terraform-driven provisioning
Every checkpoint that mutates infrastructure shall do so through the
`fpt-corp/fptcloud` Terraform provider, invoked via the `python-terraform`
SDK. Direct HTTP calls to the FPT Cloud control plane are forbidden for
mutations (allowed only for validation reads).

### FR-004 — Independent verdict
Each checkpoint's pass/fail verdict shall come from the **Validator**, not
from Terraform's apply exit code alone. A successful apply that produces the
wrong state shall be reported as FAIL.

### FR-005 — Dependency expression
The checklist shall allow expressing parent-child dependencies between
checkpoints (e.g. *attach disk* requires *VM exists*). Children shall not be
dispatched until all parents reach verdict PASS.

### FR-006 — Retry on transient failure
Tasks that fail with a classifiable transient error (5xx, network timeout,
TF state lock) shall be retried up to `max_attempts` times with exponential
backoff + jitter.

### FR-007 — Dead-letter for poison messages
Tasks exceeding `max_attempts`, or failing with non-retryable errors
(quota exceeded, schema invalid, auth failure), shall be moved to the DLQ
and never auto-retried.

### FR-008 — Reaper for dropped tasks
The system shall recover tasks abandoned by crashed workers within 10
minutes of the crash, without operator intervention.

### FR-009 — DLQ inspection & replay
Operators shall be able to list DLQ contents and replay a single entry into
the main queue via CLI.

### FR-010 — Report generation
At the end of each run, the system shall emit `report.md`, `report.html`,
and `report.json` under `./runs/<run_id>/`. The Markdown report shall mirror
the QA template (test case | description | expected | actual | verdict).

### FR-011 — Resumable runs
If a run is interrupted (operator Ctrl-C, host reboot), resuming with the
same `run_id` shall skip already-PASSED checkpoints and re-attempt only
PENDING/FAILED ones. This is the unique-key contract in action.

### FR-012 — Per-resource serialization
Two tasks acting on the same logical resource shall not execute
concurrently. A resource lock (`lock:<tenant>:<kind>:<name>`) shall guard
critical sections.

### FR-013 — Audit trail
Every state transition (enqueued, started, succeeded, failed, dead-lettered,
replayed) shall be persisted with timestamp, worker id, and attempt number.

## 2. Non-functional requirements

### NFR-001 — Throughput
The system shall sustain ≥ 8 concurrent terraform applies per host with
N=4 workers without exceeding 80% CPU on a 4 vCPU / 8 GB host.

### NFR-002 — Latency budget
The enqueue → worker pickup latency (queueing time) shall be < 2 s at p95
when the queue depth is below 50 entries.

### NFR-003 — Recovery time objective
Worker crash recovery (RTO for a single abandoned task) shall be ≤ 10 min.
This drives the `IDLE` threshold in the Reaper.

### NFR-004 — Durability
A task acknowledged as PASSED shall survive a Redis restart. (Postgres is
the source of truth for verdicts; Redis is a transport.)

### NFR-005 — Security — secrets
`FPTCLOUD_TOKEN` and any equivalent credential shall be read from a secret
store at runtime (env var injected by the orchestrator, Vault, or
`docker secret`). Tokens shall never be written to disk, logs, or
Terraform state files persisted to a shared backend.

### NFR-006 — Security — Terraform state
Terraform state may contain sensitive data (IPs, IDs). State backends shall
be encrypted at rest. Local-file backends are allowed only for ephemeral
per-task workspaces deleted at end-of-run.

### NFR-007 — Observability
Every log line shall be structured JSON with the run/task correlation
fields. The system shall expose a Prometheus `/metrics` endpoint per worker.

### NFR-008 — Portability
The full stack shall run on a single developer laptop (Linux, macOS, or
Windows + WSL2) via `docker compose up`, with no FPT Cloud connectivity
required for unit tests (provider calls are mocked).

### NFR-009 — Reproducibility
Two runs with identical inputs (`checklist.yml`, tenant state, run_id)
shall produce identical task_ids and identical verdicts (modulo flakiness
classifiable as INCONCLUSIVE).

### NFR-010 — Test coverage
Unit test coverage of the queue, dedup, retry, and reaper components shall
be ≥ 85% measured by `coverage.py`.

### NFR-011 — Linting & typing
Code shall pass `ruff check`, `ruff format --check`, and `mypy --strict`
on the queue, executor, validator, and CLI packages.

## 3. Constraints

### C-001 — Python version
Python 3.11.x exactly. No 3.10, no 3.12 (some libraries the team uses are
not yet on 3.12; pin upper bound).

### C-002 — Terraform provider
Use `fpt-corp/fptcloud` from the public Terraform Registry. The version is
pinned in each module's `required_providers` block and bumped only via PR.

### C-003 — Terraform CLI
Terraform `>= 1.6` (so `import` blocks and `removed` blocks are available),
invoked through `python-terraform`. The CLI binary must be on `PATH` inside
the worker container.

### C-004 — Single message broker
Redis only. Adding RabbitMQ, Kafka, NATS, or SQS is out of scope. The
dedup mechanism (ZSET), the queue (Streams), the DLQ (Streams), and the
scheduled-retry set (ZSET) all live in the same Redis deployment.

### C-005 — One Redis logical DB
All queue infrastructure shall live in Redis logical DB 0. Multiplexing
keys is fine; running multiple Redis databases is not.

### C-006 — No vendor-specific managed services
The system shall not depend on AWS SQS, GCP Pub/Sub, Azure Service Bus,
or any FPT-specific queue service. It must be portable to any K8s.

### C-007 — Read-only checklist
The Producer shall not mutate `checklist.yml`. Run-time state lives in
Redis and Postgres, not in the spec file.

### C-008 — Stateless workers
Workers shall hold no persistent state on local disk beyond the
per-task workspace, which is cleaned up after each task. A worker shall
be safely killable at any time.

### C-009 — Provider auth env vars only
The Terraform provider shall be configured exclusively via environment
variables (`FPTCLOUD_API_URL`, `FPTCLOUD_REGION`, `FPTCLOUD_TENANT_NAME`,
`FPTCLOUD_TOKEN`, `VPC_ID`). No `provider {}` blocks shall hard-code
credentials.

### C-010 — Idempotent modules
Each Terraform module shall be idempotent: running `apply` twice with the
same inputs shall produce no diff on the second run. This is verified by a
post-apply `plan -detailed-exitcode` returning 0.

### C-011 — License
The repository shall be MPL-2.0 (matching the upstream provider) unless an
explicit decision is recorded otherwise.

## 4. Out of scope (v1)

- Distributed tracing (OpenTelemetry).
- Web UI for queue inspection — CLI is sufficient.
- Multi-tenant scheduling fairness (we assume one tenant per run).
- Auto-rollback of partial failures across checkpoints (each TC is
  independently disposable; cleanup is a separate `teardown` run).
- Cost estimation per task.

## 5. Assumptions

- The FPT Cloud tenant has sufficient quota for the full checklist (4
  Windows VMs + 4 Ubuntu VMs + disks + IPs + buckets).
- The operator has a valid `FPTCLOUD_TOKEN` and `VPC_ID`.
- A jump host (or direct routability) exists for in-VM validation probes.
- The provider's behavior matches the documented resource set
  (`fptcloud_instance`, `fptcloud_subnet`, `fptcloud_storage`,
  `fptcloud_security_group`, `fptcloud_floating_ip`,
  `fptcloud_object_storage_*`). Resources not yet exposed by the provider
  (e.g. VM power-schedule, on-demand snapshot/backup as of writing) are
  flagged in `03-TASKS.md` as **gap items** — the framework leaves an
  extension point for direct API fallback when those resources appear.
