"""T-0107 through T-0112: ack, nack, backoff."""

from __future__ import annotations

import json
import time

import pytest

from hc.models.task import RetryPolicy, TaskSpec, compute_backoff_seconds, compute_spec_hash
from hc.queue.redis_queue import RedisQueue

pytestmark = pytest.mark.unit


def _task(attempt: int = 0, max_attempts: int = 3) -> TaskSpec:
    spec = {"action": "test"}
    return TaskSpec(
        run_id="run-1",
        tc_id="TC-003",
        tenant_id="tenant-a",
        spec_hash=compute_spec_hash(spec),
        spec=spec,
        attempt=attempt,
        retry_policy=RetryPolicy(max_attempts=max_attempts, base_seconds=30, max_seconds=600),
    )


def test_t0107_ack_removes_from_pel(queue: RedisQueue) -> None:
    queue.enqueue(_task())
    entry = queue.consume("hc-workers-test", "w1", block_ms=100)
    assert entry is not None
    stats_before = queue.stats()
    assert stats_before["pel_depth"] >= 1
    queue.ack(entry.entry_id)
    stats_after = queue.stats()
    assert stats_after["pel_depth"] == 0


def test_t0108_nack_schedules_zset(queue: RedisQueue) -> None:
    queue.enqueue(_task())
    entry = queue.consume("hc-workers-test", "w1", block_ms=100)
    assert entry is not None
    queue.nack(entry.entry_id, "transient error")
    scheduled = queue._r.zcard(queue.settings.zset_scheduled)
    assert scheduled == 1
    members = queue._r.zrange(queue.settings.zset_scheduled, 0, -1, withscores=True)
    _member, score = members[0]
    assert score > time.time()


def test_t0109_nack_increments_attempt(queue: RedisQueue) -> None:
    queue.enqueue(_task(attempt=0))
    entry = queue.consume("hc-workers-test", "w1", block_ms=100)
    assert entry is not None
    queue.nack(entry.entry_id, "retry me")
    raw = queue._r.zrange(queue.settings.zset_scheduled, 0, 0)[0]
    data = json.loads(raw)
    assert data["task"]["attempt"] == 1


def test_t0110_exponential_backoff_base() -> None:
    policy = RetryPolicy(base_seconds=30, max_seconds=600, jitter=0.0)
    assert compute_backoff_seconds(1, policy) == 30.0
    assert compute_backoff_seconds(2, policy) == 60.0
    assert compute_backoff_seconds(3, policy) == 120.0


def test_t0111_backoff_jitter_within_range() -> None:
    policy = RetryPolicy(base_seconds=30, max_seconds=600, jitter=0.2)
    for _ in range(50):
        delay = compute_backoff_seconds(1, policy)
        assert 24.0 <= delay <= 36.0


def test_t0112_backoff_caps_at_max_seconds() -> None:
    policy = RetryPolicy(base_seconds=30, max_seconds=600, jitter=0.0)
    delay = compute_backoff_seconds(10, policy)
    assert delay == 600.0
