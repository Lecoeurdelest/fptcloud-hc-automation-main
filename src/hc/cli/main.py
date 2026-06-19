"""Health-check CLI."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import click
import redis

from hc.config.settings import QueueSettings
from hc.producer import DEFAULT_MAX_JOBS, ProducerError, create_health_checks
from hc.queue.redis_queue import RedisQueue


def _queue() -> RedisQueue:
    settings = QueueSettings.from_env()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    return RedisQueue(client, settings)


@click.group()
def cli() -> None:
    """FPT Cloud health-check automation CLI."""


@cli.command("doctor")
@click.option("--json", "as_json", is_flag=True, default=False, help="Print machine-readable JSON.")
def doctor(as_json: bool) -> None:
    """Check local CLI/runtime readiness without creating cloud resources."""
    from healthcheck import config, state  # noqa: PLC0415
    from healthcheck.spec_loader import load_spec  # noqa: PLC0415

    spec_ok = True
    spec_count = 0
    spec_error = ""
    try:
        spec_count = len(load_spec())
    except Exception as exc:  # noqa: BLE001
        spec_ok = False
        spec_error = str(exc)

    result = {
        "python": sys.version.split()[0],
        "terraform": shutil.which("terraform") or "",
        "spec_path": str(state.SPEC_PATH),
        "spec_loaded": spec_ok,
        "spec_stage_count": spec_count,
        "spec_error": spec_error,
        "runtime_config": {
            "path": config.RUNTIME_CONFIG_RESULT.get("path"),
            "found": config.RUNTIME_CONFIG_RESULT.get("found"),
            "loaded": config.RUNTIME_CONFIG_RESULT.get("loaded"),
            "error": config.RUNTIME_CONFIG_RESULT.get("error") or "",
        },
        "env": {
            requirement: "present" if config.env_present(requirement) else "missing"
            for requirement in state.REQUIRED_ENV_PRESENCE
        },
    }
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    click.echo(f"Python: {result['python']}")
    click.echo(f"Terraform: {result['terraform'] or 'missing'}")
    click.echo(f"Spec: {'ok' if spec_ok else 'failed'} ({spec_count} stages)")
    if spec_error:
        click.echo(f"Spec error: {spec_error}")
    cfg = result["runtime_config"]
    click.echo(
        f"Config: path={cfg['path']} found={cfg['found']} loaded={cfg['loaded']} "
        f"error={cfg['error'] or '<none>'}"
    )
    for name, status in result["env"].items():
        click.echo(f"{name}: {status}")


@cli.group("live")
def live() -> None:
    """Live spec-gated runner commands."""


@live.command("run")
@click.option("--stage", help="Run one stage id from specs/health-check.json.")
def live_run(stage: str | None) -> None:
    """Run live health checks and write log.json/log.html."""
    from healthcheck.runner import run  # noqa: PLC0415

    try:
        run(stage)
    except Exception as exc:  # noqa: BLE001
        click.echo(str(exc), err=True)
        sys.exit(1)


@live.command("view")
@click.argument("log_json", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--filter",
    "filter_mode",
    default="summary",
    show_default=True,
    help="View filter: summary, failed, blocked, queued, retained_resources, created_resources.",
)
def live_view(log_json: str, filter_mode: str) -> None:
    """Render a filtered table from a live-run log.json file."""
    from healthcheck.reporting import FILTER_CHOICES, render_table  # noqa: PLC0415

    if filter_mode not in FILTER_CHOICES:
        choices = ", ".join(FILTER_CHOICES)
        click.echo(f"unknown filter {filter_mode!r}; choose one of: {choices}", err=True)
        sys.exit(1)
    data = json.loads(Path(log_json).read_text(encoding="utf-8-sig"))
    click.echo(render_table(data, filter_mode))


@live.command("stages")
@click.option("--all", "include_all", is_flag=True, default=False, help="Include non-automated stages.")
def live_stages(include_all: bool) -> None:
    """List stage ids from specs/health-check.json."""
    from healthcheck.spec_loader import load_spec, runnable_spec  # noqa: PLC0415

    rows = []
    for stage in load_spec().values():
        runnable, reason = runnable_spec(stage)
        if include_all or runnable:
            rows.append(
                {
                    "id": stage.id,
                    "status": stage.automation_status,
                    "safe_for_daily_run": stage.safe_for_daily_run,
                    "runnable": runnable,
                    "reason": reason,
                }
            )
    click.echo(json.dumps(rows, indent=2))


@cli.group()
def queue() -> None:
    """Queue inspection commands."""


@queue.command("stats")
def queue_stats() -> None:
    """Show pending stream, PEL, DLQ, and scheduled depths."""
    q = _queue()
    stats = q.stats()
    click.echo(json.dumps(stats, indent=2))


@queue.command("peek")
@click.option("--count", default=10, help="Number of entries to show.")
def queue_peek(count: int) -> None:
    """Peek at the next N tasks in the stream."""
    q = _queue()
    entries = q.peek(count=count)
    click.echo(json.dumps(entries, indent=2))


@cli.group()
def dlq() -> None:
    """Dead-letter queue commands."""


@dlq.command("list")
@click.option("--count", default=50, help="Max entries to list.")
def dlq_list(count: int) -> None:
    """List DLQ entries with timestamps."""
    q = _queue()
    entries = q.list_dlq(count=count)
    click.echo(json.dumps(entries, indent=2))


@dlq.command("replay")
@click.argument("entry_id")
def dlq_replay(entry_id: str) -> None:
    """Replay a DLQ entry back into the main queue."""
    q = _queue()
    try:
        new_id = q.replay_dlq(entry_id)
    except ValueError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    click.echo(json.dumps({"new_entry_id": new_id}))


@cli.group()
def db() -> None:
    """Database initialization commands."""


@db.command("migrate")
def db_migrate() -> None:
    """Apply idempotent Postgres initialization DDL."""
    from hc.db import migrate  # noqa: PLC0415

    try:
        migrate()
    except Exception as exc:  # noqa: BLE001
        click.echo(str(exc), err=True)
        sys.exit(1)
    click.echo(json.dumps({"status": "ok"}))


@cli.group("health-checks")
def health_checks() -> None:
    """Health-check job commands."""


@health_checks.command("create")
@click.option("--spec", "spec_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--run-id", required=True, help="Run identifier used for task deduplication.")
@click.option("--tenant-id", required=True, help="Tenant identifier for the health-check run.")
@click.option(
    "--max-jobs",
    default=DEFAULT_MAX_JOBS,
    show_default=True,
    help="Maximum number of health-check jobs to create at a time.",
)
@click.option(
    "--log-file",
    default="log.html",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="HTML progress log path.",
)
def health_checks_create(
    spec_path: str,
    run_id: str,
    tenant_id: str,
    max_jobs: int,
    log_file: str,
) -> None:
    """Create health-check jobs and record progress to log.html."""
    try:
        result = create_health_checks(
            queue=_queue(),
            spec_path=spec_path,
            run_id=run_id,
            tenant_id=tenant_id,
            log_path=log_file,
            max_jobs=max_jobs,
        )
    except ProducerError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    click.echo(json.dumps(result, indent=2))


@cli.group("producer")
def producer() -> None:
    """Checklist-based job producer commands (Phase 3)."""


@producer.command("run")
@click.option(
    "--checklist",
    "checklist_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to checklist.yml.",
)
@click.option("--run-id", required=True, help="Run identifier (overrides checklist run_id).")
@click.option("--dry-run", is_flag=True, default=False, help="Print plan without enqueuing.")
@click.option(
    "--registry",
    "registry_path",
    default="config/action_registry.yml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to action_registry.yml.",
)
def producer_run(
    checklist_path: str,
    run_id: str,
    dry_run: bool,
    registry_path: str,
) -> None:
    """Load checklist.yml and enqueue the first wave of ready tasks."""
    from pathlib import Path

    from hc.checklist.loader import ActionRegistry  # noqa: PLC0415
    from hc.checklist.producer import ChecklistProducer  # noqa: PLC0415

    try:
        registry = ActionRegistry(Path(registry_path))
        prod = ChecklistProducer(registry=registry, queue=_queue())
        result = prod.run(
            checklist_path=Path(checklist_path),
            run_id=run_id,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        click.echo(str(exc), err=True)
        sys.exit(1)

    click.echo(json.dumps(result.as_dict(), indent=2))


if __name__ == "__main__":
    cli()
