"""Redis Streams queue with dedup, retry scheduling, and DLQ."""

from __future__ import annotations

import json
import time
from typing import Any, cast

import redis

from hc.config.settings import QueueSettings
from hc.models.task import (
    EnqueueResult,
    QueueEntry,
    TaskSpec,
    compute_backoff_seconds,
    compute_task_id,
    now_ms,
)


class RedisQueue:
    """Unique-enqueue + at-least-once-dequeue queue backed by Redis Streams."""

    def __init__(
        self,
        client: redis.Redis[str],  # type: ignore[type-arg]
        settings: QueueSettings | None = None,
    ) -> None:
        self._r = client
        self._s = settings or QueueSettings.from_env()

    @property
    def settings(self) -> QueueSettings:
        return self._s

    def _xadd(self, stream: str, fields: dict[str, str]) -> str:
        """XADD wrapper that pins redis-py's sync return type to str."""
        return cast("str", self._r.xadd(stream, cast("dict[Any, Any]", fields)))

    def ensure_consumer_group(self, group: str | None = None) -> None:
        grp = group or self._s.consumer_group
        try:
            self._r.xgroup_create(self._s.stream_tasks, grp, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def enqueue(self, task: TaskSpec) -> EnqueueResult:
        ts = now_ms()
        added = self._r.zadd(self._s.zset_dedup, {task.task_id: ts}, nx=True)
        if added == 0:
            return EnqueueResult.DUPLICATE
        task.enqueued_at = time.time()
        self._xadd(self._s.stream_tasks, task.to_stream_fields())
        return EnqueueResult.ENQUEUED

    def consume(
        self,
        group: str,
        consumer: str,
        block_ms: int = 5000,
    ) -> QueueEntry | None:
        self.ensure_consumer_group(group)
        result = cast(
            "list[Any] | None",
            self._r.xreadgroup(
                group,
                consumer,
                {self._s.stream_tasks: ">"},
                count=1,
                block=block_ms,
            ),
        )
        if not result:
            return None
        _stream, entries = result[0]
        entry_id, fields = entries[0]
        task = TaskSpec.from_stream_fields(fields)
        return QueueEntry(entry_id=entry_id, task=task)

    def ack(self, entry_id: str, group: str | None = None) -> None:
        grp = group or self._s.consumer_group
        self._r.xack(self._s.stream_tasks, grp, entry_id)

    def nack(self, entry_id: str, reason: str, group: str | None = None) -> None:
        grp = group or self._s.consumer_group
        raw = cast(
            "list[Any]",
            self._r.xrange(self._s.stream_tasks, min=entry_id, max=entry_id, count=1),
        )
        if not raw:
            self._r.xack(self._s.stream_tasks, grp, entry_id)
            return
        _eid, fields = raw[0]
        task = TaskSpec.from_stream_fields(fields)
        task.attempt += 1
        policy = task.retry_policy

        if task.attempt > policy.max_attempts:
            self._move_to_dlq(entry_id, task, reason, grp)
            return

        wake_at = time.time() + compute_backoff_seconds(task.attempt, policy)
        scheduled_member = json.dumps(
            {"entry_id": entry_id, "task": task.model_dump(), "reason": reason}
        )
        pipe = self._r.pipeline()
        pipe.zadd(self._s.zset_scheduled, {scheduled_member: wake_at})
        pipe.xack(self._s.stream_tasks, grp, entry_id)
        pipe.execute()

    def _move_to_dlq(
        self,
        entry_id: str,
        task: TaskSpec,
        reason: str,
        group: str,
    ) -> None:
        dlq_fields: dict[str, str] = {
            "task_id": task.task_id,
            "payload": task.model_dump_json(),
            "last_error": reason,
            "failed_at": str(time.time()),
            "original_entry_id": entry_id,
        }
        pipe = self._r.pipeline()
        pipe.xadd(self._s.stream_dlq, cast("dict[Any, Any]", dlq_fields))
        pipe.xack(self._s.stream_tasks, group, entry_id)
        pipe.execute()

    def move_scheduled_to_tasks(self, limit: int = 100) -> int:
        """Move due entries from hc:scheduled back to hc:tasks."""
        now = time.time()
        due = cast(
            "list[Any]",
            self._r.zrangebyscore(self._s.zset_scheduled, "-inf", now, start=0, num=limit),
        )
        moved = 0
        for member in due:
            data = json.loads(member)
            task = TaskSpec.model_validate(data["task"])
            pipe = self._r.pipeline()
            pipe.xadd(self._s.stream_tasks, cast("dict[Any, Any]", task.to_stream_fields()))
            pipe.zrem(self._s.zset_scheduled, member)
            if pipe.execute():
                moved += 1
        return moved

    def replay_dlq(self, dlq_entry_id: str) -> str:
        """Re-enqueue a DLQ entry with a fresh task_id and attempt=0."""
        entries = cast(
            "list[Any]",
            self._r.xrange(self._s.stream_dlq, min=dlq_entry_id, max=dlq_entry_id, count=1),
        )
        if not entries:
            msg = f"DLQ entry not found: {dlq_entry_id}"
            raise ValueError(msg)
        _eid, fields = entries[0]
        task = TaskSpec.model_validate_json(fields["payload"])
        task.attempt = 0
        task.task_id = compute_task_id(
            task.run_id, task.tc_id, task.tenant_id, f"{task.spec_hash}-replay"
        )
        ts = now_ms()
        self._r.zadd(self._s.zset_dedup, {task.task_id: ts}, nx=True)
        return self._xadd(self._s.stream_tasks, task.to_stream_fields())

    def stats(self) -> dict[str, int]:
        """Return pending stream length, PEL depth, DLQ depth, scheduled count."""
        group = self._s.consumer_group
        stream_len = cast("int", self._r.xlen(self._s.stream_tasks))
        dlq_len = cast("int", self._r.xlen(self._s.stream_dlq))
        scheduled = cast("int", self._r.zcard(self._s.zset_scheduled))
        pel = 0
        try:
            pending = cast("dict[str, Any]", self._r.xpending(self._s.stream_tasks, group))
            if pending and pending.get("pending", 0):
                pel = int(pending["pending"])
        except redis.ResponseError:
            pass
        return {
            "stream_length": stream_len,
            "pel_depth": pel,
            "dlq_depth": dlq_len,
            "scheduled_count": scheduled,
        }

    def peek(self, count: int = 10) -> list[dict[str, Any]]:
        entries = cast("list[Any]", self._r.xrevrange(self._s.stream_tasks, count=count))
        result: list[dict[str, Any]] = []
        for entry_id, fields in reversed(entries):
            task = TaskSpec.from_stream_fields(fields)
            result.append({"entry_id": entry_id, "task_id": task.task_id, "attempt": task.attempt})
        return result

    def list_dlq(self, count: int = 50) -> list[dict[str, Any]]:
        entries = cast("list[Any]", self._r.xrevrange(self._s.stream_dlq, count=count))
        result: list[dict[str, Any]] = []
        for entry_id, fields in entries:
            result.append(
                {
                    "entry_id": entry_id,
                    "task_id": fields.get("task_id", ""),
                    "last_error": fields.get("last_error", ""),
                    "failed_at": fields.get("failed_at", ""),
                }
            )
        return result

    def flush_all(self) -> None:
        """Remove all queue keys — for test cleanup."""
        keys = [
            self._s.stream_tasks,
            self._s.stream_dlq,
            self._s.zset_dedup,
            self._s.zset_scheduled,
        ]
        self._r.delete(*keys)
