# fptcloud-hc-automation

Spec-driven automation framework for FPT Cloud tenant health-check.

Each checkpoint in the QA checklist is enqueued as a unique, retryable task.
Workers materialize the checkpoint via Terraform (`fpt-corp/fptcloud` provider),
assert the expected state, and emit a structured result. Failed dequeues are
auto-recovered through Redis Streams consumer-group semantics with a Dead
Letter Queue (DLQ) for poison messages.

This repository contains **only specifications**. No source code is committed
here; code lands in subsequent phases per `specs/03-TASKS.md`.

## Stack

- Python 3.11 (CPython, asyncio + threading hybrid worker pool)
- Terraform >= 1.6 CLI, invoked through `python-terraform` SDK
- `fpt-corp/fptcloud` Terraform provider (Registry)
- Redis 7 — Streams for queue, ZSET for unique-key index, Hash for DLQ payload
- Postgres 16 — durable result store (optional in dev, required in CI)
- Docker / Docker Compose for local topology
- GitHub Actions for CI execution

## Project layout

```
fptcloud-hc-automation/
├── README.md                  ← you are here
└── specs/
    ├── 00-ARCHITECTURE.md     ← components, data flow, queue design
    ├── 01-REQUIREMENTS.md     ← functional, non-functional, constraints
    ├── 02-INFRASTRUCTURE.md   ← how to stand up the runtime
    └── 03-TASKS.md            ← phased subtask backlog with DoD
```

## How to read this repo

Read in order:

1. `01-REQUIREMENTS.md` — what we are building and the rules of the game.
2. `00-ARCHITECTURE.md` — how the pieces fit.
3. `02-INFRASTRUCTURE.md` — how to stand it up.
4. `03-TASKS.md` — what to build, in what order.

Each phase in `03-TASKS.md` is independently shippable. Do **not** begin phase
N+1 until phase N's *Definition of Done* is green. Each phase ends with a
review gate; the evaluator signs off before the next phase opens.

## Naming convention

- `FR-NNN` — functional requirement
- `NFR-NNN` — non-functional requirement
- `C-NNN` — constraint
- `TC-NNN` — test case (from QA checklist)
- `P{N}.T{M}` — phase N, task M (e.g. `P2.T3`)
