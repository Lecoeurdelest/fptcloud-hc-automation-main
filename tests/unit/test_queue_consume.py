"""T-0105, T-0106: consume."""

from __future__ import annotations

import pytest

from hc.models.task import TaskSpec, compute_spec_hash
from hc.queue.redis_queue import RedisQueue

pytestmark = pytest.mark.unit


def _task() -> TaskSpec:
    spec = {"action": "probe"}
    return TaskSpec(
        run_id="run-1",
        tc_id="TC-002",
        tenant_id="tenant-a",
        spec_hash=compute_spec_hash(spec),
        spec=spec,
    )


def test_t0105_consume_returns_one_entry(queue: RedisQueue) -> None:
    queue.enqueue(_task())
    entry = queue.consume("hc-workers-test", "worker-1", block_ms=100)
    assert entry is not None
    assert entry.task.tc_id == "TC-002"
    assert entry.entry_id


def test_t0106_consume_blocks_returns_none(queue: RedisQueue) -> None:
    queue.ensure_consumer_group("hc-workers-test")
    entry = queue.consume("hc-workers-test", "worker-empty", block_ms=50)
    assert entry is None
