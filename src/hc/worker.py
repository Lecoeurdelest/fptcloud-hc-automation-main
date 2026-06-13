"""Minimal worker runtime for executing queued Terraform health-check tasks."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import click
import redis
import structlog

from hc.config.settings import CloudSettings, QueueSettings
from hc.executor.executor import TerraformExecutor
from hc.models.task import QueueEntry, TaskSpec
from hc.queue.redis_queue import RedisQueue

log = structlog.get_logger()


def _module_name(task: TaskSpec) -> str:
    raw = task.spec.get("module") or task.spec.get("resource") or task.category
    module = str(raw).strip()
    if not module:
        msg = f"{task.tc_id}: task spec does not name a Terraform module"
        raise ValueError(msg)
    return module


def _module_vars(task: TaskSpec) -> dict[str, Any]:
    raw_vars = task.spec.get("vars")
    if isinstance(raw_vars, dict):
        return raw_vars
    return {
        key: value
        for key, value in task.spec.items()
        if key not in {"module", "resource", "description", "expected", "depends_on"}
    }


def _execute_entry(entry: QueueEntry, queue: RedisQueue, workspace_root: Path) -> None:
    task = entry.task
    module_name = _module_name(task)
    module_path = Path("modules") / module_name
    if not module_path.exists():
        msg = f"{task.tc_id}: Terraform module not found: {module_path}"
        raise ValueError(msg)

    workspace = workspace_root / task.run_id / task.task_id
    cloud = CloudSettings.from_env()
    executor = TerraformExecutor(
        workspace_path=workspace,
        module_path=module_path.resolve(),
        vars=_module_vars(task),
        env=cloud.terraform_env(),
        plugin_cache_dir=Path(".terraform.d") / "plugin-cache",
        cleanup_on_success=False,
    )
    result = executor.execute()
    if not result.success:
        reason = result.error.message if result.error else "terraform execution failed"
        queue.nack(entry.entry_id, reason)
        log.warning("worker_task_failed", task_id=task.task_id, tc_id=task.tc_id, reason=reason)
        return

    queue.ack(entry.entry_id)
    log.info("worker_task_succeeded", task_id=task.task_id, tc_id=task.tc_id)


@click.command()
@click.option("--consumer", default=None, help="Consumer name. Defaults to hostname.")
@click.option("--max-tasks", default=0, show_default=True, help="Stop after N tasks; 0 means forever.")
@click.option("--block-ms", default=5000, show_default=True)
@click.option("--workspace-root", default="runs/tasks", show_default=True, type=click.Path())
def main(
    consumer: str | None,
    max_tasks: int,
    block_ms: int,
    workspace_root: str,
) -> None:
    """Consume health-check jobs and run their Terraform modules."""
    settings = QueueSettings.from_env()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    queue = RedisQueue(client, settings)
    consumer_name = consumer or socket.gethostname()
    completed = 0
    log.info("worker_started", consumer=consumer_name, group=settings.consumer_group)

    while max_tasks == 0 or completed < max_tasks:
        entry = queue.consume(settings.consumer_group, consumer_name, block_ms=block_ms)
        if entry is None:
            continue
        try:
            _execute_entry(entry, queue, Path(workspace_root))
        except Exception as exc:  # noqa: BLE001
            queue.nack(entry.entry_id, str(exc))
            log.exception("worker_task_error", task_id=entry.task.task_id, error=str(exc))
        completed += 1


if __name__ == "__main__":
    main()
