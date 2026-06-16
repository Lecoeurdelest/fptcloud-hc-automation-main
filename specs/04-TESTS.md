# 04 — Test Plan & Coverage Tracker

This file is the single source of truth for **what is tested, how well, and
what's missing**. Every test case is a checkbox. Coverage percentages are
computed from the ratio of `[x]` to total checkboxes per section.

## Progress formula

```
section_coverage = count([x]) / count(all checkboxes) × 100
overall_coverage = sum(all [x]) / sum(all checkboxes) × 100
```

Quick shell check:

```bash
# Per-section coverage
for section in "Unit" "Integration" "E2E" "Chaos" "Regression"; do
  done=$(sed -n "/^### ${section}/,/^### /p" specs/04-TESTS.md | grep -cE '^\- \[x\]')
  total=$(sed -n "/^### ${section}/,/^### /p" specs/04-TESTS.md | grep -cE '^\- \[.\]')
  if [ "$total" -gt 0 ]; then
    pct=$((done * 100 / total))
    echo "$section: $done/$total ($pct%)"
  fi
done

# Overall
done=$(grep -cE '^\- \[x\]' specs/04-TESTS.md)
total=$(grep -cE '^\- \[.\]' specs/04-TESTS.md)
pct=$((done * 100 / total))
echo "OVERALL: $done/$total ($pct%)"
```

## Convention

- `- [ ]` — test not written
- `- [x]` — test written + passing in CI
- `- [!]` — test written but failing or flaky — note beneath with issue link
- Each test item format: `**T-XXXX** — <description> (phase: P<N>.T<M>)`
  linking back to the subtask that should produce this test.
- Coverage targets are **minimums**. Exceeding them is fine.

---

## Coverage targets by component

| Component          | Target | Measured by          | Phase introduced |
|--------------------|--------|----------------------|------------------|
| `src/hc/queue/`    | ≥ 85%  | **95.0%** (189/199)  | P1               |
| `src/hc/executor/` | ≥ 80%  | **82%** (180/219)    | P2               |
| `src/hc/models/`   | ≥ 90%  | `coverage.py`        | P0               |
| `src/hc/validator/` | ≥ 80% | `coverage.py`        | P4               |
| `src/hc/reporter/` | ≥ 75%  | `coverage.py`        | P6               |
| `src/hc/cli/`      | ≥ 70%  | **85%**              | P1               |
| `src/hc/config/`   | ≥ 90%  | `coverage.py`        | P0               |
| Terraform modules  | 100%   | `tf validate + plan` | P2               |
| **Overall**        | ≥ 80%  | `coverage.py`        | P5               |

Update the "Measured" column with actual numbers after each phase closes.

---

## Unit tests

Isolated, no external dependencies. Mock everything at the boundary.
Run with: `make test-unit` (pytest marker: `@pytest.mark.unit`)

### Queue (`src/hc/queue/`) — target ≥ 85%

- [x] **T-0101** — `enqueue()` returns `Enqueued` for a new task (phase: P1.T1)
- [x] **T-0102** — `enqueue()` returns `Duplicate` for same `task_id` (phase: P1.T1)
- [x] **T-0103** — `task_id` is deterministic: same inputs → same hash (phase: P1.T1)
- [x] **T-0104** — `task_id` changes when `spec_hash` changes (phase: P1.T1)
- [x] **T-0105** — `consume()` returns one entry via `XREADGROUP` (phase: P1.T2)
- [x] **T-0106** — `consume()` blocks and returns `None` after timeout (phase: P1.T2)
- [x] **T-0107** — `ack()` removes entry from PEL (phase: P1.T3)
- [x] **T-0108** — `nack()` adds entry to `hc:scheduled` ZSET with correct backoff score (phase: P1.T3)
- [x] **T-0109** — `nack()` increments `attempt` field in payload (phase: P1.T3)
- [x] **T-0110** — Backoff is exponential: attempt 1 → 30s, attempt 2 → 60s, attempt 3 → 120s (phase: P1.T3)
- [x] **T-0111** — Backoff jitter is within ±20% of base (phase: P1.T3)
- [x] **T-0112** — Backoff caps at `max_seconds` (600s) (phase: P1.T3)
- [x] **T-0113** — Scheduler moves due entries from `hc:scheduled` to `hc:tasks` (phase: P1.T4)
- [x] **T-0114** — Scheduler ignores entries whose score is in the future (phase: P1.T4)
- [x] **T-0115** — Scheduler removes moved entries from ZSET atomically (phase: P1.T4)
- [x] **T-0116** — Reaper identifies entries idle > threshold via `XPENDING` (phase: P1.T5)
- [x] **T-0117** — Reaper `XCLAIM`s idle entries to a new consumer (phase: P1.T5)
- [x] **T-0118** — Reaper bumps `attempt` on reclaimed entries (phase: P1.T5)
- [x] **T-0119** — Reaper ignores entries idle < threshold (phase: P1.T5)
- [x] **T-0120** — DLQ: entry moves to `hc:dlq` when `attempt > max_attempts` (phase: P1.T6)
- [x] **T-0121** — DLQ: original entry is `XACK`ed after move to DLQ (phase: P1.T6)
- [x] **T-0122** — DLQ: payload includes `last_error`, `task_id`, `failed_at` (phase: P1.T6)
- [x] **T-0123** — DLQ replay: entry re-enters `hc:tasks` with `attempt=0` (phase: P1.T7)
- [x] **T-0124** — DLQ replay: new `task_id` prevents dedup collision (phase: P1.T7)

