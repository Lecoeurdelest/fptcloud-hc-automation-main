"""Structured JSON event logging for a health-check run.

The JSON event log is the source of truth: ``write_log`` writes the JSON log
(per-run and root alias) and the queue snapshots, then derives the HTML view
from the same events. No business logic lives here.

Note: this module intentionally shadows the stdlib ``logging`` name (per the
spec module map). Peers import it as ``from healthcheck import logging as hclog``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from html import escape
from string import Template

from healthcheck import state
from healthcheck.classification import _extract_classification
from healthcheck.models import QueueItem
from healthcheck.state import error_queue, events, pending_queue, stage_status


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value.lower())


def status_class(status: str) -> str:
    normalized = status.lower()
    if normalized in {"destroyed", "done", "locked", "ok", "passed", "ready", "unlocked"}:
        return "ok"
    if normalized in {"blocked", "pending", "queued", "retry", "skipped", "waiting"}:
        return "warn"
    if normalized in {"error", "failed"}:
        return "error"
    return "info"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {remaining_minutes}m {remaining_seconds}s"
    if remaining_minutes:
        return f"{remaining_minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def estimated_run_total(elapsed_seconds: float) -> str:
    total_stages = len(stage_status)
    completed_stages = sum(
        1 for status in stage_status.values() if status_class(status) in {"ok", "warn", "error"}
    )
    if total_stages <= 0 or completed_stages <= 0:
        return "calculating"
    estimated_seconds = elapsed_seconds * (total_stages / completed_stages)
    return format_duration(estimated_seconds)


def timing_summary() -> str:
    elapsed_seconds = time.monotonic() - state.RUN_STARTED_AT
    if events and events[-1]["stage"] == "run" and events[-1]["status"] == "done":
        return f"Elapsed runtime: {format_duration(elapsed_seconds)}. Final runtime recorded."
    return (
        f"Elapsed runtime: {format_duration(elapsed_seconds)}. "
        f"Estimated total runtime: {estimated_run_total(elapsed_seconds)}."
    )


def _extract_kv(text: str, key: str) -> str:
    """Extract value for exact key from semicolon-separated key=value text.

    Matches only when key appears at start-of-string or after a semicolon so
    that 'attempt' does not match inside 'remaining_retry_attempts'.
    """
    m = re.search(rf"(?:^|;\s*){re.escape(key)}=([^;]+?)(?:\s*;|$)", text)
    return m.group(1).strip() if m else ""


def emit(stage: str, status: str, message: str, resources: list[str] | None = None) -> None:
    # Governed by specs/health-check.json LOG_EVENT_SCHEMA.
    details = f"{message} Resources: {', '.join(resources)}" if resources else message
    short_msg = details.split(";")[0].strip()[:200]
    os_lbl = _extract_kv(details, "os_label") or _extract_kv(details, "image_label")
    attempt_raw = _extract_kv(details, "attempt")
    events.append(
        {
            "timestamp": now(),
            "run_id": state.RUN_ID,
            "stage": stage,
            "status": status,
            "message": short_msg,
            "details": details,
            "classification": _extract_classification(details),
            "resource": (resources or [""])[0],
            "os_label": os_lbl,
            "attempt": int(attempt_raw) if attempt_raw and attempt_raw.isdigit() else 0,
        }
    )
    if ":" not in stage:
        stage_status[stage] = status
    write_log()


def stage_ok(stage: str) -> bool:
    return stage_status.get(stage) in {"done", "passed", "ready"}


def write_queues() -> None:
    state.RUN_ROOT.mkdir(parents=True, exist_ok=True)
    (state.RUN_ROOT / "pending_queue.json").write_text(
        json.dumps([asdict(item) for item in pending_queue], indent=2),
        encoding="utf-8",
    )
    (state.RUN_ROOT / "error_queue.json").write_text(
        json.dumps([asdict(item) for item in error_queue], indent=2),
        encoding="utf-8",
    )


def write_log() -> None:
    # Governed by specs/health-check.json LOG_FORMAT_POLICY.
    # Primary: JSON log written to runs/<run_id>/log.json and root log.json alias.
    write_queues()
    serialised = json.dumps(events, indent=2, ensure_ascii=False)
    state.RUN_ROOT.mkdir(parents=True, exist_ok=True)
    (state.RUN_ROOT / "log.json").write_text(serialised, encoding="utf-8")
    state.JSON_LOG_PATH.write_text(serialised, encoding="utf-8")

    # Derived HTML view — rendered from events for human reading in browser.
    rows = "\n".join(
        "<tr>"
        f"<td>{escape(event['timestamp'])}</td>"
        f"<td>{escape(event['stage'])}</td>"
        f'<td><span class="badge {status_class(event["status"])}">{escape(event["status"])}</span></td>'
        f"<td>{escape(event['details'])}</td>"
        "</tr>"
        for event in events
    )
    failures = sum(1 for event in events if status_class(event["status"]) == "error")
    warnings = sum(1 for event in events if status_class(event["status"]) == "warn")
    summary = (
        f"{len(events)} events recorded, {failures} failed and {warnings} pending/skipped. "
        f"Pending queue: {len(pending_queue)} item(s). Error queue: {len(error_queue)} item(s)."
    )
    html = Template(state.TEMPLATE.read_text(encoding="utf-8")).safe_substitute(
        rows=rows,
        summary=summary,
        timing=timing_summary(),
    )
    state.LOG_PATH.write_text(html, encoding="utf-8")


def queue_pending(check: str, workspace, resources: list[str], reason: str) -> None:
    pending_queue.append(QueueItem(check, str(workspace), resources, reason, now()))
    emit(check, "queued", f"Queued for pending readiness: {reason}", resources)


def queue_error(check: str, workspace, resources: list[str], reason: str) -> None:
    error_queue.append(QueueItem(check, str(workspace), resources, reason, now()))
    emit(check, "failed", reason, resources)
