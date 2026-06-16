# 02 — Infrastructure

This document specifies how the runtime is stood up. It is **declarative**:
no command-line incantations are required beyond `docker compose up` and the
seven environment variables enumerated in §3.

## 1. Topology

```
┌──────────────────────── docker-compose network: hc-net ─────────────────────┐
│                                                                              │
│   ┌──────────┐   ┌──────────┐   ┌──────────┐    ┌──────────┐                 │
│   │ worker-1 │   │ worker-2 │   │ worker-3 │    │ worker-4 │                 │
│   └────┬─────┘   └────┬─────┘   └────┬─────┘    └────┬─────┘                 │
│        │              │              │               │                       │
│        └──────────────┼──────────────┼───────────────┘                       │
│                       ▼              ▼                                       │
│                  ┌─────────┐   ┌──────────┐                                  │
│                  │  redis  │   │ postgres │                                  │
│                  │   :6379 │   │  :5432   │                                  │
│                  └─────────┘   └──────────┘                                  │
│                                                                              │
│        ┌──────────┐   ┌──────────┐                                           │
│        │ producer │   │  reaper  │                                           │
│        └──────────┘   └──────────┘                                           │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
                       (egress to FPT Cloud API
                          + tenant network)
```

Local dev runs the entire box on `docker compose`. CI runs the same compose
file in a GitHub Actions runner. Production-grade deployment (K8s) is a
phase-7 stretch goal.

## 2. Services

| Service        | Image / build context             | Purpose                              |
|----------------|------------------------------------|---------------------------------------|
| `redis`        | `redis:7-alpine`                   | Streams, ZSET, locks                  |
| `postgres`     | `postgres:16-alpine`               | Result store, optional TF backend     |
| `producer`     | build `./` target=producer         | One-shot: load checklist, enqueue     |
| `worker`       | build `./` target=worker (scale N) | Long-running consumer                 |
| `reaper`       | build `./` target=reaper           | Idle-PEL claimer + scheduled retries  |
| `cli` (oneshot)| build `./` target=cli              | DLQ inspect/replay, report render     |

All Python services share a single multi-stage `Dockerfile` selecting the
entrypoint via `target=`. Base image: `python:3.11-slim-bookworm`. Terraform
binary installed via `RUN curl … && unzip …` pinned by SHA256.

The Dockerfile shall include a dedicated build stage that pre-downloads the
`fpt-corp/fptcloud` provider binary using `terraform providers mirror`. The
mirrored plugin directory is copied into the worker image at build time and
exposed via `TF_PLUGIN_CACHE_DIR`. Workers MUST NOT download the provider at
runtime (`terraform init` uses the local mirror). This eliminates ~30s of
network latency per task and removes a runtime dependency on
`registry.terraform.io` availability.

```dockerfile
# Stage: provider-cache (built once, cached in CI layer)
FROM hashicorp/terraform:1.9.8 AS tf-cache
COPY modules/ /tmp/modules/
RUN terraform providers mirror \
      -platform=linux_amd64 \
      /tf-plugins \
    && du -sh /tf-plugins/*

# Stage: worker (inherits cached provider)
COPY --from=tf-cache /tf-plugins /usr/share/terraform/plugins
ENV TF_PLUGIN_CACHE_DIR=/usr/share/terraform/plugins
```

## 3. Environment variables

The single source of secrets is the host's environment (or `docker compose`
`.env` for local dev). The compose file passes them through with
`environment:` lists — no `.env` is copied into images.

Required:

```
FPTCLOUD_API_URL=https://console-api.fptcloud.com/api
FPTCLOUD_REGION=HAN-1
FPTCLOUD_TENANT_NAME=<tenant>
FPTCLOUD_TOKEN=<bearer>
VPC_ID=<vpc-uuid>
REDIS_URL=redis://redis:6379/0
DATABASE_URL=postgresql://hc:hc@postgres:5432/hc
```

Optional:

```
HC_WORKER_COUNT=4
HC_REAPER_IDLE_MS=300000
HC_MAX_ATTEMPTS=3
HC_LOG_LEVEL=INFO
HC_RUN_ID=<auto>
```

## 4. Secrets handling

- Local dev: `direnv` + `.envrc` (git-ignored) or `docker compose --env-file`.
- CI: GitHub Actions encrypted secrets → `env:` in the workflow → forwarded
  into the compose file.
- Production: out of scope for v1; Vault sidecar is the intended path.

Terraform state files written under `./runs/<run_id>/<task_id>/` are
considered sensitive (they contain VM IPs, credentials hashes). The compose
volume that holds them is named `hc-runs` and is bind-mounted only to the
worker and CLI containers, never to anything that ships logs externally.

## 5. Postgres schema (DDL fragment)

```sql
CREATE TABLE IF NOT EXISTS hc_runs (
  run_id        TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ,
  checklist_sha TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hc_tasks (
  task_id       TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES hc_runs(run_id),
  tc_id         TEXT NOT NULL,
  category      TEXT NOT NULL,
  spec          JSONB NOT NULL,
  state         TEXT NOT NULL,  -- pending|running|passed|failed|dead
  attempts      INT  NOT NULL DEFAULT 0,
  enqueued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hc_attempts (
  id            BIGSERIAL PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES hc_tasks(task_id),
  attempt       INT  NOT NULL,
  worker_id     TEXT NOT NULL,
  started_at    TIMESTAMPTZ NOT NULL,
  finished_at   TIMESTAMPTZ,
  verdict       TEXT,           -- pass|fail|inconclusive
  error_class   TEXT,
  error_message TEXT,
  tf_plan_json  JSONB,
  tf_state_json JSONB,
  validator_log TEXT
);

CREATE INDEX IF NOT EXISTS idx_hc_tasks_run_state
  ON hc_tasks (run_id, state);
```