### Executor (`src/hc/executor/`) — target ≥ 80%

- [x] **T-0201** — Workspace bootstrap creates `main.tf` with correct module source (phase: P2.T2)
- [x] **T-0202** — Workspace bootstrap writes `terraform.tfvars.json` matching input vars (phase: P2.T2)
- [x] **T-0203** — `terraform init` is called with shared plugin cache path (phase: P2.T2)
- [x] **T-0204** — Plan returns exit code 2 (changes pending) on first apply (phase: P2.T3)
- [x] **T-0205** — Plan returns exit code 0 (no changes) on idempotent re-apply (phase: P2.T3)
- [x] **T-0206** — Apply captures stdout/stderr line by line into structured log (phase: P2.T3)
- [x] **T-0207** — Post-apply `terraform show -json` parsed into `TFState` model (phase: P2.T4)
- [x] **T-0208** — Error classifier: 5xx → `transient` (phase: P2.T5)
- [x] **T-0209** — Error classifier: quota exceeded → `quota` (phase: P2.T5)
- [x] **T-0210** — Error classifier: auth failure → `auth` (phase: P2.T5)
- [x] **T-0211** — Error classifier: schema mismatch → `schema` (phase: P2.T5)
- [x] **T-0212** — Error classifier: unknown pattern → `unknown` + warning log (phase: P2.T5)
- [x] **T-0213** — Workspace cleanup on success deletes workspace dir (phase: P2.T6)
- [x] **T-0214** — Workspace preserved on failure when `cleanup_on_fail=False` (phase: P2.T6)
- [x] **T-0215** — Env vars (`FPTCLOUD_*`) passed to subprocess, never written to disk (phase: P2.T2)

### Models (`src/hc/models/`) — target ≥ 90%

- [ ] **T-0001** — `Checkpoint` validates required fields (phase: P0.T7)
- [ ] **T-0002** — `Checkpoint` rejects unknown `category` values (phase: P0.T7)
- [ ] **T-0003** — `TaskSpec` computes deterministic `task_id` from inputs (phase: P0.T7)
- [ ] **T-0004** — `RetryPolicy` defaults: `max_attempts=3`, `base_seconds=30` (phase: P0.T7)
- [ ] **T-0005** — `RetryPolicy` validates `max_seconds >= base_seconds` (phase: P0.T7)
- [ ] **T-0006** — `ExpectedAssertion` supports types: `tf_state`, `in_vm`, `api_probe`, `manual` (phase: P0.T7)
- [ ] **T-0007** — `Verdict` enum: `PASS`, `FAIL`, `INCONCLUSIVE` (phase: P0.T7)
- [ ] **T-0008** — Pydantic models serialize/deserialize round-trip cleanly (phase: P0.T7)

### Checklist loader (`src/hc/` producer area) — target ≥ 85%

