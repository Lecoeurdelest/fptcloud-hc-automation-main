"""T-0116 through T-0119: Reaper."""

from __future__ import annotations

import pytest

from hc.models.task import TaskSpec, compute_spec_hash
from hc.queue.reaper import Reaper
from hc.queue.redis_queue import RedisQueue

pytestmark = pytest.mark.unit


def _task() -> TaskSpec:
    spec = {"action": "reap"}
    return TaskSpec(
        run_id="run-1",
        tc_id="TC-011",
        tenant_id="tenant-a",
        spec_hash=compute_spec_hash(spec),
        spec=spec,
        attempt=0,
    )


def test_t0116_reaper_identifies_idle_entries(queue: RedisQueue) -> None:
    queue.enqueue(_task())
    entry = queue.consume("hc-workers-test", "crashed-worker", block_ms=100)
    assert entry is not None
    reaper = Reaper(queue, idle_ms=0, interval_seconds=60.0)
    reclaimed = reaper.reclaim_idle("hc-workers-test")
    assert reclaimed >= 1


def test_t0117_reaper_xclaims_to_new_consumer(queue: RedisQueue) -> None:
    queue.enqueue(_task())
    queue.consume("hc-workers-test", "dead-worker", block_ms=100)
    reaper = Reaper(queue, idle_ms=0, reclaim_consumer="reaper-1")
    reaper.reclaim_idle("hc-workers-test")
    pending = queue._r.xpending_range(queue.settings.stream_tasks, "hc-workers-test", "-", "+", 10)
    consumers = {p["consumer"] for p in pending}
    assert "reaper-1" in consumers or len(pending) == 0


def test_t0118_reaper_bumps_attempt(queue: RedisQueue) -> None:
    queue.enqueue(_task())
    entry = queue.consume("hc-workers-test", "dead-worker", block_ms=100)
    assert entry is not None
    reaper = Reaper(queue, idle_ms=0)
    reaper.reclaim_idle("hc-workers-test")
    latest = queue._r.xrevrange(queue.settings.stream_tasks, count=1)
    _eid, fields = latest[0]
    task = TaskSpec.from_stream_fields(fields)
    assert task.attempt >= 1


def test_t0119_reaper_ignores_recent_idle(queue: RedisQueue) -> None:
    queue.enqueue(_task())
    queue.consume("hc-workers-test", "active-worker", block_ms=100)
    reaper = Reaper(queue, idle_ms=999_999_999)
    reclaimed = reaper.reclaim_idle("hc-workers-test")
    assert reclaimed == 0
