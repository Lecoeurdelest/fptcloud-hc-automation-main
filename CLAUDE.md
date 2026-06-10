# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Spec-driven framework that runs an FPT Cloud tenant QA checklist as a pipeline:
each checklist checkpoint becomes a unique, retryable task on a Redis Streams
queue; workers materialize it with Terraform (`fpt-corp/fptcloud` provider),
a Validator asserts the expected state, and a Reporter emits the verdict.

**The build is phased and in progress** (see `specs/03-TASKS.md`). Read the
specs as the design contract, but verify against code — they describe the
end state, not what exists today.

- **Implemented:** queue core (`src/hc/queue/`), models (`src/hc/models/`),
  CLI (`src/hc/cli/`), config (`src/hc/config/`), Terraform executor +
  error classifier (`src/hc/executor/`), Terraform modules (`modules/`).
- **Stubs only (`__init__.py` with no logic):** `src/hc/validator/`,
  `src/hc/reporter/`. There is **no `hc.worker` / `hc.producer` / `hc.reaper`
  module yet** despite the Dockerfile/`docker-compose.yml` referencing them —
  those entrypoints land in later phases. The executor and the queue's
  retry/DLQ path are not yet wired together by a running worker.

> Note: `README.md` says "this repository contains only specifications" — that
> is outdated. Source code now lives under `src/hc/`.

## Commands

Python is pinned to **3.11 exactly** (`>=3.11,<3.12`, constraint C-001). On
this Windows dev machine the interpreter is invoked as `py -3.11`; the Makefile
assumes `python`/`pytest` are already the 3.11 ones.

```powershell
py -3.11 -m pip install -e ".[dev]"     # editable install with dev tools

# Tests (unit uses fakeredis, no services needed)
py -3.11 -m pytest tests/unit -m unit -v
py -3.11 -m pytest tests/unit/test_executor.py -v              # one file
py -3.11 -m pytest tests/unit/test_dlq.py::test_name -v        # one test

# Integration: real Redis, or fakeredis fallback when Docker is unavailable
docker compose up -d redis postgres
py -3.11 -m pytest tests/integration -m integration -v
$env:HC_USE_FAKEREDIS="1"; py -3.11 -m pytest tests/integration -m integration -v

# Coverage gate (queue must stay >= 85%, NFR-010)
py -3.11 -m pytest tests/unit tests/integration --cov=src/hc/queue --cov-report=json
py -3.11 scripts/check_coverage.py --min-queue 85
```

Makefile shortcuts (use on Linux/macOS/WSL or when `python` resolves to 3.11):
`make lint` (= `ruff format --check` + `ruff check` + `mypy --strict src/`),
`make fmt`, `make test`, `make up`/`make down` (docker services),
`make coverage-check`.

Lint/type config lives in `pyproject.toml`: ruff line-length 100, rules
`E,F,I,UP,B,SIM`; mypy is **`strict = true`** — keep new code fully typed.

## Pytest markers

`unit` (no external deps), `integration` (Redis container or fakeredis
fallback via `HC_USE_FAKEREDIS=1`), `live` (real FPT Cloud tenant, gated by
`HC_LIVE_TESTS=1`). `asyncio_mode = "auto"` — async tests need no decorator.
The `queue` fixture (`tests/conftest.py`) calls `flush_all()` before and after
each test, so tests assume a clean Redis namespace.

## Architecture

### Queue design (Redis, single logical DB 0 — constraint C-004/C-005)

Four keys, all in one Redis deployment (no RabbitMQ/Kafka/SQS allowed):

| Key             | Type   | Role                                              |
|-----------------|--------|---------------------------------------------------|
| `hc:tasks`      | Stream | main work queue; consumer group `hc-workers`      |
| `hc:dlq`        | Stream | dead-letter for poison / exhausted tasks          |
| `hc:dedup`      | ZSet   | uniqueness index (`ZADD NX`), member = `task_id`  |
| `hc:scheduled`  | ZSet   | retry backlog, score = wake time                  |

`RedisQueue` ([src/hc/queue/redis_queue.py](src/hc/queue/redis_queue.py)) is the
single owner of all four. It is **synchronous** (redis-py), while `Scheduler`
and `Reaper` wrap it in **asyncio** polling loops — keep that sync-core /
async-loop split in mind when adding code.

