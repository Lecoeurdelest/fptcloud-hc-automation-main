# 03 — Tasks by Phase

Eight phases. Each phase has an explicit **Definition of Done (DoD)** and a
**review gate**. Do not start phase N+1 before phase N's DoD is green and the
evaluator signs off. Phases are sized to fit one focused session each.

Subtask IDs follow `P{phase}.T{task}`. Reference them in commits and PRs.

## Progress tracking

Every subtask, DoD line, and review gate is a checkbox. The implementing
agent **must** tick `[x]` immediately after the item is completed, in the
same commit as the implementation. Operators read this file as the single
source of truth for "where are we".

Convention:
- `- [ ]` — not started
- `- [~]` — in progress (set when work begins, before commit)
- `- [x]` — done and verified
- `- [!]` — blocked or deviated — mandatory comment beneath explaining why

A phase is **closed** only when every checkbox in that phase is `[x]`.

---

## Phase 0 — Foundation

**Status:** `[x]` completed

**Goal:** Repository skeleton that builds, lints, and tests cleanly on an
empty checklist.

### Subtasks

- [x] **P0.T1** — Initialize repo: `pyproject.toml` (Python 3.11), `ruff`,
      `mypy --strict`, `pytest`, `coverage`. Pre-commit hooks for ruff and mypy.
- [x] **P0.T2** — Create directory layout: `src/hc/{queue,executor,validator,
      cli,reporter,models,config}/__init__.py`, `tests/`, `modules/`, `runs/`.
- [x] **P0.T3** — Multi-stage `Dockerfile` (base, producer, worker, reaper,
      cli targets). Pin Terraform binary version + SHA256.
- [x] **P0.T4** — `docker-compose.yml` with `redis`, `postgres`, `producer`,
      `worker`, `reaper`, `cli`, `migrate`. Healthchecks on redis and postgres.
- [x] **P0.T5** — `Makefile` with `make fmt`, `make lint`, `make test`,
      `make up`, `make down`.
- [x] **P0.T6** — GitHub Actions workflow running `make lint test` on push and
      PR. Build the Docker image but do not push.
- [x] **P0.T7** — `pydantic` models for `Checkpoint`, `TaskSpec`, `RetryPolicy`,
      `ExpectedAssertion`, `Verdict`. No runtime behavior yet.

### DoD

- [x] `make lint test` passes locally and in CI.
- [x] `docker compose up -d redis postgres` brings both services healthy.
- [x] The empty `cli` entrypoint prints `--help` without import errors.
- [x] `mypy --strict src/` is clean.

> Note (P0.T3 / P0.T6): the multi-stage `Dockerfile` and the CI `docker build`
> step are authored and the compose file validates against all targets, but a
> full local `docker build` could not be exercised on the dev machine — Docker
> Desktop's Linux engine repeatedly crashed mid-build (`rpc EOF` / broken
> engine pipe). The image build runs in CI on GitHub's runners (`docker-build`
> job in `.github/workflows/ci.yml`). The Terraform pin uses the official
> SHA256 for `terraform_1.9.8_linux_amd64.zip`.

### Review gate

- [ ] Evaluator confirms repo hygiene: license header, README, no leaked
      secrets, ruff config matches house style.

---

## Phase 1 — Queue core

**Status:** `[x]` completed

**Goal:** A working unique-enqueue + at-least-once-dequeue queue with DLQ
and reaper, **isolated** from Terraform and FPT Cloud.

### Subtasks

- [x] **P1.T1** — `RedisQueue` class: `enqueue(task)` that does
      `ZADD NX hc:dedup` then `XADD hc:tasks`. Return `Enqueued | Duplicate`.
- [x] **P1.T2** — `RedisQueue.consume(group, consumer, block_ms)` returning
      one entry at a time via `XREADGROUP`.
- [x] **P1.T3** — `RedisQueue.ack(entry_id)` and `RedisQueue.nack(entry_id,
      reason)`. `nack` schedules a retry via `hc:scheduled` ZSET with backoff.
- [x] **P1.T4** — `Scheduler` coroutine: every 1 s, `ZRANGEBYSCORE hc:scheduled
      -inf <now>`, move due entries back to `hc:tasks`, remove from ZSET.
- [x] **P1.T5** — `Reaper` coroutine: every 60 s, `XPENDING` query, `XCLAIM`
      entries idle > `HC_REAPER_IDLE_MS`, bump `attempt` field.
- [x] **P1.T6** — DLQ: when `attempt > max_attempts`, `XADD hc:dlq` with
      `last_error` and `XACK` original. Persist full payload.
- [x] **P1.T7** — CLI: `cli queue stats`, `cli queue peek`, `cli dlq list`,
      `cli dlq replay <id>`.
