"""Scheduler coroutine: moves due retries from hc:scheduled to hc:tasks."""

from __future__ import annotations

import asyncio

import structlog

from hc.queue.redis_queue import RedisQueue

log = structlog.get_logger()


class Scheduler:
    """Poll hc:scheduled every interval and re-enqueue due tasks."""

    def __init__(
        self,
        queue: RedisQueue,
        interval_seconds: float = 1.0,
    ) -> None:
        self._queue = queue
        self._interval = interval_seconds
        self._running = False

    async def run(self) -> None:
        self._running = True
        log.info("scheduler_started", interval=self._interval)
        while self._running:
            moved = self._queue.move_scheduled_to_tasks()
            if moved:
                log.info("scheduler_moved", count=moved)
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False
