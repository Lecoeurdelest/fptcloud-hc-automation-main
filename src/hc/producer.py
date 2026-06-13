"""Health-check job producer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import redis

from hc.config.settings import QueueSettings
from hc.models.task import EnqueueResult, TaskSpec, compute_spec_hash
from hc.queue.redis_queue import RedisQueue
from hc.reporter.html_log import HtmlProgressLog

DEFAULT_MAX_JOBS = 2


class ProducerError(Exception):
    """Raised when health-check jobs cannot be built from the input spec."""


def create_health_checks(
    *,
    queue: RedisQueue,
    spec_path: str | Path,
    run_id: str,
    tenant_id: str,
    log_path: str | Path = "log.html",
    max_jobs: int = DEFAULT_MAX_JOBS,
) -> dict[str, int]:
    """Load a JSON health-check spec and enqueue at most ``max_jobs`` tasks."""
    if max_jobs < 1:
        msg = "max_jobs must be at least 1"
        raise ProducerError(msg)

    progress = HtmlProgressLog(log_path)
    progress.record("load_spec", "started", f"Reading health-check spec from {spec_path}")
    raw_checks = _load_checks(spec_path)
    progress.record("load_spec", "done", f"Loaded {len(raw_checks)} health-check definitions")

    progress.record("build_tasks", "started", "Normalizing health-check definitions")
    tasks = [_task_from_check(run_id, tenant_id, check) for check in raw_checks]
    progress.record("build_tasks", "done", f"Built {len(tasks)} candidate tasks")

    selected = tasks[:max_jobs]
    skipped = max(0, len(tasks) - len(selected))
    if skipped:
        progress.record(
            "limit_jobs",
            "pending",
            f"Queued only {len(selected)} jobs now; {skipped} remain for a later run",
        )
    else:
        progress.record("limit_jobs", "done", f"{len(selected)} jobs fit within the limit")

    enqueued = 0
    duplicates = 0
    progress.record("enqueue", "started", f"Creating up to {max_jobs} health-check jobs")
    for task in selected:
        result = queue.enqueue(task)
        if result == EnqueueResult.ENQUEUED:
            enqueued += 1
            progress.record("enqueue", "enqueued", f"{task.tc_id}: {task.task_id}")
        else:
            duplicates += 1
            progress.record("enqueue", "duplicate", f"{task.tc_id}: {task.task_id}")

    progress.record(
        "complete",
        "done",
        f"Created {enqueued} new jobs, saw {duplicates} duplicates, deferred {skipped}",
    )
    return {
        "loaded": len(tasks),
        "enqueued": enqueued,
        "duplicates": duplicates,
        "deferred": skipped,
    }


def _load_checks(spec_path: str | Path) -> list[dict[str, Any]]:
    path = Path(spec_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProducerError(f"unable to read spec file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProducerError(f"invalid JSON in spec file: {exc}") from exc

    checks = data.get("checks") if isinstance(data, dict) else data
    if not isinstance(checks, list):
        msg = "health-check spec must be a JSON list or an object with a 'checks' list"
        raise ProducerError(msg)
    result: list[dict[str, Any]] = []
    for index, item in enumerate(checks, start=1):
        if not isinstance(item, dict):
            raise ProducerError(f"check #{index} must be an object")
        result.append(item)
    return result


def _task_from_check(run_id: str, tenant_id: str, check: dict[str, Any]) -> TaskSpec:
    tc_id = str(check.get("tc_id") or check.get("id") or "").strip()
    if not tc_id:
        msg = "each health-check definition must include 'tc_id' or 'id'"
        raise ProducerError(msg)

    spec = check.get("spec", check)
    if not isinstance(spec, dict):
        raise ProducerError(f"{tc_id}: spec must be an object")

    category = str(check.get("category", spec.get("category", "compute")))
    description = str(check.get("description", spec.get("description", "")))
    spec_hash = compute_spec_hash(spec)
    return TaskSpec(
        run_id=run_id,
        tc_id=tc_id,
        tenant_id=tenant_id,
        spec_hash=spec_hash,
        category=category,
        description=description,
        spec=spec,
    )


@click.command()
@click.option("--spec", "spec_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--run-id", required=True)
@click.option("--tenant-id", required=True)
@click.option("--max-jobs", default=DEFAULT_MAX_JOBS, show_default=True)
@click.option("--log-file", default="log.html", show_default=True, type=click.Path(dir_okay=False))
def main(
    spec_path: str,
    run_id: str,
    tenant_id: str,
    max_jobs: int,
    log_file: str,
) -> None:
    """Container entrypoint for creating health-check jobs."""
    settings = QueueSettings.from_env()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    result = create_health_checks(
        queue=RedisQueue(client, settings),
        spec_path=spec_path,
        run_id=run_id,
        tenant_id=tenant_id,
        max_jobs=max_jobs,
        log_path=log_file,
    )
    click.echo(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