- **Uniqueness:** `task_id = sha256(run_id/tc_id/tenant_id/spec_hash)`
  (`compute_task_id` in [src/hc/models/task.py](src/hc/models/task.py)).
  `enqueue` does `ZADD NX hc:dedup` then `XADD hc:tasks`; a duplicate returns
  `EnqueueResult.DUPLICATE` and never touches the stream. This determinism is
  what makes producer retries and resumable runs (FR-011) no-ops.
- **At-least-once + retry:** `consume` → `XREADGROUP`. `ack` → `XACK`. `nack`
  bumps `attempt`; if `attempt <= max_attempts` it schedules a re-enqueue into
  `hc:scheduled` at `now + backoff` (exponential + jitter), else it
  `_move_to_dlq`. `Scheduler.move_scheduled_to_tasks` moves due entries back.
- **Crash recovery:** `Reaper.reclaim_idle` runs `XPENDING`/`XCLAIM` for
  entries idle past `reaper_idle_ms` (default 300_000 ms), bumping `attempt`.
- **DLQ replay:** `replay_dlq` re-enqueues with `attempt=0` and a **new**
  `task_id` (suffixes `spec_hash` with `-replay`) so it bypasses the dedup set.

### Terraform executor ([src/hc/executor/](src/hc/executor/))

`TerraformExecutor.execute()` = bootstrap → plan → apply → show → optional
cleanup. Key invariants:

- **Per-task isolated workspace.** It *generates* a `main.tf` that references a
  versioned module by `source` (`modules/<kind>`) — the executor does not
  inline HCL. The bulk of infrastructure logic lives in `modules/` (vm, subnet,
  disk, floating_ip, security_group, object_storage); the generated file only
  wires variables into one `module "this"` block.
- **Secrets never hit disk (NFR-005, C-009).** `FPTCLOUD_*` creds are passed to
  the terraform subprocess via `env` only. `_write_tfvars` explicitly filters
  out any key starting with `FPTCLOUD_`. The provider is configured purely from
  env vars (`provider "fptcloud" {}` is empty). Do not add credential vars to
  tfvars or `provider {}` blocks.
- `plan` uses `-detailed-exitcode`: **rc 2 = "changes pending" is success**
  (expected on first apply); only other non-zero codes raise.

`ErrorClassifier` ([src/hc/executor/classifier.py](src/hc/executor/classifier.py))
regex-maps terraform/provider stderr to an `ErrorCategory` that should drive
retry vs DLQ once a worker wires it up: `TRANSIENT` → retry, `QUOTA`/`AUTH`/
`SCHEMA` → DLQ immediately, `UNKNOWN` → retry once then DLQ. Add new failure
signatures here (patterns are ordered, first match wins).

### Models

Pydantic v2 throughout. `TaskSpec`/`RetryPolicy` in
[src/hc/models/task.py](src/hc/models/task.py) (stream payload is the JSON of
`TaskSpec` under a single `payload` field). `Checkpoint`/`Verdict`/
`ExpectedAssertion` in [src/hc/models/checkpoint.py](src/hc/models/checkpoint.py)
model the `checklist.yml` schema (assertion types `tf_state`, `in_vm`,
`api_probe`, `manual`). The verdict comes from the Validator, **not** from
terraform's apply exit code (FR-004).

### CLI

Click group `hc` (console script in `pyproject.toml`):
`hc queue stats|peek`, `hc dlq list|replay <entry_id>`. Settings come from env
(`REDIS_URL`, `HC_CONSUMER_GROUP`, `HC_REAPER_IDLE_MS`, `HC_MAX_ATTEMPTS`) via
`QueueSettings.from_env` ([src/hc/config/settings.py](src/hc/config/settings.py)).
Copy `.env.example` → `.env` for local credentials (git-ignored).

## Working conventions

- Each phase in `specs/03-TASKS.md` has a Definition of Done and a review gate;
  do not start phase N+1 until phase N is green. Naming: `FR-`/`NFR-`/`C-`
  (requirements/constraints), `TC-` (checklist test case), `P{N}.T{M}` (task).
- `checklist.yml` is read-only to the runtime (C-007); runtime state lives in
  Redis + Postgres, never written back to the spec.
- Terraform modules must be idempotent (C-010): a second `apply` produces no
  diff (`plan -detailed-exitcode` returns 0). The provider version is pinned in
  each module's `required_providers` and bumped only via a PR touching all
  modules together.