## 6. Terraform layout

```
modules/                           ← versioned, hand-written modules
├── subnet/
│   ├── main.tf
│   ├── variables.tf
│   └── outputs.tf
├── vm/
├── disk/
├── security_group/
├── floating_ip/
└── object_storage/

runs/<run_id>/<task_id>/           ← generated per task, ephemeral
├── main.tf                        ← module call only
├── terraform.tfvars.json
├── .terraform/                    ← provider resolved from baked mirror (TF_PLUGIN_CACHE_DIR)
└── terraform.tfstate              ← local backend, scoped to this task
```

Runtime workers MUST use the provider mirror baked into the image at build time
(`TF_PLUGIN_CACHE_DIR`, see §2): `terraform init` resolves the `fpt-corp/fptcloud`
provider from that local mirror and performs no registry download. Any provider
download happens only at Docker image-build time, when the mirror is created or
refreshed — never during a normal worker run.

### 6.1 Backend choice

- **Local file backend** per task workspace — chosen for v1. The Postgres
  result store is the durable record; the `.tfstate` file is treated as a
  cache and re-derivable from the cloud's actual state if necessary.
- A future migration to the `pg` backend (one schema per run) is straightforward
  and noted as a phase-7 enhancement.

### 6.2 Module versioning

Each module folder includes a `versions.tf` pinning the provider:

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    fptcloud = {
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }
  }
}
```

The version constraint is updated only through a PR that bumps it in all
modules atomically. Provider version bumps require a Docker image rebuild to
refresh the cached mirror (see §2).

## 7. Network requirements

- The host running `docker compose` must have **outbound HTTPS** to
  `FPTCLOUD_API_URL`. Outbound HTTPS to the Terraform Registry
  (`registry.terraform.io`) and provider GitHub release assets is required
  **only at image-build time** to create or refresh the provider mirror (§2),
  not for normal worker runtime — runtime workers resolve the provider from the
  baked-in mirror.
- For **in-VM validation**, workers must reach provisioned VMs. Options:
  1. Run the stack on a host inside the tenant's VPC.
  2. Run on the public internet and target VMs with public IPs +
     temporary SG rules opening 22/3389 from the worker's egress IP.
  3. Tunnel via a jump host — the worker SSHes into the jump, then into
     the VM. The jump host's address is `HC_JUMP_HOST` env var; SSH key
     mounted as a Docker secret.

Option 2 is the default for the QA-checklist runs because the checklist
itself exercises public-IP + SG flows.

## 8. Bootstrapping (operator runbook)

```bash
# 1. Clone & configure
git clone <repo>
cd fptcloud-hc-automation
cp .env.example .env
$EDITOR .env                       # fill the 7 FPTCLOUD_* vars

# 2. Bring up the runtime
docker compose up -d redis postgres
docker compose run --rm migrate    # applies Postgres DDL

# 3. Launch workers
docker compose up -d --scale worker=4 worker reaper

# 4. Submit a run
docker compose run --rm producer \
  --checklist /workspace/checklist.yml \
  --run-id smoke-$(date +%Y%m%d-%H%M%S)

# 5. Monitor
docker compose logs -f worker
docker compose run --rm cli queue stats
docker compose run --rm cli dlq list

# 6. Generate report (when state=finished)
docker compose run --rm cli report render --run-id <id>
```

## 9. Teardown

`teardown` is a special run that walks the result store in reverse
dependency order and runs `terraform destroy` per task workspace. It is
**explicit** — no auto-teardown — to prevent destroying a tenant by
accident.

```bash
docker compose run --rm cli teardown --run-id <id> --yes
```

## 10. CI integration (GitHub Actions outline)

```yaml
jobs:
  health-check:
    runs-on: ubuntu-latest
    env:
      FPTCLOUD_API_URL: ${{ secrets.FPTCLOUD_API_URL }}
      FPTCLOUD_REGION: ${{ secrets.FPTCLOUD_REGION }}
      FPTCLOUD_TENANT_NAME: ${{ secrets.FPTCLOUD_TENANT_NAME }}
      FPTCLOUD_TOKEN: ${{ secrets.FPTCLOUD_TOKEN }}
      VPC_ID: ${{ secrets.VPC_ID }}
    steps:
      - uses: actions/checkout@v4
      - run: docker compose up -d redis postgres
      - run: docker compose run --rm migrate
      - run: docker compose up -d --scale worker=4 worker reaper
      - run: docker compose run --rm producer --checklist checklist.yml --run-id ci-${{ github.run_id }}
      - run: docker compose run --rm cli wait --run-id ci-${{ github.run_id }} --timeout 3600
      - run: docker compose run --rm cli report render --run-id ci-${{ github.run_id }} --out report/
      - uses: actions/upload-artifact@v4
        with:
          name: hc-report
          path: report/
      - if: always()
        run: docker compose run --rm cli teardown --run-id ci-${{ github.run_id }} --yes
```

## 11. Quotas to keep in mind

The QA checklist provisions in one run:

- 8 VMs (4 Windows + 4 Ubuntu) — each 2 vCPU / 2 GB / 40 GB.
- 1 hot-add disk grow + 1 attached disk (40 GB) → +80 GB block storage.
- 1 floating IP.
- 1 security group (≥ 4 rules).
- 1 object storage bucket.
- Backup & snapshot artifacts.

Verify quota headroom before triggering a run. The Producer's pre-flight
check shall query `fptcloud_*` data sources to estimate consumption and
abort with a clear error if any limit is < 1.2× the demand.
