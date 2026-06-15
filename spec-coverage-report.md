# Spec Coverage Report — `healthcheck` package refactor

> Non-authoritative audit. References the governing specs under `specs/`
> (`specs/05-SPEC-GOVERNANCE.md`). It records findings only; it defines no
> requirements.

Scope: audit of the `scripts/run_health_checks.py` → `src/healthcheck/*`
refactor against `specs/`. Because the refactor is **behavior-preserving**, any
finding below pre-existed in the working-tree WIP (baseline commit `457e179`)
and was not introduced by the split.

## Method

- `py -3.11 scripts/validate_health_check_spec.py` (scans `scripts/`, `src/`,
  `modules/`).
- Diff of moved stage-ID literals between the monolith and the package.
- Unit baseline comparison (21 failed / 132 passed, identical before and after).

## Behaviors found outside specs

None introduced. The runner emits sub-event stage IDs (`<stage>:lock`,
`:destroy`, `:context`, `:inputs`, `:cleanup`, `:attempt`, `:overlap-detected`,
`:retry-summary`), discovery/quota event stages (`compute.detect-quota-exceeded`,
`compute.resolve-instance-flavor`, `network.select-additional-subnet-cidr`,
`reclaim.import_unsupported`, `instance.create_failed_queued`,
`instance.retry_started|succeeded|failed|exhausted`). The spec validator reports
**no** "implementation reference lacks spec coverage" findings, i.e. these are
all declared in `specs/health-check.json` (`stages` / `report_events`). Moving
the literals from `scripts/` to `src/` (both scanned) left coverage unchanged.

## Outputs found outside specs

None. The package writes the same declared artifacts as before: `log.html`,
`log.json`, and `runs/<run_id>/{log.json,pending_queue.json,error_queue.json,
input_diagnostics.json,*-evidence.zip,created-instances.json}` — all governed by
`specs/05-SPEC-GOVERNANCE.md` and `specs/health-check.json`. `implementation-
notes.md` and `spec-coverage-report.md` are declared generated artifacts.

## Validation rules found outside specs

None added. Spec gating (`runnable_spec`, `spec_preflight`), subnet/instance
input validation, password policy, and Premium-SSD exact-name selection are
unchanged and traceable to `specs/health-check.json` and
`specs/06-QUOTA-AWARE-ROLLING-STRATEGY.md`.

## Implementation assumptions found outside specs (non-requirement decisions)

- Package physically placed at `src/healthcheck/` (import path `healthcheck.*`).
- A documented `tool.ruff.lint.per-file-ignores` for `src/healthcheck/*`
  (`E501, SIM114, UP022`). These are code-style decisions, not spec behavior.

## Removed non-spec requirements

None. No behavior, stage IDs, Terraform inputs, quota-precheck policy, or
cleanup policy were removed or changed.

## Implementation items lacking spec coverage / drift (pre-existing)

1. **Spec ↔ validator inconsistency (GAP).** `specs/health-check.json` declares
   `compute.select-vpc` and `compute.validate-instance-active` with
   `automation_status: "not_implemented"`, which is **not** in the validator's
   allowed set (`automated, partially_automated, manual_only, blocked,
   unsupported`). `scripts/validate_health_check_spec.py` fails on these two
   stages. Resolution requires either adding `not_implemented` to the validator's
   `VALID_STATUSES` (and to `specs/05-SPEC-GOVERNANCE.md`'s Stage Definition
   Policy) or correcting the two stages' status in the spec. **Pre-existing at
   baseline; out of scope for this behavior-preserving refactor.**

2. **Test ↔ spec drift (not a runtime gap).** 21 unit tests in
   `tests/unit/test_live_health_runner.py` encode old storage-policy IDs
   (`3f359ae7…`) and event-message formats that no longer match the current
   `specs/health-check.json` (`COLLECTED_INSTANCE_STORAGE_POLICIES`, live
   Premium-SSD id `9fe95650-d902-4613-ad5a-88c94c71d725`) and the current runner
   output. The runner behavior matches the spec; the tests are stale. Recommend
   updating these tests to the spec-declared IDs/formats in a follow-up.

3. **Type-checking gap (code quality, not spec).** See `implementation-notes.md`
   — `mypy --strict src/` would flag moved `stage` parameters and untyped
   `diagnose_health_inputs` / `hc.inventory` imports.