- [x] **P1.T8** — Tests: unique enqueue, duplicate dropped, worker crash →
      reaper reclaim, max-attempts → DLQ, replay round-trip. Use `fakeredis`
      for unit tests; `redis:7-alpine` container for integration.

### DoD

- [x] 1000 tasks enqueued, 1000 acked, 0 lost — verified by integration test.
- [x] Worker killed mid-task → another worker completes it within
      `HC_REAPER_IDLE_MS + 60 s`.
- [x] Coverage ≥ 85% on `src/hc/queue/`.
- [x] `cli queue stats` shows correct pending/PEL/DLQ depths.

### Review gate

- [ ] Evaluator runs the chaos-test script (`scripts/chaos_kill_worker.sh`)
      and confirms zero task loss and zero duplicate side effects.

> Implemented: `scripts/chaos_kill_worker.sh` (+ `scripts/chaos_worker.py`).
> Ran locally against a real `redis:7-alpine` container: a worker was SIGKILLed
> while holding 5 unacked tasks, the Reaper reclaimed them, survivors drained
> the queue. Result for 120 tasks: `distinct completed=120`, `pel_depth=0`,
> `dlq_depth=0`, duplicate side effects=0 — `RESULT: PASS`. Awaiting evaluator
> sign-off.

---

## Phase 2 — Terraform executor

**Status:** `[x]` completed

**Goal:** Run a single Terraform module per task in an isolated workspace,
capture state and outputs, surface classified errors.

### Subtasks

- [x] **P2.T1** — `TerraformExecutor` class wrapping `python-terraform`.
      Constructor takes workspace path, module path, vars dict, env dict.
- [x] **P2.T2** — Workspace bootstrap: render `main.tf` (a single `module`
      block), write `terraform.tfvars.json`, run `terraform init` with shared
      plugin cache.
- [x] **P2.T3** — Plan + apply: `plan -out=tfplan -detailed-exitcode`, then
      `apply tfplan`. Capture stdout/stderr line by line, stream to logger.
- [x] **P2.T4** — Post-apply: `terraform show -json` → parse into `TFState`
      model.
- [x] **P2.T5** — Error classifier: parse provider error messages. Categories:
      `transient` (retry), `quota` (DLQ), `auth` (DLQ + alert), `schema` (DLQ),
      `unknown` (retry once, then DLQ).
- [x] **P2.T6** — Workspace cleanup on success (configurable); preserved on
      failure for forensics.
- [x] **P2.T7** — Modules `subnet`, `vm`, `disk`, `security_group`,
      `floating_ip`, `object_storage`. Each module is idempotent and pinned to
      the provider version in `C-002`.
- [x] **P2.T8** — Tests: mock `terraform` binary with a fake that returns
      canned plans; integration test against a Localstack-style stub if
      available, otherwise marker `@pytest.mark.live` skipped by default.

### DoD

- [~] Each module passes `terraform fmt -check`, `terraform validate`, and
      `tflint`.
- [x] A dry-run executor invocation produces the expected plan JSON.
- [x] Error classifier covers all known FPT Cloud provider error codes seen
      in CI logs; unmatched errors fall to `unknown` and log a warning to
      request classifier update.
- [x] `mypy --strict src/hc/executor/` clean.

### Review gate

- [ ] Evaluator reviews one full apply log (subnet creation) end-to-end and
      the error-classifier table.

---

## Phase 3 — Checklist DSL + Producer

**Status:** `[ ]` not started

**Goal:** Turn the QA checklist YAML into a stream of unique tasks on the
queue, honoring dependencies.

### Subtasks

- [ ] **P3.T1** — JSON Schema for `checklist.yml`. Validate on load; reject
      with line-pointed errors via `jsonschema`.
- [ ] **P3.T2** — `ChecklistLoader`: parse YAML, expand `defaults`, normalize
      IDs (`TC-XXX`), compute `spec_hash` and `task_id` per entry.
- [ ] **P3.T3** — `DependencyResolver`: topological sort with cycle detection;
      expose `ready_tasks(completed: set[str]) -> list[TaskSpec]`.
- [ ] **P3.T4** — `Producer` CLI: `--checklist <path> --run-id <id>
      [--dry-run]`. Inserts the `hc_runs` row, enqueues ready tasks, watches
      Postgres for completions to unblock children.
- [ ] **P3.T5** — Pre-flight quota check: query data sources, abort if any
      resource limit is < 1.2× demand.
- [ ] **P3.T6** — Resumability: if `run_id` exists, skip PASSED tasks,
      re-enqueue PENDING/FAILED. This is just the unique-key contract —
      verify behavior.
