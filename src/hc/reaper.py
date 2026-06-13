"""Runtime entrypoint for scheduled retries and idle task reaping."""

from __future__ import annotations

import asyncio
import contextlib
import signal

import redis
import structlog

from hc.config.settings import QueueSettings
from hc.queue.reaper import Reaper
from hc.queue.redis_queue import RedisQueue
from hc.queue.scheduler import Scheduler

log = structlog.get_logger()


async def run() -> None:
    """Run the scheduler and reaper loops until interrupted."""
    settings = QueueSettings.from_env()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    queue = RedisQueue(client, settings)
    scheduler = Scheduler(queue)
    reaper = Reaper(queue)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    scheduler_task = asyncio.create_task(scheduler.run(), name="scheduler")
    reaper_task = asyncio.create_task(reaper.run(), name="reaper")
    log.info("reaper_runtime_started")
    try:
        await stop_event.wait()
    finally:
        scheduler.stop()
        reaper.stop()
        for task in (scheduler_task, reaper_task):
            task.cancel()
        for task in (scheduler_task, reaper_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        log.info("reaper_runtime_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
