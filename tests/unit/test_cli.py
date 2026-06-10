"""T-0702 through T-0706: CLI queue and dlq commands."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from hc.cli.main import cli
from hc.models.task import TaskSpec, compute_spec_hash
from hc.queue.redis_queue import RedisQueue

pytestmark = pytest.mark.unit


def _task(tc_id: str = "TC-CLI") -> TaskSpec:
    spec = {"action": "cli-test"}
    return TaskSpec(
        run_id="run-cli",
        tc_id=tc_id,
        tenant_id="tenant-a",
        spec_hash=compute_spec_hash(spec),
        spec=spec,
    )


def test_t0701_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "queue" in result.output


def test_t0702_cli_queue_stats(queue: RedisQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    queue.enqueue(_task())
    queue.consume("hc-workers-test", "w1", block_ms=100)

    def _fake_queue() -> RedisQueue:
        return queue

    monkeypatch.setattr("hc.cli.main._queue", _fake_queue)
    runner = CliRunner()
    result = runner.invoke(cli, ["queue", "stats"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "pel_depth" in data
    assert "dlq_depth" in data


def test_t0703_cli_queue_peek(queue: RedisQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    queue.enqueue(_task())
    monkeypatch.setattr("hc.cli.main._queue", lambda: queue)
    runner = CliRunner()
    result = runner.invoke(cli, ["queue", "peek", "--count", "5"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) >= 1


def test_t0704_cli_dlq_list(queue: RedisQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    t = _task()
    t.attempt = 3
    queue.enqueue(t)
    entry = queue.consume("hc-workers-test", "w1", block_ms=100)
    assert entry
    queue.nack(entry.entry_id, "fail")
    monkeypatch.setattr("hc.cli.main._queue", lambda: queue)
    runner = CliRunner()
    result = runner.invoke(cli, ["dlq", "list"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) >= 1
    assert "failed_at" in data[0]


def test_t0705_cli_dlq_replay(queue: RedisQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    t = _task("TC-REPLAY")
    t.attempt = 3
    queue.enqueue(t)
    entry = queue.consume("hc-workers-test", "w1", block_ms=100)
    assert entry
    queue.nack(entry.entry_id, "fail")
    dlq_id = queue._r.xrevrange(queue.settings.stream_dlq, count=1)[0][0]
    monkeypatch.setattr("hc.cli.main._queue", lambda: queue)
    runner = CliRunner()
    result = runner.invoke(cli, ["dlq", "replay", dlq_id])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "new_entry_id" in data


def test_t0706_cli_dlq_replay_bad_id(queue: RedisQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hc.cli.main._queue", lambda: queue)
    runner = CliRunner()
    result = runner.invoke(cli, ["dlq", "replay", "nonexistent-0"])
    assert result.exit_code == 1