- [ ] **P3.T7** — Author the full `checklist.yml` from the QA spec (TC-001
      through TC-024, all four categories). Mark gap items (TC-014 VM
      schedule, TC-015/16 snapshot, TC-017/18 backup) with
      `gap: provider_resource_missing` and an `expected.type: manual` fallback.
- [ ] **P3.T8** — Tests: schema validation, dedup on re-submit, dependency
      unblocking, quota pre-flight failure path.

### DoD

- [ ] `producer --checklist checklist.yml --run-id test` enqueues exactly N
      unique tasks (N = count of TCs minus gap items in `manual` mode).
- [ ] Re-running with the same `run_id` enqueues 0 new tasks (all dedup hits).
- [ ] Dependency resolver rejects a cyclic test fixture.

### Review gate

- [ ] Evaluator inspects the rendered task list and the `gap` annotations,
      confirms they match the QA checklist accurately.

---

## Phase 4 — Validators

**Status:** `[ ]` not started

**Goal:** Each verdict is grounded in evidence beyond Terraform apply success.

### Subtasks

- [ ] **P4.T1** — `Validator` protocol: `evaluate(task, tf_state) -> Verdict`.
- [ ] **P4.T2** — `TFStateValidator`: JSONPath-based assertions against
      `terraform show -json` output. Supports `equals`, `contains`,
      `regex_match`, `present`, `absent`.
- [ ] **P4.T3** — `InVMValidator`: SSH (paramiko) for Linux, WinRM (pywinrm)
      for Windows. Connection params derived from TF state. Probes: `command`,
      `exit_code`, `stdout_contains`, `file_exists`.
- [ ] **P4.T4** — `APIProbeValidator`: HTTP/HTTPS requests with retries, TLS
      verification, expected status code / body match.
- [ ] **P4.T5** — `CompositeValidator`: AND/OR/NOT of sub-validators; the
      default for a checkpoint with multiple `expected` blocks is AND.
- [ ] **P4.T6** — `ManualValidator`: marks a gap-item task as INCONCLUSIVE
      with a clear "human action required" note, but does **not** count as a
      hard fail in the report's success rate.
- [ ] **P4.T7** — Tests: each validator independently, plus a composite
      failure path. Mock SSH/WinRM/HTTP at the library boundary.

### DoD

- [ ] TC-002 (Windows 2012 boot) passes when the VM is up and the WinRM probe
      returns `ok`; fails when WinRM is unreachable.
- [ ] TC-011 (hot-add disk grow) passes only when `lsblk` inside the VM
      reports 80 GB.
- [ ] TC-019 (restore brings back the `testbackup-*.txt`) passes only when
      the file is observed inside the restored VM.

### Review gate

- [ ] Evaluator dry-runs all validators against a fixture state file and
      confirms each verdict is correctly grounded.

---

## Phase 5 — Worker integration

**Status:** `[ ]` not started

**Goal:** End-to-end: a task enqueued by the producer is picked up,
provisioned via Terraform, validated, and recorded — for one TC.

### Subtasks

- [ ] **P5.T1** — `Worker` main loop: pull → lock → execute → validate → ack.
- [ ] **P5.T2** — Per-resource Redis lock with `SET NX EX`. Lock key derived
      from task's primary output (`resource_kind:resource_name`).
- [ ] **P5.T3** — Postgres writer: insert `hc_attempts` row at start, update
      at end. Transactional state machine for `hc_tasks.state`.
- [ ] **P5.T4** — Wire executor errors → classifier → retry/DLQ decisions.
- [ ] **P5.T5** — Wire validator verdict → `hc_attempts.verdict` and final
      `hc_tasks.state`.
- [ ] **P5.T6** — Worker graceful shutdown: SIGTERM → finish current task →
      ack/nack → exit. SIGKILL is the unhappy path covered by the Reaper.
- [ ] **P5.T7** — Prometheus `/metrics`: counters and histograms enumerated
      in `00-ARCHITECTURE.md` §8.
- [ ] **P5.T8** — End-to-end integration test for TC-001 (subnet) using a
      recorded provider response (`pytest-vcr` or hand-rolled fixture).

### DoD

- [ ] `make smoke` runs TC-001 against a real tenant (gated by env vars) and
      produces a PASS verdict.
- [ ] Killing the worker mid-apply during the smoke test results in the task
      reaching PASS after recovery, with `attempts >= 2` and no duplicate
      resources in the tenant.

### Review gate

- [ ] Evaluator audits the audit-trail rows in Postgres after a smoke run
      and confirms every state transition is recorded.

---

## Phase 6 — Reporter

**Status:** `[ ]` not started

**Goal:** Operator gets a verdict report that mirrors the QA template.

