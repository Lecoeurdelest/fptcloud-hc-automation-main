"""Postgres initialization helpers."""

from __future__ import annotations

import os

import psycopg


DDL = """
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
  state         TEXT NOT NULL,
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
  verdict       TEXT,
  error_class   TEXT,
  error_message TEXT,
  tf_plan_json  JSONB,
  tf_state_json JSONB,
  validator_log TEXT
);

CREATE INDEX IF NOT EXISTS idx_hc_tasks_run_state
  ON hc_tasks (run_id, state);
"""


def migrate(database_url: str | None = None) -> None:
    """Apply the idempotent initialization DDL."""
    dsn = database_url or os.environ.get("DATABASE_URL")
    if not dsn:
        msg = "DATABASE_URL is required for db migrate"
        raise ValueError(msg)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
