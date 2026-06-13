"""Small self-contained HTML progress log writer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from importlib import resources
from pathlib import Path
from string import Template


@dataclass(frozen=True)
class LogEvent:
    stage: str
    status: str
    message: str
    timestamp: datetime


class HtmlProgressLog:
    """Append-only progress log rendered to a standalone HTML file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._events: list[LogEvent] = []

    @property
    def path(self) -> Path:
        return self._path

    def record(self, stage: str, status: str, message: str) -> None:
        self._events.append(
            LogEvent(
                stage=stage,
                status=status,
                message=message,
                timestamp=datetime.now(UTC),
            )
        )
        self.write()

    def write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = "\n".join(self._row(event) for event in self._events)
        html = Template(_html_template()).safe_substitute(
            rows=rows or self._empty_row(),
            summary=self._summary(),
        )
        self._path.write_text(html, encoding="utf-8")

    @staticmethod
    def _row(event: LogEvent) -> str:
        status_class = _status_class(event.status)
        timestamp = event.timestamp.isoformat(timespec="seconds")
        return (
            "<tr>"
            f"<td>{escape(timestamp)}</td>"
            f"<td>{escape(event.stage)}</td>"
            f'<td><span class="badge {status_class}">{escape(event.status)}</span></td>'
            f"<td>{escape(event.message)}</td>"
            "</tr>"
        )

    def _summary(self) -> str:
        total = len(self._events)
        errors = sum(1 for event in self._events if _status_class(event.status) == "error")
        warnings = sum(1 for event in self._events if _status_class(event.status) == "warn")
        if total == 0:
            return "No stages recorded yet."
        if errors:
            return f"{total} stages recorded, {errors} require attention."
        if warnings:
            return f"{total} stages recorded, {warnings} pending or duplicate."
        return f"{total} stages recorded successfully."

    @staticmethod
    def _empty_row() -> str:
        return (
            '<tr class="empty">'
            '<td colspan="4">No progress events have been recorded yet.</td>'
            "</tr>"
        )


def _status_class(status: str) -> str:
    normalized = status.lower()
    if normalized in {"ok", "done", "enqueued"}:
        return "ok"
    if normalized in {"skipped", "duplicate", "pending"}:
        return "warn"
    if normalized in {"error", "failed"}:
        return "error"
    return "info"


def _html_template() -> str:
    return resources.files(__package__).joinpath("html_log.html").read_text(encoding="utf-8")
