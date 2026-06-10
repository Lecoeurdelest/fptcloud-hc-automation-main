"""T-0101 through T-0104: enqueue and task_id."""

from __future__ import annotations

import pytest

from hc.models.task import EnqueueResult, TaskSpec, compute_spec_hash, compute_task_id
from hc.queue.redis_queue import RedisQueue

pytestmark = pytest.mark.unit


def _task(
    run_id: str = "run-1",
    tc_id: str = "TC-001",
    tenant_id: str = "tenant-a",
    spec: dict | None = None,
) -> TaskSpec:
    spec = spec or {"action": "create_subnet"}
    spec_hash = compute_spec_hash(spec)
    return TaskSpec(
        run_id=run_id,
        tc_id=tc_id,
        tenant_id=tenant_id,
        spec_hash=spec_hash,
        spec=spec,
    )


def test_t0101_enqueue_returns_enqueued(queue: RedisQueue) -> None:
    result = queue.enqueue(_task())
    assert result == EnqueueResult.ENQUEUED


def test_t0102_enqueue_returns_duplicate(queue: RedisQueue) -> None:
    task = _task()
    assert queue.enqueue(task) == EnqueueResult.ENQUEUED
    assert queue.enqueue(task) == EnqueueResult.DUPLICATE


def test_t0103_task_id_deterministic() -> None:
    a = compute_task_id("run-1", "TC-001", "tenant-a", "hash1")
    b = compute_task_id("run-1", "TC-001", "tenant-a", "hash1")
    assert a == b


def test_t0104_task_id_changes_with_spec_hash() -> None:
    h1 = compute_spec_hash({"action": "a"})
    h2 = compute_spec_hash({"action": "b"})
    id1 = compute_task_id("run-1", "TC-001", "tenant-a", h1)
    id2 = compute_task_id("run-1", "TC-001", "tenant-a", h2)
    assert id1 != id2
