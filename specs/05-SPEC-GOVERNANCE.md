# Spec Governance

This repository is spec-driven. The `specs/` directory is the only
authoritative source for requirements, behavior, workflows, validations,
outputs, stages, artifacts, implementation rules, reporting rules, safety
rules, architecture decisions, cleanup behavior, retry behavior, and
classification behavior.

## Source of Truth Policy

- No requirement may exist outside `specs/`.
- No implementation may be introduced unless the behavior is defined in
  `specs/` first.
- No generated artifact may exist unless it is declared in this file or another
  file under `specs/`.
- No workflow may exist unless it is defined under `specs/`.
- No validation rule may exist unless it is defined under `specs/`.
- No report format may exist unless it is defined under `specs/`.
- No cleanup behavior may exist unless it is defined under `specs/`.
- Files outside `specs/` may summarize, reference, or link to specs, but they
  must not define independent requirements or behavior.

## Governing Spec Index

| Policy area | Governing spec |
|---|---|
| Architecture | `specs/00-ARCHITECTURE.md` |
| Product and runtime requirements | `specs/01-REQUIREMENTS.md` |
| Infrastructure and local/CI runtime | `specs/02-INFRASTRUCTURE.md` |
| Task workflow and phase gates | `specs/03-TASKS.md` |
| Test policy and coverage targets | `specs/04-TESTS.md` |
| Health-check stage catalog | `specs/health-check.json` |
| Spec governance and generated artifacts | `specs/05-SPEC-GOVERNANCE.md` |
| Quota-aware rolling instance strategy | `specs/06-QUOTA-AWARE-ROLLING-STRATEGY.md` |

## Stage Definition Policy

All runnable or reportable health-check stages must be declared in
`specs/health-check.json`. Each stage must define:

- `id`
- `manual_check_item`
- `automation_status`
- `required_inputs`
- `required_cloud_resources`
- `expected_result`
- `validation_method`
- `cleanup_behavior`
- `dependency_stages`
- `failure_classification`
- `safe_for_daily_run`

Implementation must not introduce stage IDs that are absent from
`specs/health-check.json`.

## Dependency Policy

Stage dependencies must be declared in `dependency_stages`. A stage must not run
when any declared dependency has not completed successfully. Dependency behavior
implemented in code must match the stage catalog.

## Safety Controls

Safety controls must be declared in the stage catalog or in a governing spec.
Safe automated stages must have `automation_status: automated`,
`safe_for_daily_run: true`, and explicit cleanup behavior when they create or
mutate cloud resources.

## Identifier Policy

Identifier precedence and discovery behavior must be declared before
implementation. For health-check stages, the governing source is each stage's
`validation_method` and `cleanup_behavior` in `specs/health-check.json`.

## Discovery Policy

Discovery stages must be declared as stages. Discovery results may be passed to
dependent stages only when the discovery stage is complete or when an explicit
input override is declared in the governing stage spec.

## Reporting Policy

Report events, summaries, report destinations, and redaction behavior must be
declared in specs before implementation. Report output must not reveal secrets.

## Runtime Configuration Policy

User-editable runtime configuration may live in `healthcheck.toml` at the
repository root, or in the path named by `HC_CONFIG_TOML`. This file is
configuration, not a generated artifact. Environment variables retain highest
precedence, followed by TOML phase configuration, followed by defaults declared
in `specs/health-check.json`.

Target infrastructure coverage may be declared in `[targets]`, for example
`vpcs = ["FCI-L1-HAN-VPC", "FCI-L1-OPS-HAN"]`. Environment variables remain
highest precedence, so `VPC_IDS` and `VPC_ID` override `[targets].vpcs` for
compatibility.

Phase configuration must be keyed by stage id, for example
`[phases."compute.create-instance"]`. Constraint checks in TOML must be
structured as `key`, `op`, `value`, and optional `message`; implementations must
not evaluate arbitrary TOML expressions as code. Any requested phase behavior
that is not implemented must fail or skip before Terraform apply rather than
being reported as a pass.

## Generated Artifact Policy

Generated artifacts must be declared before they are produced.

