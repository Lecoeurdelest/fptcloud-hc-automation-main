# Implementation Notes — `run_health_checks.py` → `healthcheck` package refactor

> Non-authoritative. This file summarizes a refactor milestone and references
> the governing specs under `specs/`. It defines no requirements or behavior;
> the authoritative source is `specs/` (see `specs/05-SPEC-GOVERNANCE.md`).

## Goal

Split the ~4,500-line `scripts/run_health_checks.py` live-runner monolith into
single-responsibility modules **without changing behavior**, keep every log
field name and event string intact, and add tests + governance/spec-gap reports.

## What changed

`scripts/run_health_checks.py` is now a thin **compatibility facade + CLI**. The
implementation moved into a new package `src/healthcheck/`:

| Module | Responsibility |
|---|---|
| `state.py` | Per-run constants/paths/status sets/stage-IDs + shared mutable run state (`events`, `run_context`, `stage_status`, queues, `instance_validation`, `existing_subnet_inventory`); password primitives. |
| `models.py` | Dataclasses (`Check`, `StageSpec`, `CandidateState`, `QueueItem`, `FailureContext`, `_ImageCreateResult`, `SubnetCandidateSelection`). |
| `config.py` | env/dotenv, spec constants, runtime settings, disk-size, `cloud_context`. |
| `spec_loader.py` | `load_spec`, gating (`runnable_spec`, `spec_preflight`, `preflight`, `input_configured`), `select_stages`, `check_from_spec`. |
| `logging.py` | JSON event log is source of truth; HTML/queues derived (`emit`, `write_log`, `now`, `status_class`, …). |
| `reporting.py` | Filtered views (`filter_events`, `render_table`), redaction, `format_failure`, quota-message formatting. |
| `classification.py` | `classify_error`, `classify_context`, `is_quota_error`, `conflicting_subnet_name`. |
| `terraform_executor.py` | `run`/`run_instance_terraform`, workspace rendering, TF state I/O, `readiness`, `destroy`, import-destroy, `ResourceLock` — no stage decisions. |
| `discovery.py` | VPC/subnet/storage-policy/image/flavor discovery + subnet-candidate math; **exact-name Premium-SSD selection preserved**. |
| `instance_runner.py` | One-VM-per-apply matrix, optimistic quota, instance/hostname/password/network validation, round selection, error-queue retry. |
| `cleanup.py` | Retain-by-default, fail-closed cleanup + reclamation. |
| `runner.py` | Orchestration: subnet validation/evidence, the two executors, `checks()`, `run()`, CLI `main()`. |

### Decisions (assumptions/tradeoffs)

- **Package placement `src/healthcheck/`** so it is importable (src already on
  `sys.path`) and already covered by the governance validator's scan of `src/`.
- **Full split + compatibility facade** (chosen with the requester): the facade
  re-exports the package's names so `import run_health_checks as runner` keeps
  working. Shared mutable state keeps object identity, so tests that mutate
  `runner.events` / `runner.run_context` work unchanged.
- **Single-patch-point convention:** heavily test-patched Terraform primitives
  (`run`, `planned_resources`, `state_resources`, `readiness`,
  `instance_id_from_state`, `instance_state_values`, `run_instance_terraform`,
  `destroy`) are module globals in `terraform_executor` and are called
  module-qualified (`tf.<name>`) by peers, so one `monkeypatch.setattr` affects
  all callers. Rebindable scalars patched by tests (`RUN_ROOT`,
  `SETTLE_SECONDS`) are referenced as `state.<NAME>`.
- **Entrypoint size:** the facade is ~70 lines of logic but re-exports the full
  namespace, so it is not under the aspirational <150-line target — this is the
  explicit tradeoff of the "facade" option that keeps the test surface intact.
- **`logging.py`** intentionally shadows the stdlib name (per the requested
  module map); peers import it as `from healthcheck import logging as hclog`.

## Behavior preserved (verification)

- Unit baseline **before** refactor (commit `457e179`): `21 failed, 132 passed,
  4 deselected`. **After** refactor: identical `21 failed, 132 passed, 4
  deselected` — same 21 pre-existing failures, **zero new regressions**.
- The 21 pre-existing failures are stale test expectations from in-flight WIP
  (old storage-policy IDs `3f359ae7…` vs current `9fe95650…`, changed event
  message formats). They were red at the WIP baseline and are intentionally
  left untouched.
- 4 previously-green tests required **patch-target migration only** (their call
  sites moved into modules): they now patch `healthcheck.terraform_executor.*`,
  `healthcheck.discovery.collected_storage_policies`, `healthcheck.state.*`,
  `healthcheck.instance_runner.*`, `healthcheck.cleanup.*`. No assertions
  changed.
- Spec governance (`scripts/validate_health_check_spec.py`): the refactor adds
  **no** new "implementation reference lacks spec coverage" errors — every moved
  stage-ID literal remains declared in `specs/health-check.json`.

## Tests added

`tests/unit/test_healthcheck_modules.py` — 32 tests importing the new modules
directly: classification table, config/disk/quota toggles, JSON-log schema +
HTML-derived, report filtering modes + redaction, spec gating + no-spec-no-run,
**Premium-SSD exact-match (and no Premium-SSD-4000 fallback)**, image/flavor/
subnet-candidate helpers, optimistic-quota report fields, password policy, and
retain-by-default/fail-closed cleanup.

## Things intentionally NOT done

- The 21 pre-existing failing tests were not "fixed" (out of scope; they encode
  stale IDs/formats — a test/spec drift to resolve separately).
- No functional behavior, stage IDs, Terraform module inputs, Premium-SSD
  exact-match logic, quota-precheck policy, or cleanup policy were changed.
- `mypy --strict src/` over the new package is not made clean in this pass (see
  Remaining risks); the config-driven `mypy` scope is `packages = ["hc"]`, so
  the package is not in the default type gate.

## Remaining risks / follow-ups

- **mypy strict:** a few `stage` parameters were moved without their
  `StageSpec | None` annotations, and `diagnose_health_inputs` / `hc.inventory`
  are untyped imports; `mypy --strict src/` would flag these. Recommended
  follow-up: restore the parameter annotations and add typing for the two
  imports (or scope mypy to `hc` as configured).
- **Ruff:** `pyproject.toml` adds a documented `per-file-ignores` for
  `src/healthcheck/*` = `E501, SIM114, UP022` (intrinsic long log-event strings,
  preserved branch structure, explicit subprocess PIPE). `ruff check` and `ruff
  format --check` are otherwise green for the package and the new test file.
- **Spec gaps** found during the audit: see `spec-coverage-report.md`.
