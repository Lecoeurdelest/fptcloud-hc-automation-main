# fptcloud-hc-automation

Spec-driven framework that runs an FPT Cloud tenant QA checklist as a pipeline.
Each checklist checkpoint becomes a unique, retryable task on a Redis Streams queue;
a Terraform executor materializes it via the `fpt-corp/fptcloud` provider, a Validator
asserts the expected state, and a Reporter emits the verdict.

The design contract lives in `specs/`. If any file outside `specs/` conflicts with it,
`specs/` wins.

## Requirements

- **Python 3.11** (exactly `>=3.11,<3.12`) — sufficient for all queue, checklist, and API-inventory operations
- Redis 7+ — only needed for persistent runs; replaced by in-process `fakeredis` via `HC_USE_FAKEREDIS=1`
- PostgreSQL 14+ — only needed for result persistence (`hc db migrate`, worker result rows)
- Docker + Docker Compose — optional convenience wrapper for Redis and Postgres

> **No Terraform CLI required.** FPT Cloud resources are read via direct Python HTTP calls
> (`urllib.request`, stdlib only). The `TerraformExecutor` is a Phase 5 worker component
> that is not yet active; all currently functional paths are pure Python.

## Quick start

All queue, checklist, and inventory operations run with in-process `fakeredis` and
stdlib HTTP — no Redis server, no Docker, no Postgres, no Terraform needed.

```powershell
# 1. Install (editable, with dev tools)
py -3.11 -m pip install -e ".[dev]"

# 2. Copy credentials file and fill in FPTCLOUD_* values
Copy-Item .env.example .env

# 3. Tell the queue layer to use in-process fakeredis
$env:HC_USE_FAKEREDIS = "1"

# 4. Dry-run: resolve the checklist and print the enqueue plan without touching anything
hc producer run --checklist checklist.yml --run-id smoke-001 --dry-run

# 5. Enqueue for real (state lives in-process for the lifetime of the process)
hc producer run --checklist checklist.yml --run-id smoke-001
hc queue stats
hc queue peek

# 6. Alternative: enqueue from a JSON health-check spec and write progress to log.html
hc health-checks create --spec specs/health-check.json \
    --run-id smoke-001 --tenant-id my-tenant
```

> With `HC_USE_FAKEREDIS=1` the queue is ephemeral — state is lost when the process exits.
> Use a real Redis for persistent or multi-process runs (see below).

## Full-stack setup (with Redis and Postgres)

```powershell
# Start services
docker compose up -d redis postgres

# Apply DB schema
$env:DATABASE_URL = "postgresql://hc:hc@localhost:5432/hc"
hc db migrate

# Enqueue and inspect
hc producer run --checklist checklist.yml --run-id smoke-001
hc queue stats
hc queue peek
```

## CLI reference

```
hc queue stats                          # stream / PEL / DLQ / scheduled depths
hc queue peek [--count N]               # preview next N tasks
hc dlq list [--count N]                 # list dead-letter entries
hc dlq replay <entry-id>                # reinject a DLQ entry with attempt=0
hc db migrate                           # idempotent Postgres DDL apply
hc producer run --checklist <path> \
    --run-id <id> [--dry-run]           # load checklist.yml, enqueue first wave
hc health-checks create --spec <path> \
    --run-id <id> --tenant-id <id>      # create health-check jobs, write log.html
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `HC_CONSUMER_GROUP` | `hc-workers` | Redis consumer group name |
| `HC_REAPER_IDLE_MS` | `300000` | PEL idle threshold before reclaim (ms) |
| `HC_MAX_ATTEMPTS` | `3` | Max retry attempts before DLQ |
| `FPTCLOUD_API_URL` | — | FPT Cloud API base URL |
| `FPTCLOUD_REGION` | — | Region identifier |
| `FPTCLOUD_TENANT_NAME` | — | Tenant name |
| `FPTCLOUD_TOKEN` | — | API token (never written to disk) |
| `VPC_ID` | — | VPC to deploy resources into |
| `HC_USE_FAKEREDIS` | — | Set to `1` to use in-process fakeredis |

## Running tests

```powershell
# Unit tests — pure Python, no external services (uses fakeredis internally)
py -3.11 -m pytest tests/unit -m unit -v

# Integration tests — fakeredis fallback, still no Docker needed
$env:HC_USE_FAKEREDIS = "1"
py -3.11 -m pytest tests/integration -m integration -v

# Integration tests — real Redis (requires Docker or a local Redis install)
docker compose up -d redis postgres
py -3.11 -m pytest tests/integration -m integration -v