| Artifact | Purpose | Generation trigger | Location | Overwrite behavior | Retention policy | Governing spec |
|---|---|---|---|---|---|---|
| `log.html` | Current live health-check HTML event log. | `scripts/run_health_checks.py` run. | Repository root. | Overwritten at the start of each run. | Latest only in root; per-run Terraform logs remain under `runs/<run_id>/`. | `specs/health-check.json`, `specs/05-SPEC-GOVERNANCE.md` |
| `run-log.html` | Normalized copy of the latest health-check HTML report when a milestone explicitly requests that filename. | Explicit milestone/report generation step after a health-check run. | Repository root. | Overwritten by the latest requested report. | Latest only. | `specs/05-SPEC-GOVERNANCE.md` |
| `implementation-notes.md` | Running log of assumptions, tradeoffs, provider gaps, safety decisions, and intentionally unimplemented items for an implementation milestone. | Created or updated only when an implementation milestone requires notes. | Repository root. | Appended or updated in place for the active milestone. | Retained until superseded by a later milestone note or archived under `runs/<run_id>/`. | `specs/05-SPEC-GOVERNANCE.md` |
| `spec-coverage-report.md` | Audit of repository behavior, outputs, validations, and assumptions against `specs/`. | Spec-governance audit. | Repository root. | Overwritten by the latest audit. | Latest only unless copied into a run archive. | `specs/05-SPEC-GOVERNANCE.md` |
| `spec-compliance-report.md` | Compliance matrix for implemented features against governing specs and tests. | Spec-governance audit or CI compliance check. | Repository root. | Overwritten by the latest audit. | Latest only unless copied into a run archive. | `specs/05-SPEC-GOVERNANCE.md` |
| `runs/<run_id>/` | Per-run workspace, logs, queue snapshots, and evidence bundles. | Health-check run or evidence collection stage. | `runs/`. | Never overwritten for unique run IDs. | Retained until manually archived or deleted. | `specs/health-check.json`, `specs/01-REQUIREMENTS.md` |
| `runs/<run_id>/report.md` | Markdown health-check verdict report. | Report rendering workflow. | `runs/<run_id>/`. | Overwritten only inside the same run ID. | Retained with the run directory. | `specs/00-ARCHITECTURE.md`, `specs/01-REQUIREMENTS.md` |
| `runs/<run_id>/report.html` | HTML health-check verdict report. | Report rendering workflow. | `runs/<run_id>/`. | Overwritten only inside the same run ID. | Retained with the run directory. | `specs/00-ARCHITECTURE.md`, `specs/01-REQUIREMENTS.md` |
| `runs/<run_id>/report.json` | Machine-readable health-check verdict report. | Report rendering workflow. | `runs/<run_id>/`. | Overwritten only inside the same run ID. | Retained with the run directory. | `specs/00-ARCHITECTURE.md`, `specs/01-REQUIREMENTS.md` |
| `runs/<run_id>/*-evidence.zip` | Sanitized provider support evidence bundle. | Evidence collection stage. | `runs/<run_id>/`. | One bundle per stage/run. | Retained with the run directory. | `specs/health-check.json` |
| `runs/diagnostics/latest.json` | Latest sanitized diagnostic snapshot. | `scripts/diagnose_health_inputs.py`. | `runs/diagnostics/`. | Overwritten on each diagnostics run. | Latest plus timestamped diagnostics files. | `specs/05-SPEC-GOVERNANCE.md` |
| `runs/diagnostics/diagnostics-*.json` | Timestamped sanitized diagnostic snapshots. | `scripts/diagnose_health_inputs.py`. | `runs/diagnostics/`. | Never overwritten for unique timestamps. | Retained until manually archived or deleted. | `specs/05-SPEC-GOVERNANCE.md` |
| `coverage.json` | Machine-readable Python coverage report. | Coverage workflow. | Repository root. | Overwritten by coverage workflow. | Latest only. | `specs/04-TESTS.md` |
| `htmlcov/` | HTML Python coverage report. | Coverage workflow. | Repository root. | Overwritten by coverage workflow. | Latest only or CI artifact. | `specs/04-TESTS.md` |
| `runs/fptcloud-connect-check/` | Terraform connectivity-check workspace and instructions. | FPT Cloud connectivity check workflow. | `runs/`. | Updated by connectivity check workflow. | Retained until manually deleted. | `specs/02-INFRASTRUCTURE.md` |
| `runs/fptcloud-api-url-explanation.html` | Local explanatory reference for FPT Cloud API URL checks. | Connectivity troubleshooting workflow. | `runs/`. | Overwritten when regenerated. | Latest only. | `specs/02-INFRASTRUCTURE.md` |

## Cleanup Policy

Cleanup behavior must be declared per stage. If a stage creates or mutates cloud
resources and lacks declared cleanup behavior, it must not be marked safe for
daily execution.

## Retry Policy

Retry behavior must be declared before implementation. Queue retry policy is
governed by `specs/01-REQUIREMENTS.md` and `specs/03-TASKS.md`. Stage-specific
retry behavior is governed by `specs/health-check.json`.

## Classification Policy

Failure classifications used by health-check stages must be listed in
`specs/health-check.json`. Queue/executor classifications must be governed by
`specs/01-REQUIREMENTS.md`, `specs/03-TASKS.md`, or `specs/04-TESTS.md`.

## Validation Policy

Validation checks must be traceable to `specs/`. The spec validator must fail
when:

- A stage is missing required catalog fields.
- A stage references a dependency absent from `specs/health-check.json`.
- A safe stage with cloud resources lacks cleanup behavior.
- A failure classification used by a stage is absent from
  `failure_classifications`.
- Implementation contains a stage ID that is absent from
  `specs/health-check.json`.
- A generated artifact known to the validator is absent from the generated
  artifact policy.
- `docs/` contains authoritative requirement language instead of references to
  governing specs.

## Implementation Notes Policy

When an implementation milestone has assumptions, tradeoffs, provider/doc gaps,
or intentionally unimplemented behavior, those notes must be recorded in
`implementation-notes.md` only if this artifact is required by the milestone.
The notes must include:

- Decisions not explicitly covered by the implementation request.
- Assumptions and tradeoffs.
- Provider or documentation gaps.
- Changes made to satisfy the governing spec.
- Things intentionally not implemented.
- Safety decisions and cleanup behavior.
- Remaining risks.

Implementation notes are not authoritative. If a note defines behavior, that
behavior must be promoted into `specs/` before implementation.

## Compliance Reporting Policy

`spec-compliance-report.md` must list each implemented feature with:

- Governing spec section.
- Implementation files.
- Test coverage.
- Compliance status.

`spec-coverage-report.md` must list:

- Behaviors found outside specs.
- Outputs found outside specs.
- Validation rules found outside specs.
- Implementation assumptions found outside specs.
- Removed non-spec requirements.
- Implementation items lacking spec coverage.