- [x] **T-0301** — JSON Schema validates a correct `checklist.yml` (phase: P3.T1)
- [x] **T-0302** — JSON Schema rejects missing `run_id` (phase: P3.T1)
- [x] **T-0303** — JSON Schema rejects unknown fields (phase: P3.T1)
- [x] **T-0304** — JSON Schema rejects invalid `cidr` format in vars (phase: P3.T1)
- [x] **T-0305** — `ChecklistLoader` expands `defaults` into each test case (phase: P3.T2)
- [x] **T-0306** — `ChecklistLoader` normalizes IDs: `1` → `TC-001` (phase: P3.T2)
- [x] **T-0307** — `spec_hash` changes when spec content changes (phase: P3.T2)
- [x] **T-0308** — `spec_hash` stable when spec content is identical (phase: P3.T2)
- [x] **T-0309** — `DependencyResolver` topological sort: linear chain A→B→C (phase: P3.T3)
- [x] **T-0310** — `DependencyResolver` rejects cycle A→B→A (phase: P3.T3)
- [x] **T-0311** — `DependencyResolver.ready_tasks()` returns only unblocked tasks (phase: P3.T3)
- [x] **T-0312** — `DependencyResolver.ready_tasks()` unblocks children when parent PASS (phase: P3.T3)
- [x] **T-0313** — Producer dry-run mode enqueues 0 tasks, prints plan (phase: P3.T4)
- [x] **T-0314** — Producer resumability: re-submit same `run_id` → 0 new enqueues (phase: P3.T6)
- [x] **T-0315** — Gap items enqueued with `expected.type: manual` (phase: P3.T7)
- [x] **T-0316** — Action registry maps `create_vm` to module `vm` (phase: P3.T2)
- [x] **T-0317** — Action registry rejects unknown action name (phase: P3.T2)
- [x] **T-0318** — Action registry infers `default_depends_on_actions` correctly (phase: P3.T3)
- [ ] **T-0319** — Gap action with `module: null` routes to `api_fallback` executor (phase: P7.T1)
- [x] **T-0320** — `TemplateRenderer` resolves `${context.*}` refs; identical (TaskSpec, context) yields identical `resolved_vars` and `spec_hash` (phase: P3.T2.1)
- [x] **T-0321** — `TemplateRenderer` rejects non-deterministic context (e.g. timestamp) in vars (phase: P3.T2.1)

### Validators (`src/hc/validator/`) — target ≥ 80%

- [ ] **T-0401** — `TFStateValidator` `equals` assertion passes on match (phase: P4.T2)
- [ ] **T-0402** — `TFStateValidator` `equals` assertion fails on mismatch (phase: P4.T2)
- [ ] **T-0403** — `TFStateValidator` `contains` assertion (phase: P4.T2)
- [ ] **T-0404** — `TFStateValidator` `regex_match` assertion (phase: P4.T2)
- [ ] **T-0405** — `TFStateValidator` `present` / `absent` assertions (phase: P4.T2)
- [ ] **T-0406** — `TFStateValidator` returns `FAIL` with JSONPath that doesn't exist (phase: P4.T2)
- [ ] **T-0407** — `InVMValidator` SSH: command returns expected stdout (phase: P4.T3)
- [ ] **T-0408** — `InVMValidator` SSH: connection timeout → `INCONCLUSIVE` (phase: P4.T3)
- [ ] **T-0409** — `InVMValidator` WinRM: command returns expected stdout (phase: P4.T3)
- [ ] **T-0410** — `InVMValidator` WinRM: auth failure → `FAIL` (phase: P4.T3)
- [ ] **T-0411** — `InVMValidator` `file_exists` probe (phase: P4.T3)
- [ ] **T-0412** — `APIProbeValidator` HTTP 200 + body match → `PASS` (phase: P4.T4)
- [ ] **T-0413** — `APIProbeValidator` HTTP 503 → retry then `FAIL` (phase: P4.T4)
- [ ] **T-0414** — `APIProbeValidator` TLS verify failure → `FAIL` (phase: P4.T4)
- [ ] **T-0415** — `CompositeValidator` AND: all pass → `PASS` (phase: P4.T5)
- [ ] **T-0416** — `CompositeValidator` AND: one fail → `FAIL` (phase: P4.T5)
- [ ] **T-0417** — `CompositeValidator` OR: one pass → `PASS` (phase: P4.T5)
- [ ] **T-0418** — `ManualValidator` always returns `INCONCLUSIVE` with note (phase: P4.T6)

### Reporter (`src/hc/reporter/`) — target ≥ 75%

