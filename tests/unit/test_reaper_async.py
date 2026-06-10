"""Additional coverage for Reaper async loop."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from hc.models.task import TaskSpec, compute_spec_hash
from hc.queue.reaper import Reaper

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_reaper_run_loop(queue) -> None:
    spec = {"action": "async-reap"}
    task = TaskSpec(
        run_id="run-1",
        tc_id="TC-REAP-ASYNC",
        tenant_id="tenant-a",
        spec_hash=compute_spec_hash(spec),
        spec=spec,
    )
    queue.enqueue(task)
    queue.consume("hc-workers-test", "dead", block_ms=100)
    reaper = Reaper(queue, idle_ms=0, interval_seconds=0.05)
    handle = asyncio.create_task(reaper.run())
    await asyncio.sleep(0.2)
    reaper.stop()
    handle.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await handle