# Coverage gate (queue module must stay >= 85%)
py -3.11 -m pytest tests/unit tests/integration --cov=src/hc/queue --cov-report=json
py -3.11 scripts/check_coverage.py --min-queue 85

# One file / one test
py -3.11 -m pytest tests/unit/test_executor.py -v
py -3.11 -m pytest tests/unit/test_dlq.py::test_name -v
```

Pytest markers: `unit` (no external deps), `integration` (Redis or fakeredis), `live` (real tenant, gated by `HC_LIVE_TESTS=1`).

## Extending the framework

Adding a new FPT Cloud resource type requires **no Python changes** to the queue or
checklist layers:

1. **Register the action** in `config/action_registry.yml` — maps the action name to
   its executor, validators, and resource key template.
2. **Add test cases** to `checklist.yml` using `spec.action: <name>`.

The `ChecklistLoader` resolves `spec.action` at load time via the registry.

The `modules/` directory contains Terraform HCL for six resource types
(`subnet`, `vm`, `disk`, `floating_ip`, `security_group`, `object_storage`), all
pinned to `fpt-corp/fptcloud 0.3.50`. These are used by the Phase 5 executor path;
the currently active pure-Python path calls the FPT Cloud API directly and does not
invoke the Terraform CLI.

## Architecture overview

```
checklist.yml ──► ChecklistLoader ──► Producer ──► Redis Stream (hc:tasks)
                                                          │
                                                    Worker Pool          ← Phase 5
                                                          │
                                          ┌───────────────┴───────────────┐
                                          ▼                               ▼
                               FPT Cloud API                          Reaper
                          (Python urllib.request)               (XCLAIM idle PEL)
                                          │
                                      Validator
                                    (tf_state / in_vm / api_probe)
                                          │
                                 ┌────────┴────────┐
                                 ▼                 ▼
                           Postgres            hc:dlq
                         (hc_attempts)     (poison msgs)
                                 │
                              Reporter
                           (md / html / json)
```

Resource provisioning and inventory reads hit the FPT Cloud REST API directly using
Python's stdlib `urllib.request` — no Terraform binary is required in the current
phases. The `TerraformExecutor` (`src/hc/executor/`) is scaffolded for Phase 5 workers
but is not yet wired into a running pipeline.

Four Redis keys, one deployment:

| Key | Type | Role |
|---|---|---|
| `hc:tasks` | Stream | Main work queue, consumer group `hc-workers` |
| `hc:dlq` | Stream | Dead-letter for exhausted / poison tasks |
| `hc:dedup` | ZSet | Uniqueness index — `ZADD NX`, member = `task_id` |
| `hc:scheduled` | ZSet | Retry backlog, score = wake time (epoch s) |

Task ID is content-addressable: `sha256(run_id/tc_id/tenant_id/spec_hash)` — producer retries and resumable runs are no-ops.

## Build status

| Phase | Status | Scope |
|---|---|---|
| 0 — Foundation | done | Repo skeleton, CI, Pydantic models |
| 1 — Queue core | done | Redis Streams, DLQ, Reaper, Scheduler |
| 2 — Executor scaffolding | done | TerraformExecutor scaffold + ErrorClassifier (not yet active) |
| 3 — Checklist DSL + Producer | done | ChecklistLoader, TemplateRenderer, DependencyResolver |
| **4 — Validators** | **next** | `tf_state`, `in_vm`, `api_probe`, `manual` |
| 5 — Worker integration | not started | End-to-end worker: pull → Python API call → validate → record |
| 6 — Reporter | not started | MD / HTML / JSON verdict reports |
| 7 — Hardening | not started | Gap items, distributed deploy, security scans |

Phases 0–3 run entirely in Python with no Terraform CLI dependency. Phase 5 is where
a running worker will invoke the executor against the live FPT Cloud API.

See `specs/03-TASKS.md` for detailed subtasks, Definitions of Done, and review gates.

## Spec index

| File | Contents |
|---|---|
| `specs/00-ARCHITECTURE.md` | Component diagram, data flow, queue contract |
| `specs/01-REQUIREMENTS.md` | Functional and non-functional requirements |
| `specs/02-INFRASTRUCTURE.md` | Postgres schema, Terraform backend, Docker layout |
| `specs/03-TASKS.md` | Phased task list with DoD and review gates |
| `specs/04-TESTS.md` | End-to-end test table (TC-001 – TC-028) |
| `specs/05-SPEC-GOVERNANCE.md` | How to amend the specs |
| `specs/health-check.json` | JSON Schema for `checklist.yml` |