- [ ] **T-0601** — Markdown renderer produces valid table with all columns (phase: P6.T2)
- [ ] **T-0602** — Markdown renderer orders TCs by category then `tc_id` (phase: P6.T2)
- [ ] **T-0603** — HTML renderer produces self-contained file (no external deps) (phase: P6.T3)
- [ ] **T-0604** — HTML collapsible sections contain TF diff per row (phase: P6.T3)
- [ ] **T-0605** — JSON renderer output validates against a stable JSON Schema (phase: P6.T4)
- [ ] **T-0606** — JSON renderer version field is present (phase: P6.T4)
- [ ] **T-0607** — Snapshot test: MD output matches golden file (phase: P6.T7)
- [ ] **T-0608** — Snapshot test: HTML output matches golden file (phase: P6.T7)
- [ ] **T-0609** — Snapshot test: JSON output matches golden file (phase: P6.T7)

### CLI (`src/hc/cli/`) — target ≥ 70%

- [x] **T-0701** — `cli --help` prints usage without import errors (phase: P0.T2)
- [x] **T-0702** — `cli queue stats` outputs pending/PEL/DLQ counts (phase: P1.T7)
- [x] **T-0703** — `cli queue peek` shows next N entries (phase: P1.T7)
- [x] **T-0704** — `cli dlq list` shows DLQ entries with timestamps (phase: P1.T7)
- [x] **T-0705** — `cli dlq replay <id>` re-enqueues entry, returns new entry_id (phase: P1.T7)
- [x] **T-0706** — `cli dlq replay <bad-id>` returns clear error (phase: P1.T7)
- [ ] **T-0707** — `cli report render` produces 3 files (md, html, json) (phase: P6.T5)
- [ ] **T-0708** — `cli wait` exits 0 when all PASS (phase: P6.T6)
- [ ] **T-0709** — `cli wait` exits 1 when any FAIL (phase: P6.T6)
- [ ] **T-0710** — `cli wait` exits 1 on timeout (phase: P6.T6)
- [ ] **T-0711** — `cli teardown` destroys in reverse dependency order (phase: P7)

### Config (`src/hc/config/`) — target ≥ 90%

- [ ] **T-0801** — Config loads from env vars (phase: P0.T7)
- [ ] **T-0802** — Config raises on missing required var (`FPTCLOUD_TOKEN`) (phase: P0.T7)
- [ ] **T-0803** — Config applies defaults for optional vars (`HC_WORKER_COUNT=4`) (phase: P0.T7)
- [ ] **T-0804** — Config validates types: `HC_REAPER_IDLE_MS` must be int (phase: P0.T7)

---

## Integration tests

Require running Redis + Postgres containers. No FPT Cloud access.
Run with: `make test-integration` (pytest marker: `@pytest.mark.integration`)

### Queue integration

- [x] **T-1001** — 1000 tasks enqueued → 1000 acked → 0 lost (phase: P1.T8)
- [x] **T-1002** — 100 duplicate enqueues → 0 extra entries in stream (phase: P1.T8)
- [x] **T-1003** — Consumer group creation is idempotent (phase: P1.T8)
- [x] **T-1004** — Two consumers round-robin tasks from one stream (phase: P1.T8)
- [x] **T-1005** — Scheduler + Reaper coexist without race conditions (phase: P1.T8)

### Executor integration

- [x] **T-1101** — `terraform init` succeeds with real provider (offline cache) (phase: P2.T8)
- [x] **T-1102** — `terraform validate` passes for every module in `modules/` (phase: P2.T8)
- [x] **T-1103** — `terraform fmt -check` passes for every module (phase: P2.T8)

### Producer → Queue → Worker pipeline

