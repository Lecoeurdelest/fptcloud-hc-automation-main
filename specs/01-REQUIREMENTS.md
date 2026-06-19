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
mutations (allowed only for validation reads). The tag-scoped health-check
instance reclamation in `06-QUOTA-AWARE-ROLLING-STRATEGY.md` honors this:
instance **inventory listing is a read** (permitted here), and the **deletion
is performed through Terraform** (import + targeted destroy). A direct
mutating `DELETE` is permitted only as the explicit, tag-guarded exception in
**C-013**.

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

### FR-014 — Multi-VPC rolling VM validation
The health-check runner shall validate VM creation across all VPCs in
`TARGET_VPCS` (resolved from `healthcheck.toml` `[targets].vpcs`, with
`VPC_IDS`/`VPC_ID` as environment overrides) in round-robin order, creating exactly
one VM per VPC per round and keeping each created VM. Governed by
`06-QUOTA-AWARE-ROLLING-STRATEGY.md` §5.

### FR-015 — One-at-a-time create and post-create validation
Each VM shall be created one at a time, then waited to ACTIVE and validated for
instance status, network attachment, IP assignment, password generation,
metadata, and (best-effort/report-only) boot success before the next image is
attempted. Governed by §6.

### FR-015A — Exact Premium-SSD storage policy selection
For Compute Instance creation, the runner shall request `Premium-SSD` by exact
name, never by partial match, and shall not select `Premium-SSD-4000` for that
request. Terraform VM creation shall pass the provider-facing storage policy
`id` `3f359ae7-b64c-4491-84df-7ab3899400a5` as `storage_policy_id`; `id_db`
`0334c678-d427-4654-beab-39067a145aca` is report/debug metadata only. If the
exact requested policy is unresolved, classify
`storage_policy_preferred_not_found` and skip instance creation.

### FR-016 — Optimistic quota apply and stop
The runner shall not perform Compute Instance quota prechecks. It shall assume
quota is sufficient, proceed to Terraform plan/apply, and use the provider apply
result as the authoritative quota check. On a provider quota rejection, the
runner shall classify `instance_storage_quota_exceeded` or
`instance_quota_exceeded`, stop immediately, retain created/failed resources,
set `run_status=blocked_waiting_user_confirmation`, report
`remaining_images_not_attempted`, and wait for explicit user confirmation before
any cleanup, recovery, retry, or continuation. Governed by §7.

### FR-017 — Tag-and-name scoped deletion safety
Reclamation shall delete only instances proven health-check by **both** a
health-check tag (`managed_by=health-check` or `health_check=true`) **and** an
HC name pattern, never the current run's VM, and never more than one per quota
event. Selection/deletion is fail-closed. Governed by §8.

### FR-018 — Permanently-unavailable image skip
Images in `UNAVAILABLE_IMAGES` (`ubuntu-16-04`, `ubuntu-18-04`) shall be skipped
and recorded as `image.skipped`, never attempted. Governed by §4/§9.

### FR-019 — Rolling-lifecycle report events
`run-log.html`/`log.html` shall record the rolling-lifecycle events: `vpc.selected`,
`instance.created`, `instance.validated`, `quota.exceeded`, `image.skipped`,
`round.completed`, and the optimistic quota fields `quota_precheck=disabled`,
`quota_assumption=assume_sufficient`, `quota_exceeded_action=stop_and_wait_for_user`,
and `user_action_required=True` when quota blocks the run. Governed by §9.

### FR-020 — Runtime phase configuration
The live health-check runner shall accept user-editable runtime phase
configuration from `healthcheck.toml` at the repository root, or from the path
named by `HC_CONFIG_TOML`. Configuration shall be keyed by stage id, for
example `[phases."compute.create-instance"]`, and may control phase-scoped
options such as `delete_after_create`, `instances_per_apply`, subnet/security
group/floating-IP attachment intent, resize intent, snapshot intent, additional
NIC intent, disk size, and image selection order.

Environment variables shall retain highest precedence, followed by TOML phase
configuration, followed by defaults declared in `specs/health-check.json`.
Requested behavior that is not implemented shall fail or skip before Terraform
apply and must be reported in `log.json`/`log.html`; it must not be reported as
a successful health-check.

### FR-021 - Installable operator CLI
Operators shall be able to install the project as a Python package and run the
health-check tooling through console scripts, without setting `PYTHONPATH` or
calling repository scripts directly. The primary command is `hc`; `fptcloud-hc`
is an alias. The installed CLI shall expose live-runner commands (`hc live run`,
`hc live view`, `hc live stages`), queue/checklist commands, and `hc doctor` for
readiness checks that do not create cloud resources.

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

### NFR-012 — Bounded quota stop
Quota handling shall be idempotent and bounded by stopping at the first provider
quota rejection. The runner shall perform zero automatic deletions, zero
automatic recovery attempts, zero automatic retries, and shall not continue to
the next image until the user explicitly confirms the next action.

### NFR-013 — Proof-gated destruction
No instance shall be deleted without verified ownership proof (health-check tag
**and** HC name pattern). Missing tags, unreadable inventory, or import failure
shall result in no deletion (fail-closed).

