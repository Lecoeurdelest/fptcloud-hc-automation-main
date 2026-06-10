"""Reaper coroutine: reclaims idle PEL entries via XCLAIM."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import redis
import structlog

from hc.models.task import TaskSpec
from hc.queue.redis_queue import RedisQueue

log = structlog.get_logger()


class Reaper:
    """Claim idle pending entries and bump attempt counter."""

    def __init__(
        self,
        queue: RedisQueue,
        idle_ms: int | None = None,
        interval_seconds: float = 60.0,
        reclaim_consumer: str = "reaper-1",
    ) -> None:
        self._queue = queue
        self._s = queue.settings
        self._idle_ms = idle_ms if idle_ms is not None else self._s.reaper_idle_ms
        self._interval = interval_seconds
        self._consumer = reclaim_consumer
        self._running = False

    def reclaim_idle(self, group: str | None = None) -> int:
        grp = group or self._s.consumer_group
        self._queue.ensure_consumer_group(grp)
        pending = cast(
            "list[Any]",
            self._queue._r.xpending_range(
                self._s.stream_tasks,
                grp,
                min="-",
                max="+",
                count=100,
            ),
        )
        reclaimed = 0
        for item in pending:
            idle = int(item.get("time_since_delivered", 0))
            if idle < self._idle_ms:
                continue
            entry_id = item["message_id"]
            consumer = item.get("consumer", "unknown")
            try:
                claimed = cast(
                    "list[Any]",
                    self._queue._r.xclaim(
                        self._s.stream_tasks,
                        grp,
                        self._consumer,
                        self._idle_ms,
                        [entry_id],
                    ),
                )
            except redis.ResponseError:
                continue
            if not claimed:
                continue
            for cid, fields in claimed:
                task = TaskSpec.from_stream_fields(fields)
                task.attempt += 1
                pipe = self._queue._r.pipeline()
                pipe.xack(self._s.stream_tasks, grp, cid)
                pipe.xadd(self._s.stream_tasks, cast("dict[Any, Any]", task.to_stream_fields()))
                pipe.execute()
                log.info(
                    "reaper_reclaimed",
                    entry_id=cid,
                    from_consumer=consumer,
                    attempt=task.attempt,
                )
                reclaimed += 1
        return reclaimed

    async def run(self) -> None:
        self._running = True
        log.info("reaper_started", idle_ms=self._idle_ms, interval=self._interval)
        while self._running:
            count = self.reclaim_idle()
            if count:
                log.info("reaper_cycle", reclaimed=count)
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False
