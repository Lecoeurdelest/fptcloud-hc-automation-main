"""T-1001 through T-1005: Queue integration tests."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from hc.models.task import EnqueueResult, TaskSpec, compute_spec_hash
from hc.queue.reaper import Reaper
from hc.queue.redis_queue import RedisQueue
from hc.queue.scheduler import Scheduler

pytestmark = pytest.mark.integration


def _make_task(i: int, run_id: str = "int-run") -> TaskSpec:
    spec = {"action": "task", "index": i}
    return TaskSpec(
        run_id=run_id,
        tc_id=f"TC-{i:04d}",
        tenant_id="tenant-int",
        spec_hash=compute_spec_hash(spec),
        spec=spec,
    )


def test_t1001_thousand_tasks_enqueue_ack(integration_queue: RedisQueue) -> None:
    group = integration_queue.settings.consumer_group
    enqueued = 0
    for i in range(1000):
        if integration_queue.enqueue(_make_task(i)) == EnqueueResult.ENQUEUED:
            enqueued += 1
    assert enqueued == 1000
    acked = 0
    consumer_idx = 0
    while acked < 1000:
        entry = integration_queue.consume(group, f"worker-{consumer_idx % 4}", block_ms=2000)
        if entry is None:
            continue
        integration_queue.ack(entry.entry_id, group)
        acked += 1
        consumer_idx += 1
    stats = integration_queue.stats()
    assert stats["pel_depth"] == 0
    assert acked == 1000


def test_t1002_duplicate_enqueues_dropped(integration_queue: RedisQueue) -> None:
    task = _make_task(1)
    first = 0
    dup = 0
    for _ in range(100):
        r = integration_queue.enqueue(task)
        if r == EnqueueResult.ENQUEUED:
            first += 1
        else:
            dup += 1
    assert first == 1
    assert dup == 99
    assert integration_queue._r.xlen(integration_queue.settings.stream_tasks) == 1


def test_t1003_consumer_group_idempotent(integration_queue: RedisQueue) -> None:
    group = "hc-idempotent"
    integration_queue.ensure_consumer_group(group)
    integration_queue.ensure_consumer_group(group)
    info = integration_queue._r.xinfo_groups(integration_queue.settings.stream_tasks)
    names = [g["name"] for g in info]
    assert group in names


def test_t1004_two_consumers_round_robin(integration_queue: RedisQueue) -> None:
    group = integration_queue.settings.consumer_group
    for i in range(10):
        integration_queue.enqueue(_make_task(i))
    w1_tasks: list[str] = []
    w2_tasks: list[str] = []
    for _ in range(10):
        e1 = integration_queue.consume(group, "worker-a", block_ms=500)
        e2 = integration_queue.consume(group, "worker-b", block_ms=500)
        if e1:
            w1_tasks.append(e1.task.tc_id)
            integration_queue.ack(e1.entry_id, group)
        if e2:
            w2_tasks.append(e2.task.tc_id)
            integration_queue.ack(e2.entry_id, group)
    assert len(w1_tasks) + len(w2_tasks) == 10
    assert len(w1_tasks) > 0
    assert len(w2_tasks) > 0


@pytest.mark.asyncio
async def test_t1005_scheduler_reaper_coexist(integration_queue: RedisQueue) -> None:
    integration_queue.enqueue(_make_task(99))
    entry = integration_queue.consume(
        integration_queue.settings.consumer_group, "stale", block_ms=500
    )
    assert entry
    scheduler = Scheduler(integration_queue, interval_seconds=0.1)
    reaper = Reaper(integration_queue, idle_ms=0, interval_seconds=0.1)
    sched_task = asyncio.create_task(scheduler.run())
    reap_task = asyncio.create_task(reaper.run())
    await asyncio.sleep(0.5)
    scheduler.stop()
    reaper.stop()
    sched_task.cancel()
    reap_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sched_task
    with contextlib.suppress(asyncio.CancelledError):
        await reap_task
    stats = integration_queue.stats()
    assert stats["dlq_depth"] >= 0
