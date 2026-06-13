"""Health-check producer tests."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from hc.cli.main import cli
from hc.producer import create_health_checks
from hc.queue.redis_queue import RedisQueue


def _spec_file(path: Path, count: int = 3) -> Path:
    checks = [
        {
            "tc_id": f"TC-{i:03d}",
            "category": "compute",
            "description": f"Health check {i}",
            "spec": {"module": "vm", "name": f"hc-{i}"},
        }
        for i in range(1, count + 1)
    ]
    path.write_text(json.dumps({"checks": checks}), encoding="utf-8")
    return path


def test_create_health_checks_enqueues_at_most_two(
    queue: RedisQueue,
    tmp_path: Path,
) -> None:
    spec = _spec_file(tmp_path / "checks.json", count=4)
    log_file = tmp_path / "log.html"

    result = create_health_checks(
        queue=queue,
        spec_path=spec,
        run_id="run-a",
        tenant_id="tenant-a",
        log_path=log_file,
    )

    assert result == {"loaded": 4, "enqueued": 2, "duplicates": 0, "deferred": 2}
    assert len(queue.peek(count=10)) == 2
    html = log_file.read_text(encoding="utf-8")
    assert "limit_jobs" in html
    assert "deferred 2" in html


def test_create_health_checks_deduplicates_same_run(
    queue: RedisQueue,
    tmp_path: Path,
) -> None:
    spec = _spec_file(tmp_path / "checks.json", count=2)

    first = create_health_checks(
        queue=queue,
        spec_path=spec,
        run_id="run-a",
        tenant_id="tenant-a",
        log_path=tmp_path / "first.html",
    )
    second = create_health_checks(
        queue=queue,
        spec_path=spec,
        run_id="run-a",
        tenant_id="tenant-a",
        log_path=tmp_path / "second.html",
    )

    assert first["enqueued"] == 2
    assert second["enqueued"] == 0
    assert second["duplicates"] == 2
    assert len(queue.peek(count=10)) == 2


def test_cli_health_checks_create(
    queue: RedisQueue,
    monkeypatch,
    tmp_path: Path,
) -> None:
    spec = _spec_file(tmp_path / "checks.json", count=3)
    log_file = tmp_path / "log.html"
    monkeypatch.setattr("hc.cli.main._queue", lambda: queue)

    result = CliRunner().invoke(
        cli,
        [
            "health-checks",
            "create",
            "--spec",
            str(spec),
            "--run-id",
            "run-cli",
            "--tenant-id",
            "tenant-a",
            "--log-file",
            str(log_file),
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["enqueued"] == 2
    assert data["deferred"] == 1
    assert log_file.exists()