- [ ] **T-1201** — Producer enqueues → worker consumes → Postgres row inserted (phase: P5.T8)
- [ ] **T-1202** — Producer enqueues dependent tasks → child waits for parent (phase: P5.T8)
- [ ] **T-1203** — Worker crash mid-task → Reaper reclaims → second worker finishes (phase: P5.T8)
- [ ] **T-1204** — Worker encounters `transient` error → retry succeeds on attempt 2 (phase: P5.T4)
- [ ] **T-1205** — Worker encounters `quota` error → straight to DLQ (phase: P5.T4)
- [ ] **T-1206** — Live runner places non-ready post-apply resources in a pending queue and polls before verdict (phase: P5.T9)
- [ ] **T-1207** — Live runner writes terminal failures to an error queue with resource and reason (phase: P5.T9)
- [ ] **T-1208** — Live runner prevents resource conflicts with per-group locks and releases locks after destroy (phase: P5.T9)
- [ ] **T-1209** — Live runner disables quota prechecks, classifies provider quota exceeded, retains resources, stops without attempting later images, and reports `user_action_required=True` (phase: P5.T9)
- [ ] **T-1210** — Live runner selects `Premium-SSD` by exact name, passes provider-facing `id` as VM `storage_policy_id`, logs `id_db` only for debugging, and does not select `Premium-SSD-4000` by partial match (phase: P5.T9)
- [ ] **T-1211** — If async validator: provisional PASS emitted by worker, final PASS confirmed by validator consumer (phase: P5.T10)
- [ ] **T-1212** — If async validator: validator timeout → verdict becomes INCONCLUSIVE, not stuck (phase: P5.T10)
- [ ] **T-1213** — After a Redis wipe, Producer re-reads `hc_tasks.state` and re-enqueues only PENDING/FAILED entries; no verdict lost (C-015) (phase: P3.T6)

### Database integration

- [ ] **T-1301** — Migration creates all tables and indexes (phase: P0.T4)
- [ ] **T-1302** — `hc_tasks.state` transition: pending → running → passed (phase: P5.T3)
- [ ] **T-1303** — `hc_tasks.state` transition: pending → running → failed → dead (phase: P5.T3)
- [ ] **T-1304** — `hc_attempts` records every attempt with correct timestamps (phase: P5.T3)
- [ ] **T-1305** — Concurrent inserts to `hc_attempts` for different tasks don't deadlock (phase: P5.T3)

---

## End-to-end tests (E2E)

Require real FPT Cloud tenant. Gated by `HC_LIVE_TESTS=1` env var.
Run with: `make test-e2e` (pytest marker: `@pytest.mark.live`)

### Compute

- [ ] **T-2001** — TC-001: Create subnet 172.26.221.0/24 → PASS (phase: P5.T8)
- [ ] **T-2002** — TC-002: Create VM Windows Server 2012 → login OK → PASS (phase: P5.T8)
- [ ] **T-2003** — TC-003: Create VM Windows Server 2016 → login OK → PASS (phase: P5.T8)
- [ ] **T-2004** — TC-004: Create VM Windows Server 2019 → login OK → PASS (phase: P5.T8)
- [ ] **T-2005** — TC-005: Create VM Windows Server 2022 → login OK → PASS (phase: P5.T8)
- [ ] **T-2006** — TC-006: Create VM Ubuntu 16.04 → login OK → PASS (phase: P5.T8)
- [ ] **T-2007** — TC-007: Create VM Ubuntu 18.04 → login OK → PASS (phase: P5.T8)
- [ ] **T-2008** — TC-008: Create VM Ubuntu 20.04 → login OK → PASS (phase: P5.T8)
- [ ] **T-2009** — TC-009: Create VM Ubuntu 22.04 → login OK → PASS (phase: P5.T8)
- [ ] **T-2010** — TC-010: Resize VM to 4vCPU/4GB → in-VM sees new config → PASS (phase: P5.T8)
- [ ] **T-2011** — TC-011: Hot-add OS disk 40→80GB → `lsblk` shows 80GB → PASS (phase: P5.T8)
- [ ] **T-2012** — TC-012: Attach 40GB data disk → disk visible in OS → PASS (phase: P5.T8)
- [ ] **T-2013** — TC-013: Delete VM → attached disk survives → PASS (phase: P5.T8)
- [ ] **T-2014** — TC-014: VM power schedule (gap) → INCONCLUSIVE (phase: P7.T1)
- [ ] **T-2015** — TC-015: Create snapshot → PASS (phase: P7.T1)
- [ ] **T-2016** — TC-016: Revert snapshot → PASS (phase: P7.T1)

### Networking

- [ ] **T-2101** — TC-017: Assign public IP → accessible → PASS (phase: P5.T8)
- [ ] **T-2102** — TC-018: NSG inbound RDP+SSH only → port 3389,22 open, others blocked → PASS (phase: P5.T8)
- [ ] **T-2103** — TC-019: NSG outbound 80,443 → VM can curl https → PASS (phase: P5.T8)
- [ ] **T-2104** — TC-020: Create additional subnet 10.136.10.0/24 → PASS (phase: P5.T8)
- [ ] **T-2105** — TC-021: Add NIC from new subnet → visible in OS → PASS (phase: P5.T8)

