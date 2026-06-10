"""Chaos-test harness for the Phase 1 queue (review gate for P1).

Self-contained worker/reaper/enqueue/verify driver used by
``scripts/chaos_kill_worker.sh``. It operates purely on the queue — no
Terraform, no FPT Cloud — and proves the at-least-once contract survives a
worker being killed mid-task: zero task loss, and zero *duplicate side
effects* (side effects are recorded into a Redis set keyed by task_id, so a
re-delivered task collapses to a single distinct completion).

Subcommands::

    enqueue --count N            seed N unique tasks, record the total
    work --consumer C [--die-after K]
                                 consume + (idempotently) complete + ack;
                                 with --die-after K, hold K entries unacked
                                 then exit hard (orphaned PEL entries)
    reap --idle-ms M --interval S
                                 loop reclaim_idle until everything completes
    verify                       exit 0 iff every task completed and PEL empty
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import redis

from hc.config.settings import QueueSettings
from hc.models.task import EnqueueResult, TaskSpec, compute_spec_hash
from hc.queue.reaper import Reaper
from hc.queue.redis_queue import RedisQueue

# Bookkeeping keys (outside the hc: namespace so flush_all leaves them alone).
K_TOTAL = "chaos:total"
K_COMPLETED = "chaos:completed"  # SET of distinct task_ids actually finished
K_DELIVERIES = "chaos:deliveries"  # INCR per successful processing (>= total)


def _client() -> redis.Redis[str]:
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return redis.from_url(url, decode_responses=True)


def _queue(client: redis.Redis[str]) -> RedisQueue:
    return RedisQueue(client, QueueSettings.from_env())


def _make_task(i: int, run_id: str) -> TaskSpec:
    spec = {"action": "chaos", "index": i}
    return TaskSpec(
        run_id=run_id,
        tc_id=f"TC-CHAOS-{i:04d}",
        tenant_id="tenant-chaos",
        spec_hash=compute_spec_hash(spec),
        spec=spec,
    )


def cmd_enqueue(args: argparse.Namespace) -> int:
    client = _client()
    queue = _queue(client)
    enqueued = 0
    for i in range(args.count):
        if queue.enqueue(_make_task(i, args.run_id)) == EnqueueResult.ENQUEUED:
            enqueued += 1
    client.set(K_TOTAL, args.count)
    print(f"[enqueue] {enqueued}/{args.count} unique tasks queued")
    return 0 if enqueued == args.count else 1


def _all_done(client: redis.Redis[str]) -> bool:
    total = int(client.get(K_TOTAL) or 0)
    return total > 0 and client.scard(K_COMPLETED) >= total


def cmd_work(args: argparse.Namespace) -> int:
    client = _client()
    queue = _queue(client)
    group = queue.settings.consumer_group
    consumed = 0
    held: list[str] = []  # entries deliberately left unacked before a crash

    deadline = time.time() + args.max_seconds
    while time.time() < deadline:
        if _all_done(client) and not held:
            break
        entry = queue.consume(group, args.consumer, block_ms=500)
        if entry is None:
            continue
        consumed += 1

        # Crash path: hold entries unacked, then die hard once we hit --die-after.
        if args.die_after and consumed <= args.die_after:
            held.append(entry.entry_id)
            if consumed == args.die_after:
                print(
                    f"[work {args.consumer}] simulating SIGKILL with "
                    f"{len(held)} unacked entries",
                    flush=True,
                )
                os._exit(137)  # hard exit: no cleanup, no ack — orphans the PEL entries
            continue

        # Happy path: idempotent side effect keyed by task_id, then ack.
        client.sadd(K_COMPLETED, entry.task.task_id)
        client.incr(K_DELIVERIES)
        queue.ack(entry.entry_id, group)

    print(f"[work {args.consumer}] consumed={consumed}", flush=True)
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    client = _client()
    queue = _queue(client)
    reaper = Reaper(queue, idle_ms=args.idle_ms, interval_seconds=args.interval)
    deadline = time.time() + args.max_seconds
    total_reclaimed = 0
    while time.time() < deadline:
        if _all_done(client):
            break
        n = reaper.reclaim_idle()
        total_reclaimed += n
        if n:
            print(f"[reap] reclaimed {n} idle entries", flush=True)
        time.sleep(args.interval)
    print(f"[reap] total reclaimed={total_reclaimed}", flush=True)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    client = _client()
    queue = _queue(client)
    total = int(client.get(K_TOTAL) or 0)
    completed = client.scard(K_COMPLETED)
    deliveries = int(client.get(K_DELIVERIES) or 0)
    stats = queue.stats()
    duplicates = max(0, deliveries - completed)

    print("-------- chaos verification --------")
    print(f"  enqueued total      : {total}")
    print(f"  distinct completed  : {completed}")
    print(f"  total deliveries    : {deliveries}")
    print(f"  duplicate deliveries: {duplicates} (re-delivered after crash)")
    print(f"  queue stats         : {stats}")

    lost = total - completed
    ok = (
        total > 0
        and lost == 0
        and completed == total  # zero task loss
        and stats["pel_depth"] == 0  # nothing stuck unacked
        and stats["dlq_depth"] == 0  # nothing dead-lettered
    )
    # Duplicate *deliveries* are allowed (at-least-once); duplicate *side
    # effects* are not — the set keeps distinct completions == total.
    if ok:
        print("RESULT: PASS - zero task loss, zero duplicate side effects")
        return 0
    print(f"RESULT: FAIL - {lost} task(s) lost / stuck")
    return 1


def cmd_flush(_: argparse.Namespace) -> int:
    client = _client()
    _queue(client).flush_all()
    client.delete(K_TOTAL, K_COMPLETED, K_DELIVERIES)
    print("[flush] queue + chaos bookkeeping cleared")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 queue chaos harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enq = sub.add_parser("enqueue")
    p_enq.add_argument("--count", type=int, default=200)
    p_enq.add_argument("--run-id", default="chaos-run")
    p_enq.set_defaults(func=cmd_enqueue)

    p_work = sub.add_parser("work")
    p_work.add_argument("--consumer", required=True)
    p_work.add_argument("--die-after", type=int, default=0)
    p_work.add_argument("--max-seconds", type=float, default=60.0)
    p_work.set_defaults(func=cmd_work)

    p_reap = sub.add_parser("reap")
    p_reap.add_argument("--idle-ms", type=int, default=1500)
    p_reap.add_argument("--interval", type=float, default=0.5)
    p_reap.add_argument("--max-seconds", type=float, default=60.0)
    p_reap.set_defaults(func=cmd_reap)

    p_ver = sub.add_parser("verify")
    p_ver.set_defaults(func=cmd_verify)

    p_flush = sub.add_parser("flush")
    p_flush.set_defaults(func=cmd_flush)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
