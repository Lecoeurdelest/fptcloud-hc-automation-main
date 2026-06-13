"""Health-check CLI."""

from __future__ import annotations

import json
import sys

import click
import redis

from hc.config.settings import QueueSettings
from hc.db import migrate
from hc.producer import DEFAULT_MAX_JOBS, ProducerError, create_health_checks
from hc.queue.redis_queue import RedisQueue


def _queue() -> RedisQueue:
    settings = QueueSettings.from_env()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    return RedisQueue(client, settings)


@click.group()
def cli() -> None:
    """FPT Cloud health-check automation CLI."""


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


if __name__ == "__main__":
    cli()