### Backup & Recovery

- [ ] **T-2201** — TC-022: Create backup → job succeeds → PASS (phase: P7.T1)
- [ ] **T-2202** — TC-023: Restore VM → `testbackup-*.txt` exists → PASS (phase: P7.T1)

### Object storage

- [ ] **T-2301** — TC-024: Create bucket → PASS (phase: P5.T8)
- [ ] **T-2302** — TC-025: Upload file → openable in browser → PASS (phase: P5.T8)
- [ ] **T-2303** — TC-026: Connect via S3 endpoint (gap) → INCONCLUSIVE (phase: P7.T1)
- [ ] **T-2304** — TC-027: Delete file → PASS (phase: P5.T8)
- [ ] **T-2305** — TC-028: Delete bucket → PASS (phase: P5.T8)

---

## Chaos tests

Scripted fault injection. Verify auto-recovery properties.
Run with: `make test-chaos` (pytest marker: `@pytest.mark.chaos`)

- [ ] **T-3001** — Kill 1 of 4 workers mid-apply → task completes via another worker (phase: P7.T5)
- [ ] **T-3002** — Kill all 4 workers → restart → all pending tasks eventually complete (phase: P7.T5)
- [ ] **T-3003** — Redis restart (SIGTERM + up) → workers reconnect, no task loss (phase: P7.T5)
- [ ] **T-3004** — Network partition worker↔redis for 30s → tasks resume after heal (phase: P7.T5)
- [ ] **T-3005** — Postgres restart → workers buffer results, flush on reconnect (phase: P7.T5)
- [ ] **T-3006** — FPT Cloud API throttle (simulate 429) → backoff + retry → eventual PASS (phase: P7.T5)
- [ ] **T-3007** — Producer killed mid-enqueue → re-run same `run_id` → no duplicates (phase: P7.T5)
- [ ] **T-3008** — Reaper and Scheduler crash simultaneously → restart → queue converges (phase: P7.T5)

---

## Regression tests

Added when bugs are found. Each links to the issue that spawned it.

### (empty — add entries as bugs are discovered)

Template for new regression tests:

```markdown
- [ ] **T-9NNN** — <description> (issue: #NNN, phase: P<N>.T<M>)
```

---

## Coverage report automation

CI generates a coverage report after each phase. The workflow should:

1. Run `make test-unit test-integration` with `--cov` flags.
2. Parse `coverage.py` JSON output.
3. Compare per-component coverage against the targets table above.
4. If any component is below target, the CI step **warns** (not fails)
   during phases 0–4 and **fails** starting phase 5.
5. Upload `htmlcov/` as a CI artifact alongside the test results.

### Makefile targets

```makefile
test-unit:
	pytest tests/unit -m unit --cov=src/hc --cov-report=json --cov-report=html

test-integration:
	pytest tests/integration -m integration --cov=src/hc --cov-report=json --cov-report=html --cov-append

test-e2e:
	HC_LIVE_TESTS=1 pytest tests/e2e -m live --cov=src/hc --cov-report=json --cov-report=html --cov-append

test-chaos:
	pytest tests/chaos -m chaos -x -v

test-all: test-unit test-integration

coverage-check:
	python scripts/check_coverage.py --targets specs/04-TESTS.md --report coverage.json
```

---

## How to read this file after a session

```bash
# Quick dashboard
echo "=== Test Coverage Dashboard ==="
done=$(grep -cE '^\- \[x\]' specs/04-TESTS.md)
total=$(grep -cE '^\- \[.\]' specs/04-TESTS.md)
blocked=$(grep -cE '^\- \[!\]' specs/04-TESTS.md)
pending=$((total - done - blocked))
pct=$((done * 100 / total))
echo "Total:   $total"
echo "Done:    $done ($pct%)"
echo "Pending: $pending"
echo "Blocked: $blocked"
echo ""
echo "=== By Category ==="
for cat in "Unit" "Integration" "E2E" "Chaos"; do
  d=$(sed -n "/^## ${cat}/,/^## /p" specs/04-TESTS.md | grep -cE '^\- \[x\]')
  t=$(sed -n "/^## ${cat}/,/^## /p" specs/04-TESTS.md | grep -cE '^\- \[.\]')
  if [ "$t" -gt 0 ]; then
    echo "  $cat: $d/$t ($((d * 100 / t))%)"
  fi
done
```
