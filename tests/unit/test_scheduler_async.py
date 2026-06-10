"""Additional coverage for Scheduler async loop."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from hc.models.task import TaskSpec, compute_spec_hash
from hc.queue.scheduler import Scheduler

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_scheduler_run_loop(queue) -> None:
    spec = {"action": "async"}
    task = TaskSpec(
        run_id="run-1",
        tc_id="TC-ASYNC",
        tenant_id="tenant-a",
        spec_hash=compute_spec_hash(spec),
        spec=spec,
        attempt=1,
    )
    import json
    import time

    member = json.dumps({"entry_id": "1-0", "task": task.model_dump(), "reason": "err"})
    queue._r.zadd(queue.settings.zset_scheduled, {member: time.time() - 1})
    sched = Scheduler(queue, interval_seconds=0.05)
    task_handle = asyncio.create_task(sched.run())
    await asyncio.sleep(0.2)
    sched.stop()
    task_handle.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task_handle
    assert queue._r.xlen(queue.settings.stream_tasks) >= 1