### NFR-014 — Structured runtime constraints
Runtime phase constraints authored in TOML shall be structured data only:
`key`, `op`, `value`, and optional `message`. Implementations shall not evaluate
arbitrary TOML expressions as code. Supported operators are `==`, `!=`, `<`,
`<=`, `>`, `>=`, `in`, and `not_in`.

### NFR-015 - CLI packaging completeness
Build artifacts shall include every import required by installed console
scripts: `src/hc`, `src/healthcheck`, the reporter HTML template, and the
diagnostics helper used by the live runner. They shall also include `specs/`,
`modules/`, and default `healthcheck.toml` runtime assets. `hc --help`,
`hc doctor --help`, and `hc live run --help` must not require Postgres, Redis,
Terraform, cloud credentials, or a live tenant connection.

## 3. Constraints

### C-001 — Python version
Python 3.11.x exactly. No 3.10, no 3.12 (some libraries the team uses are
not yet on 3.12; pin upper bound).

### C-002 — Terraform provider
Use `fpt-corp/fptcloud` from the public Terraform Registry. The version is
pinned in each module's `required_providers` block and bumped only via PR. The
pinned version is enforced at runtime by the provider mirror baked into the
worker image at build time (see `02-INFRASTRUCTURE.md` §2); runtime workers do
not resolve providers from the registry.

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

### C-012 — Reactive-only quota model
The provider exposes no quota/usage/capacity read API or schema field for
compute, VPC, or volume storage (proven in `quota-investigation-report.md`).
Quota precheck shall be disabled and quota shall be assumed sufficient until
provider apply proves otherwise. Quota figures shall never be guessed, and quota
failures shall stop the run and wait for explicit user confirmation.

### C-013 — Inventory read direct; deletion via Terraform
Health-check instance inventory may be read via direct HTTP `GET` against
`FPTCLOUD_API_URL` (an FR-003 read). No inventory read or deletion shall run
automatically after quota exceeded. Any user-confirmed reclamation deletion shall
be performed through Terraform (`import` + `destroy -target`). A direct mutating
`DELETE` is not allowed as an automatic quota response.

### C-014 — Centralized rolling constants
All rolling-strategy tunables (`INSTANCE_DISK_GB`, `INSTANCE_VCPU`,
`INSTANCE_RAM_MB`, `MAX_INSTANCE_CREATE_RETRY`, `MAX_QUOTA_RECOVERY_ATTEMPTS`,
`TARGET_VPCS`, `SUPPORTED_IMAGES`, `UNAVAILABLE_IMAGES`, ACTIVE wait/poll) shall
live in `health-check.json → constants.ROLLING_INSTANCE_STRATEGY` with same-named
environment overrides; no tunable shall be hard-coded outside that section.

### C-015 — Verdict durability
Redis is a transport layer only. The authoritative verdict for any task is the
latest row in `hc_attempts` (Postgres). If Redis is completely wiped, the system
shall be resumable from Postgres state alone: the Producer re-reads
`hc_tasks.state` and re-enqueues only PENDING/FAILED entries. No verdict data
shall be lost. This makes NFR-004 a hard guarantee and underwrites the
resumable-runs contract (FR-011).

### C-016 — Checklist authoring complexity
A QA engineer with no Terraform knowledge shall be able to add a new test case
to `checklist.yml` by copying an existing entry and changing only:
`description`, `os`, `cpu`, `ram_gb`, `disk_gb`, `cidr`, or `port` values.
Module selection shall be inferred from `spec.action` by the ChecklistLoader
using an action-to-module registry (`config/action_registry.yml`). Dependency
wiring shall be inferred from resource references, not manually specified
(except for explicit overrides via `depends_on`).

### C-017 — TOML is runtime configuration, not provider auth
`healthcheck.toml` may configure phase behavior, but provider credentials and
provider authentication values remain environment-only per C-009. TOML must not
contain `FPTCLOUD_TOKEN`, private keys, generated passwords, or equivalent
secrets. If a TOML option requests unsupported destructive behavior, mutation
must be fail-closed before Terraform apply unless another spec explicitly
allows it.

## 4. Out of scope (v1)

- Distributed tracing (OpenTelemetry).
- Web UI for queue inspection — CLI is sufficient.
- Multi-tenant scheduling fairness (we assume one tenant per run).
- Auto-rollback of partial failures across checkpoints (each TC is
  independently disposable; cleanup is a separate `teardown` run).
- Cost estimation per task.

## 5. Assumptions

- The FPT Cloud tenant may **not** have sufficient quota for the full checklist.
  The rolling strategy (FR-016, `06-QUOTA-AWARE-ROLLING-STRATEGY.md`) assumes
  quota is sufficient until provider apply rejects the create. On quota
  exhaustion it stops and waits for explicit user instruction instead of
  reclaiming, deleting, retrying, or continuing automatically.
- The operator has a valid `FPTCLOUD_TOKEN` and `VPC_ID`.
- A jump host (or direct routability) exists for in-VM validation probes.
- The provider's behavior matches the documented resource set
  (`fptcloud_instance`, `fptcloud_subnet`, `fptcloud_storage`,
  `fptcloud_security_group`, `fptcloud_floating_ip`,
  `fptcloud_object_storage_*`). Resources not yet exposed by the provider
  (e.g. VM power-schedule, on-demand snapshot/backup as of writing) are
  flagged in `03-TASKS.md` as **gap items** — the framework leaves an
  extension point for direct API fallback when those resources appear.
