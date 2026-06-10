"""T-0120 through T-0124: DLQ and replay."""

from __future__ import annotations

import pytest

from hc.models.task import TaskSpec, compute_spec_hash
from hc.queue.redis_queue import RedisQueue

pytestmark = pytest.mark.unit


def _task(attempt: int = 3, max_attempts: int = 3) -> TaskSpec:
    spec = {"action": "fail"}
    return TaskSpec(
        run_id="run-1",
        tc_id="TC-012",
        tenant_id="tenant-a",
        spec_hash=compute_spec_hash(spec),
        spec=spec,
        attempt=attempt,
        retry_policy={"max_attempts": max_attempts, "base_seconds": 30},
    )


def test_t0120_dlq_when_attempt_exceeds_max(queue: RedisQueue) -> None:
    queue.enqueue(_task(attempt=3))
    entry = queue.consume("hc-workers-test", "w1", block_ms=100)
    assert entry is not None
    queue.nack(entry.entry_id, "final failure")
    assert queue._r.xlen(queue.settings.stream_dlq) == 1


def test_t0121_dlq_acks_original(queue: RedisQueue) -> None:
    queue.enqueue(_task(attempt=3))
    entry = queue.consume("hc-workers-test", "w1", block_ms=100)
    assert entry is not None
    queue.nack(entry.entry_id, "dead")
    stats = queue.stats()
    assert stats["pel_depth"] == 0


def test_t0122_dlq_payload_fields(queue: RedisQueue) -> None:
    queue.enqueue(_task(attempt=3))
    entry = queue.consume("hc-workers-test", "w1", block_ms=100)
    assert entry is not None
    queue.nack(entry.entry_id, "quota exceeded")
    dlq_entries = queue._r.xrevrange(queue.settings.stream_dlq, count=1)
    _eid, fields = dlq_entries[0]
    assert "task_id" in fields
    assert fields["last_error"] == "quota exceeded"
    assert "failed_at" in fields
    assert "payload" in fields


def test_t0123_dlq_replay_resets_attempt(queue: RedisQueue) -> None:
    queue.enqueue(_task(attempt=3))
    entry = queue.consume("hc-workers-test", "w1", block_ms=100)
    assert entry is not None
    queue.nack(entry.entry_id, "dead")
    dlq_id = queue._r.xrevrange(queue.settings.stream_dlq, count=1)[0][0]
    new_id = queue.replay_dlq(dlq_id)
    assert new_id
    replayed = queue.consume("hc-workers-test", "w2", block_ms=100)
    assert replayed is not None
    assert replayed.task.attempt == 0


def test_t0124_dlq_replay_new_task_id(queue: RedisQueue) -> None:
    task = _task(attempt=3)
    original_id = task.task_id
    queue.enqueue(task)
    entry = queue.consume("hc-workers-test", "w1", block_ms=100)
    assert entry is not None
    queue.nack(entry.entry_id, "dead")
    dlq_id = queue._r.xrevrange(queue.settings.stream_dlq, count=1)[0][0]
    queue.replay_dlq(dlq_id)
    replayed = queue.consume("hc-workers-test", "w2", block_ms=100)
    assert replayed is not None
    assert replayed.task.task_id != original_id
