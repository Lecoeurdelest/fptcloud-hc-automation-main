"""Queue exports."""

__all__ = ["RedisQueue", "Reaper", "Scheduler"]


def __getattr__(name: str):
    if name == "RedisQueue":
        from hc.queue.redis_queue import RedisQueue

        return RedisQueue
    if name == "Reaper":
        from hc.queue.reaper import Reaper

        return Reaper
    if name == "Scheduler":
        from hc.queue.scheduler import Scheduler

        return Scheduler
    raise AttributeError(name)