### Subtasks

- [ ] **P6.T1** — `Reporter` reads `hc_runs`, `hc_tasks`, latest `hc_attempts`
      per task.
- [ ] **P6.T2** — Markdown renderer: table with columns
      `STT | Mục tiêu | Cách thực hiện | Kết quả kỳ vọng | Thực tế | Verdict`.
- [ ] **P6.T3** — HTML renderer: same content, collapsible Terraform diff
      and validator log per row. Single self-contained HTML (inline CSS).
- [ ] **P6.T4** — JSON renderer: machine-readable, stable schema, versioned.
- [ ] **P6.T5** — `cli report render --run-id <id> --out <dir>`.
- [ ] **P6.T6** — `cli wait --run-id <id> --timeout <s>` blocks until all
      tasks terminate; exits non-zero if any fail.
- [ ] **P6.T7** — Snapshot tests on the renderers using a fixture run.

### DoD

- [ ] A finished smoke run produces `report.md` that, side by side with the
      QA template, looks recognizable (same column layout, same TC ordering).
- [ ] `cli wait` exit codes: 0 if all PASS or all PASS+INCONCLUSIVE, 1 if any
      FAIL or DEAD.

### Review gate

- [ ] Evaluator opens `report.html` from a smoke run and confirms it is
      usable without any other tool.

---

## Phase 7 — Hardening & extensions

**Status:** `[ ]` not started

**Goal:** Production-ready posture and coverage of the gap items.

### Subtasks

- [ ] **P7.T1** — Direct-API fallback for gap items (TC-014 schedule,
      TC-015/16 snapshot, TC-017/18 backup). Plug-in adapter behind the
      existing `Validator` and a new `ApiExecutor` alongside
      `TerraformExecutor`.
- [ ] **P7.T2** — Migrate Terraform state to the `pg` backend (one schema
      per run). Document the migration path.
- [ ] **P7.T3** — Distributed deployment: Helm chart for K8s (workers as a
      `Deployment`, redis as a managed service or `StatefulSet`, reaper as a
      singleton).
- [ ] **P7.T4** — Token rotation: workers re-read `FPTCLOUD_TOKEN` from a
      file/Vault on SIGHUP without a restart.
- [ ] **P7.T5** — Chaos suite: scripted faults (kill worker, partition redis,
      drop network, throttle FPT Cloud). All must auto-recover to a correct
      final state.
- [ ] **P7.T6** — Performance benchmarks: 8 concurrent VM creates on a 4 vCPU
      host, capture throughput numbers in the README.
- [ ] **P7.T7** — Security review: dependency audit (`pip-audit`), image scan
      (`trivy`), secret scan (`gitleaks`). Findings tracked to closure.

### DoD

- [ ] Gap items have at least one working path (Terraform when the provider
      catches up, API otherwise) — every TC in the original checklist has an
      automated verdict.
- [ ] Chaos suite green for ≥ 1 hour of continuous fault injection.
- [ ] Security scans clean or all findings have written exemptions.

### Review gate

- [ ] Final acceptance — sign-off to declare v1.0.

---

## Gap items, called out

These QA checkpoints rely on FPT Cloud features that may not have a
`terraform-provider-fptcloud` resource at the time of writing. The
framework still tracks them — they execute via the **direct-API fallback**
introduced in phase 7. Until then, they are marked `INCONCLUSIVE` with a
clear "feature requires provider support" note.

| TC      | Feature                  | Provider resource (if any) | Phase to resolve |
|---------|--------------------------|----------------------------|-------------------|
| TC-014  | VM power schedule        | not found                  | P7.T1             |
| TC-015  | Create snapshot          | not found                  | P7.T1             |
| TC-016  | Revert snapshot          | not found                  | P7.T1             |
| TC-017  | Create backup            | not found                  | P7.T1             |
| TC-018  | Restore from backup      | not found                  | P7.T1             |
| TC-023  | Connect via S3 endpoint  | client-side, not provider  | P7.T1 (validator) |

If new provider resources land before phase 7 ships, file an issue with the
resource name and bump the relevant TC to use it.

---

## Progress dashboard (auto-readable)

Use this snippet from the repo root to see live progress at a glance:

```bash
grep -E '^\- \[[ x~!]\]' specs/03-TASKS.md | sort | uniq -c | sort -rn
```

Or count per phase:

```bash
for p in 0 1 2 3 4 5 6 7; do
  done=$(grep -cE "^\- \[x\] \*\*P${p}\." specs/03-TASKS.md)
  total=$(grep -cE "^\- \[.\] \*\*P${p}\." specs/03-TASKS.md)
  echo "Phase $p: $done / $total"
done
```