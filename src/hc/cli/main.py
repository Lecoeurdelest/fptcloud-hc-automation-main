"""Health-check CLI."""

from __future__ import annotations

import json
import sys

import click
import redis

from hc.config.settings import QueueSettings
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


if __name__ == "__main__":
    cli()
