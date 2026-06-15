"""Report rendering, filtered views, redaction, and failure formatting.

All views are derived from the JSON event log. No health-check execution logic.
"""

from __future__ import annotations

import json
from typing import Any

from healthcheck import config, state
from healthcheck.classification import QUOTA_CLEANUP_CLASSIFICATIONS
from healthcheck.models import FailureContext

SECRET_VAR_PARTS = ("password", "ssh_key", "token", "secret", "private")

# Governed by specs/health-check.json FILTERED_OUTPUT_MODES.
FILTER_CHOICES = (
    "summary",
    "failed",
    "blocked",
    "queued",
    "retained_resources",
    "created_resources",
)


def redact_value(key: str, value: Any) -> Any:
    if any(part in key.lower() for part in SECRET_VAR_PARTS):
        return "<redacted>" if value not in (None, "", [], {}) else value
    if isinstance(value, dict):
        return redacted_vars(value)
    if isinstance(value, list):
        return [redact_value(key, item) for item in value]
    return value


def redacted_vars(values: dict[str, Any]) -> dict[str, Any]:
    return {key: redact_value(key, value) for key, value in values.items()}


def redact_text(text: str) -> str:
    if state.GENERATED_INSTANCE_PASSWORD:
        text = text.replace(state.GENERATED_INSTANCE_PASSWORD, "<redacted>")
    return text


def cleanup_policy_summary(
    *,
    classification: str = "",
    delete_reason: str = "",
    delete_allowed: bool = False,
    retained_instance_ids: list[str] | None = None,
    deleted_instance_ids: list[str] | None = None,
    skipped_delete_reason: str = "",
) -> str:
    return (
        "cleanup_policy=retain_by_default; "
        f"keep_instance={config.keep_instance_enabled()}; "
        f"cleanup_on_quota_exceeded={config.cleanup_on_quota_exceeded_enabled()}; "
        f"delete_allowed={delete_allowed}; "
        f"delete_reason={delete_reason or ('quota cleanup' if classification in QUOTA_CLEANUP_CLASSIFICATIONS else 'not quota cleanup')}; "
        f"retained_instance_ids={','.join(retained_instance_ids or []) or '<none>'}; "
        f"deleted_instance_ids={','.join(deleted_instance_ids or []) or '<none>'}; "
        f"skipped_delete_reason={skipped_delete_reason or '<none>'}"
    )


def filter_events(all_events: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """Return a filtered subset of events for the given output mode."""
    if mode == "summary":
        seen: dict[str, dict[str, Any]] = {}
        for ev in all_events:
            if ":" not in ev["stage"]:
                seen[ev["stage"]] = ev
        return list(seen.values())
    if mode == "failed":
        return [ev for ev in all_events if ev["status"] in ("failed", "blocked", "error")]
    if mode == "blocked":
        return [ev for ev in all_events if ev["status"] == "blocked"]
    if mode == "queued":
        return [ev for ev in all_events if ev["status"] == "queued"]
    if mode == "retained_resources":
        return [ev for ev in all_events if "retained_instance_ids" in ev.get("details", "")]
    if mode == "created_resources":
        return [
            ev
            for ev in all_events
            if ev["status"] in ("passed", "ready") and "instance_id=" in ev.get("details", "")
        ]
    return all_events


def render_table(all_events: list[dict[str, Any]], mode: str) -> str:
    """Render a filtered event list as a concise fixed-width text table (AI-friendly)."""
    filtered = filter_events(all_events, mode)
    run_id = filtered[0].get("run_id", "?") if filtered else "?"
    header = f"{'TIMESTAMP':<24}  {'STAGE':<44}  {'STATUS':<10}  {'OS':<14}  {'AT':>2}  MESSAGE"
    sep = "-" * min(len(header) + 40, 140)
    lines = [f"filter={mode}  run_id={run_id}  events={len(filtered)}", sep, header, sep]
    for ev in filtered:
        ts = str(ev.get("timestamp", ""))[:19]
        stage = str(ev.get("stage", ""))[:44]
        status = str(ev.get("status", ""))[:10]
        os_lbl = str(ev.get("os_label", ""))[:14]
        attempt = str(ev.get("attempt") or "")
        msg = str(ev.get("message", ""))[:80]
        lines.append(f"{ts:<24}  {stage:<44}  {status:<10}  {os_lbl:<14}  {attempt:>2}  {msg}")
    if not filtered:
        lines.append(f"  (no events match filter={mode})")
    return "\n".join(lines)


def format_failure(context: FailureContext) -> str:
    details = (
        f"{context.reason}\n"
        f"Classification: {context.classification}\n"
        f"Resource type: {context.resource_type}\n"
        f"Address: {context.address}\n"
        f"Module path: {context.module_path}\n"
        f"Tenant: {context.tenant or '<unset>'}; Region: {context.region or '<unset>'}; "
        f"VPC: {context.vpc_id or '<unset>'}"
    )
    if context.attempted_cidr or context.attempted_gateway:
        details += (
            f"\nAttempted subnet CIDR: {context.attempted_cidr or '<unset>'}; "
            f"Attempted gateway: {context.attempted_gateway or '<unset>'}"
        )
    if context.conflicting_subnet:
        details += f"\nConflicting subnet: {context.conflicting_subnet}"
    if context.classification == "subnet_cidr_overlap":
        details += "\nFailure type: input/environment conflict; choose an unused subnet CIDR/gateway inside the VPC range."
    return details


def format_quota_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return "not_available"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def quota_report_message(report: dict[str, Any]) -> str:
    fields = [
        "quota_precheck",
        "quota_assumption",
        "quota_exceeded_action",
        "quota_source",
        "quota_status",
        "stop_on_quota_exceeded",
        "run_status",
        "user_action_required",
        "tenant_quota",
        "vpc_quota",
        "used_storage_gb",
        "remaining_storage_gb",
        "storage_policy_quota_gb",
        "storage_policy_used_gb",
        "vm_count_quota",
        "vm_count_used",
        "vm_count_remaining",
        "cpu_quota",
        "cpu_used",
        "cpu_remaining",
        "ram_quota_mb",
        "ram_used_mb",
        "ram_remaining_mb",
        "disk_quota_gb",
        "existing_instances_consuming_storage",
        "existing_volumes_consuming_storage",
        "target_requested_disk_size_gb",
        "requested_instance_count",
        "requested_total_storage_gb",
        "requested_cpu",
        "requested_ram_mb",
        "reduced_disk_test",
        "provider_schema_quota_fields",
        "provider_instance_inventory",
        "provider_storage_inventory",
    ]
    return "; ".join(f"{field}={format_quota_value(report.get(field))}" for field in fields)
