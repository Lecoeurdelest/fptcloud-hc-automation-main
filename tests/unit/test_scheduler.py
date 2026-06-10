"""T-0113 through T-0115: Scheduler."""

from __future__ import annotations

import json
import time

import pytest

from hc.models.task import TaskSpec, compute_spec_hash
from hc.queue.redis_queue import RedisQueue

pytestmark = pytest.mark.unit


def _scheduled_task() -> TaskSpec:
    spec = {"action": "retry"}
    return TaskSpec(
        run_id="run-1",
        tc_id="TC-010",
        tenant_id="tenant-a",
        spec_hash=compute_spec_hash(spec),
        spec=spec,
        attempt=1,
    )


def test_t0113_scheduler_moves_due_entries(queue: RedisQueue) -> None:
    task = _scheduled_task()
    member = json.dumps({"entry_id": "1-0", "task": task.model_dump(), "reason": "err"})
    queue._r.zadd(queue.settings.zset_scheduled, {member: time.time() - 1})
    moved = queue.move_scheduled_to_tasks()
    assert moved == 1
    assert queue._r.xlen(queue.settings.stream_tasks) == 1


def test_t0114_scheduler_ignores_future_entries(queue: RedisQueue) -> None:
    task = _scheduled_task()
    member = json.dumps({"entry_id": "1-0", "task": task.model_dump(), "reason": "err"})
    queue._r.zadd(queue.settings.zset_scheduled, {member: time.time() + 3600})
    moved = queue.move_scheduled_to_tasks()
    assert moved == 0
    assert queue._r.zcard(queue.settings.zset_scheduled) == 1


def test_t0115_scheduler_removes_from_zset_atomically(queue: RedisQueue) -> None:
    task = _scheduled_task()
    member = json.dumps({"entry_id": "1-0", "task": task.model_dump(), "reason": "err"})
    queue._r.zadd(queue.settings.zset_scheduled, {member: time.time() - 1})
    queue.move_scheduled_to_tasks()
    assert queue._r.zcard(queue.settings.zset_scheduled) == 0
