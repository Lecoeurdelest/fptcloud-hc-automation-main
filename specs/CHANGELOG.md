# Spec Changelog

Durable, tracked record of amendments to the specification set under `specs/`.
This is the canonical changelog for spec changes; the root `log.html` is a
generated run-log view (rendered from JSON run events, overwritten each runner
execution) and is **not** a changelog.

Each entry lists the amendment, the files it touched, a one-line summary, and a
rationale tag.

---

## 2026-06-16 — Architecture improvements (Atlassian OSB learnings)

Spec-only session. Six amendments applied, zero skipped. Two new constraints,
one new architecture component, one new config-driven extension mechanism, plus
task/test coverage. No code was written.

> Renumbering note: the source prompt proposed IDs that were already in use
> (`C-012`, `C-013`, `P5.T9`, `T-1206`, `T-1207`). Per the operator's standing
> instruction, existing IDs were preserved and the new items renumbered to the
> next free IDs. The mapping is called out per amendment below.

### Amendment 1 — Verdict durability (C-015)
- **Files:** [01-REQUIREMENTS.md](01-REQUIREMENTS.md), [00-ARCHITECTURE.md](00-ARCHITECTURE.md)
- **Summary:** Added constraint **C-015** making Postgres the authoritative
  verdict store and the system resumable from Postgres alone after a Redis wipe;
  added a §2.9 cross-reference to it. (Renumbered from proposed `C-012`, which
  already exists as "Reactive-only quota model".)
- **Rationale:** `atlassian-osb-learning`

### Amendment 2 — Template Renderer layer (§2.5.1)
- **Files:** [00-ARCHITECTURE.md](00-ARCHITECTURE.md), [03-TASKS.md](03-TASKS.md)
- **Summary:** Added architecture **§2.5.1 Template Renderer** (static → interpolated
  → plugin-driven) between ChecklistLoader and TerraformExecutor, with a determinism
  requirement; added the renderer step to the §3 data-flow diagram; added subtask
  **P3.T2.1** (interpolation) and a Phase-2 note that the renderer ships as a no-op
  pass-through.
- **Rationale:** `atlassian-osb-learning`

### Amendment 3 — Bake provider into the Docker image
- **Files:** [02-INFRASTRUCTURE.md](02-INFRASTRUCTURE.md), [01-REQUIREMENTS.md](01-REQUIREMENTS.md), [03-TASKS.md](03-TASKS.md)
- **Summary:** Added a `terraform providers mirror` build stage so workers use a
  baked-in provider mirror and never download the provider at runtime; reconciled
  §6 plugin-cache wording, §6.2 (rebuild on version bump), §7 (registry egress is
  build-time only), and C-002 (mirror enforces the pin); pinned the snippet to
  Terraform `1.9.8`; added a Phase-0 note that the mirror stage must be validated
  by CI (P0.T3 stays `[x]`).
- **Rationale:** `atlassian-osb-learning`

### Amendment 4 — Async validator evaluation point (P5.T10)
- **Files:** [03-TASKS.md](03-TASKS.md), [04-TESTS.md](04-TESTS.md)
- **Summary:** Added subtask **P5.T10** to evaluate async vs. inline validation via
  a 1-page ADR (`docs/adr/001-async-validator.md`); added integration tests **T-1211**
  and **T-1212** for the async path. (Renumbered from proposed `P5.T9` /
  `T-1206`/`T-1207`, all of which already exist as live-runner items.)
- **Rationale:** `atlassian-osb-learning`

### Amendment 5 — Checklist authoring complexity (C-016)
- **Files:** [01-REQUIREMENTS.md](01-REQUIREMENTS.md), [00-ARCHITECTURE.md](00-ARCHITECTURE.md), [03-TASKS.md](03-TASKS.md)
- **Summary:** Added constraint **C-016** — a QA engineer can add a checklist entry
  by copy-and-edit; module is inferred from `spec.action` via the action registry,
  and dependency wiring is inferred unless `depends_on` overrides. Updated the §5
  task-schema example to drop authored `spec.module`; extended **P3.T2**.
  (Renumbered from proposed `C-013`, which already exists as "Inventory read direct;
  deletion via Terraform".)
- **Rationale:** `atlassian-osb-learning`

### Amendment 6 — Action Registry (§5.1)
- **Files:** [00-ARCHITECTURE.md](00-ARCHITECTURE.md), [03-TASKS.md](03-TASKS.md), [04-TESTS.md](04-TESTS.md)
- **Summary:** Added **§5.1 Action Registry** (`config/action_registry.yml`) as the
  single config-driven wiring point for action→module, validators, resource-key
  templates, dependency defaults, and `module: null` gap items routed to
  `api_fallback`; extended **P3.T2** to load+validate the registry; added tests
  **T-0316–T-0319**. Resolves Amendment 5's forward reference to §5.1.
- **Rationale:** `atlassian-osb-learning`

### Skipped
- None.

### New IDs introduced this session
- **Constraints:** C-015, C-016
- **Architecture:** §2.5.1 (Template Renderer), §5.1 (Action Registry)
- **Subtasks:** P3.T2.1, P5.T10
- **Tests:** T-0316, T-0317, T-0318, T-0319, T-1211, T-1212
