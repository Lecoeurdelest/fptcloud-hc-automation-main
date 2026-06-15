from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import argparse
import ipaddress
import re
import secrets
import zipfile
from dataclasses import asdict, dataclass
from html import escape
from pathlib import Path
from string import Template, ascii_lowercase, digits
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from diagnose_health_inputs import DOTENV_RESULT as INPUT_DOTENV_RESULT  # noqa: E402
from diagnose_health_inputs import diagnostics as input_diagnostics, effective_config, looks_uuid  # noqa: E402
from hc.inventory.fptcloud_inventory import (  # noqa: E402
    list_vpc_instances,
    select_oldest_reclaimable,
)


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = time.strftime("hc-%Y%m%d-%H%M%S")
RUN_STARTED_AT = time.monotonic()
RUN_ROOT = ROOT / "runs" / RUN_ID
INSTANCE_RUN_SUFFIX = "".join(secrets.choice(ascii_lowercase + digits) for _ in range(12))
PASSWORD_SPECIALS = "?@*%$&!#"
PASSWORD_MIN_LENGTH = 12


def generate_instance_password(length: int = 24) -> str:
    if length < PASSWORD_MIN_LENGTH:
        raise ValueError(f"generated instance password length must be at least {PASSWORD_MIN_LENGTH}")
    alphabet = ascii_lowercase + ascii_lowercase.upper() + digits + PASSWORD_SPECIALS
    required = [
        secrets.choice(ascii_lowercase),
        secrets.choice(ascii_lowercase.upper()),
        secrets.choice(digits),
        secrets.choice(PASSWORD_SPECIALS),
    ]
    remaining = [secrets.choice(alphabet) for _ in range(length - len(required))]
    chars = required + remaining
    for index in range(len(chars) - 1, 0, -1):
        swap = secrets.randbelow(index + 1)
        chars[index], chars[swap] = chars[swap], chars[index]
    return "".join(chars)


GENERATED_INSTANCE_PASSWORD = generate_instance_password()
LOG_PATH = ROOT / "log.html"
JSON_LOG_PATH = ROOT / "log.json"
MODULES = ROOT / "modules"
TEMPLATE = ROOT / "src" / "hc" / "reporter" / "html_log.html"
LOCK_ROOT = ROOT / "runs" / ".locks"
SPEC_PATH = ROOT / "specs" / "health-check.json"


SECRET_ENV_PARTS = ("TOKEN", "PASSWORD", "SECRET", "KEY_MATERIAL", "PRIVATE")
SECRET_ENV_EXACT = {"HC_SSH_KEY"}
REQUIRED_ENV_PRESENCE = (
    "FPTCLOUD_TOKEN",
    "FPTCLOUD_REGION",
    "FPTCLOUD_TENANT_NAME",
)


def parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return (key, value) if key else None


def load_dotenv(path: Path = ROOT / ".env", *, override: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "cwd": str(Path.cwd()),
        "path": str(path),
        "found": path.exists(),
        "loaded": [],
        "skipped_existing": [],
    }
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        parsed = parse_dotenv_line(raw)
        if not parsed:
            continue
        key, value = parsed
        if key in os.environ and not override:
            result["skipped_existing"].append(key)
            continue
        os.environ[key] = value
        result["loaded"].append(key)
    return result


def env_present(requirement: str) -> bool:
    if " or " in requirement:
        return any(env_present(part.strip()) for part in requirement.split(" or "))
    return bool(os.environ.get(requirement, "").strip())


def env_presence_report() -> str:
    parts = []
    for requirement in REQUIRED_ENV_PRESENCE:
        parts.append(f"{requirement}={'present' if env_present(requirement) else 'missing'}")
    parts.append(f"generated_instance_password={'present' if GENERATED_INSTANCE_PASSWORD else 'missing'}")
    parts.append("password_generated=True" if GENERATED_INSTANCE_PASSWORD else "password_generated=False")
    parts.append("password_redacted=True")
    return "; ".join(parts)


DOTENV_RESULT = INPUT_DOTENV_RESULT if INPUT_DOTENV_RESULT.get("found") else load_dotenv()

SETTLE_SECONDS = int(os.environ.get("HC_SETTLE_SECONDS", "20"))
PENDING_POLL_SECONDS = int(os.environ.get("HC_PENDING_POLL_SECONDS", "15"))
PENDING_TIMEOUT_SECONDS = int(os.environ.get("HC_PENDING_TIMEOUT_SECONDS", "300"))
# Governed by specs/health-check.json INSTANCE_ERROR_QUEUE_RETRY_POLICY.max_attempts_per_case.
MAX_INSTANCE_CREATE_ATTEMPTS = 3

READY_STATUSES = {
    "ACTIVE",
    "AVAILABLE",
    "COMPLETED",
    "CREATED",
    "ENABLED",
    "OK",
    "PASS",
    "PASSED",
    "POWERED_ON",
    "READY",
    "RUNNING",
    "SUCCESS",
}
PENDING_STATUSES = {
    "BUILD",
    "BUILDING",
    "CREATE",
    "CREATING",
    "DEPLOYING",
    "IN_PROGRESS",
    "PENDING",
    "PROVISIONING",
    "STARTING",
    "UPDATING",
}

SUBNET_VALIDATION_STAGE = "compute.validate-subnet-inputs"
SUBNET_CREATE_STAGE = "compute.create-subnet"
SUBNET_EVIDENCE_STAGE = "compute.collect-subnet-create-evidence"
VPC_DISCOVERY_STAGE = "compute.discover-vpc"
EXISTING_SUBNETS_STAGE = "network.discover-existing-subnets"
INSTANCE_VALIDATE_STAGE = "compute.validate-instance-inputs"
INSTANCE_PASSWORD_POLICY_STAGE = "compute.validate-instance-password-policy"
INSTANCE_CREATE_STAGE = "compute.create-instance"
INSTANCE_CLEANUP_STAGE = "compute.cleanup-instance"
INSTANCE_IMAGE_DISCOVERY_STAGE = "compute.discover-instance-images"
INSTANCE_FLAVOR_DISCOVERY_STAGE = "compute.discover-instance-flavor"
INSTANCE_NETWORK_VALIDATE_STAGE = "compute.validate-instance-network-inputs"
INSTANCE_STORAGE_POLICY_VALIDATE_STAGE = "compute.validate-instance-storage-policy"
INSTANCE_HOSTNAME_VALIDATE_STAGE = "compute.validate-instance-hostname"
INSTANCE_QUOTA_INSPECT_STAGE = "compute.inspect-instance-quota"
INSTANCE_QUOTA_VALIDATE_STAGE = "compute.validate-instance-quota"
INSTANCE_ROUND_SELECT_STAGE = "compute.select-instance-round"
INSTANCE_IMAGE_MATRIX = (
    ("windows-2012", "HC_IMAGE_WINDOWS_2012"),
    ("windows-2016", "HC_IMAGE_WINDOWS_2016"),
    ("windows-2019", "HC_IMAGE_WINDOWS_2019"),
    ("windows-2022", "HC_IMAGE_WINDOWS_2022"),
    ("ubuntu-16-04", "HC_IMAGE_UBUNTU_16_04"),
    ("ubuntu-18-04", "HC_IMAGE_UBUNTU_18_04"),
    ("ubuntu-20-04", "HC_IMAGE_UBUNTU_20_04"),
    ("ubuntu-22-04", "HC_IMAGE_UBUNTU_22_04"),
)


@dataclass(frozen=True)
class Check:
    name: str
    module: str
    vars: dict[str, Any]
    required_env: tuple[str, ...] = ()
    required_vars: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    retries: int = 0
    stop_group_on_success: str | None = None


@dataclass(frozen=True)
class CandidateState:
    start_cidr: str
    start_gateway: str
    max_attempts: int
    rejected_cidrs: tuple[str, ...] = ()
    conflict_sources: tuple[str, ...] = ()
    conflicting_subnets: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageSpec:
    id: str
    manual_check_item: str
    automation_status: str
    required_inputs: tuple[str, ...]
    required_cloud_resources: tuple[str, ...]
    expected_result: str
    validation_method: str
    cleanup_behavior: str
    dependency_stages: tuple[str, ...]
    failure_classification: str
    safe_for_daily_run: bool


@dataclass(frozen=True)
class QueueItem:
    check: str
    workspace: str
    resources: list[str]
    reason: str
    queued_at: str


@dataclass(frozen=True)
class FailureContext:
    stage: str
    resource_type: str
    address: str
    module_path: str
    tenant: str
    region: str
    vpc_id: str
    reason: str
    classification: str
    attempted_cidr: str = ""
    attempted_gateway: str = ""
    conflicting_subnet: str = ""


@dataclass
class _ImageCreateResult:
    """Result of one create attempt for a single image instance.

    Governed by specs/health-check.json INSTANCE_ERROR_QUEUE_RETRY_POLICY.
    """
    label: str
    succeeded: bool
    is_quota: bool
    retryable: bool
    classification: str
    error_code: str
    terraform_error: str
    workspace: Path
    resources: list[str]
    context: FailureContext | None
    failed_instance_id: str


events: list[dict[str, str]] = []
pending_queue: list[QueueItem] = []
error_queue: list[QueueItem] = []
stage_status: dict[str, str] = {}
run_context: dict[str, Any] = {
    "vpc_name": "",
    "explicit_vpc_id": "",
    "discovered_vpc_id": "",
    "effective_vpc_id": "",
    "vpc_id_source": "unresolved",
    "effective_subnet_id": "",
    "subnet_id_source": "unresolved",
    "effective_storage_policy_id": "",
    "storage_policy_id_source": "unresolved",
    "storage_policy_requested": "",
    "selected_storage_policy_name": "",
    "selected_storage_policy_id": "",
    "selected_storage_policy_db_id": "",
    "selected_storage_policy_provider_field_used": "storage_policy_id",
    "selected_storage_policy_quota_status": "not_available",
    "discovered_storage_policies": [],
    "validated_instance_hostnames": {},
    "discovered_instance_images": {},
    "instance_image_sources": {},
    "discovered_instance_flavor": "",
    "discovered_instance_flavor_name": "",
    "instance_flavor_source": "unresolved",
    "instance_quota": {},
    "selected_instance_round": {},
    "run_status": "running",
    "run_blocked": False,
    "user_action_required": False,
    "remaining_images_not_attempted": [],
}
existing_subnet_inventory: list[dict[str, str]] = []
instance_validation: dict[str, Any] = {"valid": False, "vars": {}, "diagnostics": {}, "errors": []}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str) -> bool:
    return env(name).lower() == "true"


def env_bool_default(name: str, default: bool) -> bool:
    raw = env(name)
    if not raw:
        return default
    return raw.lower() == "true"


QUOTA_CLEANUP_CLASSIFICATIONS = {"instance_quota_exceeded", "instance_storage_quota_exceeded"}


def keep_instance_enabled() -> bool:
    # specs/health-check.json INSTANCE_CLEANUP_POLICY: HC_KEEP_INSTANCE defaults to true.
    return env_bool_default("HC_KEEP_INSTANCE", True)


def cleanup_on_quota_exceeded_enabled() -> bool:
    # specs/health-check.json INSTANCE_CLEANUP_POLICY: HC_CLEANUP_ON_QUOTA_EXCEEDED defaults to false.
    return env_bool_default("HC_CLEANUP_ON_QUOTA_EXCEEDED", False)


def instance_batching_policy() -> dict[str, Any]:
    # Governed by specs/health-check.json INSTANCE_BATCHING_POLICY.
    policy = spec_constants().get("INSTANCE_BATCHING_POLICY", {})
    return policy if isinstance(policy, dict) else {}


def env_int_default(name: str, default: int) -> int:
    raw = env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def batching_int(name: str, fallback: int) -> int:
    value = instance_batching_policy().get(name, fallback)
    try:
        default = int(value)
    except (TypeError, ValueError):
        default = fallback
    return max(0, env_int_default(name, default))


def instances_per_apply() -> int:
    # specs/health-check.json INSTANCE_BATCHING_POLICY: health checks create exactly one VM per apply.
    return 1


def stop_on_quota_exceeded_enabled() -> bool:
    # Quota handling is optimistic-apply-only: any provider-side quota rejection
    # stops the run and waits for explicit user confirmation.
    return True


def instance_selection_order() -> list[str]:
    order = instance_batching_policy().get("selection_order", [])
    if isinstance(order, list) and order:
        return [str(item) for item in order]
    return ["windows-2012", "windows-2016", "windows-2019", "windows-2022", "ubuntu-20-04", "ubuntu-22-04"]


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
        f"keep_instance={keep_instance_enabled()}; "
        f"cleanup_on_quota_exceeded={cleanup_on_quota_exceeded_enabled()}; "
        f"delete_allowed={delete_allowed}; "
        f"delete_reason={delete_reason or ('quota cleanup' if classification in QUOTA_CLEANUP_CLASSIFICATIONS else 'not quota cleanup')}; "
        f"retained_instance_ids={','.join(retained_instance_ids or []) or '<none>'}; "
        f"deleted_instance_ids={','.join(deleted_instance_ids or []) or '<none>'}; "
        f"skipped_delete_reason={skipped_delete_reason or '<none>'}"
    )


SECRET_VAR_PARTS = ("password", "ssh_key", "token", "secret", "private")


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
    if GENERATED_INSTANCE_PASSWORD:
        text = text.replace(GENERATED_INSTANCE_PASSWORD, "<redacted>")
    return text


def _extract_kv(text: str, key: str) -> str:
    """Extract value for exact key from semicolon-separated key=value text.

    Matches only when key appears at start-of-string or after a semicolon so
    that 'attempt' does not match inside 'remaining_retry_attempts'.
    """
    m = re.search(rf'(?:^|;\s*){re.escape(key)}=([^;]+?)(?:\s*;|$)', text)
    return m.group(1).strip() if m else ""


def _extract_classification(text: str) -> str:
    """Extract failure classification from 'Classification: value' or 'classification=value'."""
    m = re.search(r'Classification:\s*([A-Za-z_]+)', text)
    if m:
        return m.group(1)
    m = re.search(r'(?:^|;\s*)classification=([A-Za-z_]+)', text)
    return m.group(1) if m else ""


# Governed by specs/health-check.json FILTERED_OUTPUT_MODES.
FILTER_CHOICES = ("summary", "failed", "blocked", "queued", "retained_resources", "created_resources")


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
            ev for ev in all_events
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
    elapsed_seconds = time.monotonic() - RUN_STARTED_AT
    if events and events[-1]["stage"] == "run" and events[-1]["status"] == "done":
        return f"Elapsed runtime: {format_duration(elapsed_seconds)}. Final runtime recorded."
    return (
        f"Elapsed runtime: {format_duration(elapsed_seconds)}. "
        f"Estimated total runtime: {estimated_run_total(elapsed_seconds)}."
    )


def emit(stage: str, status: str, message: str, resources: list[str] | None = None) -> None:
    # Governed by specs/health-check.json LOG_EVENT_SCHEMA.
    details = f"{message} Resources: {', '.join(resources)}" if resources else message
    short_msg = details.split(";")[0].strip()[:200]
    os_lbl = _extract_kv(details, "os_label") or _extract_kv(details, "image_label")
    attempt_raw = _extract_kv(details, "attempt")
    events.append({
        "timestamp": now(),
        "run_id": RUN_ID,
        "stage": stage,
        "status": status,
        "message": short_msg,
        "details": details,
        "classification": _extract_classification(details),
        "resource": (resources or [""])[0],
        "os_label": os_lbl,
        "attempt": int(attempt_raw) if attempt_raw and attempt_raw.isdigit() else 0,
    })
    if ":" not in stage:
        stage_status[stage] = status
    write_log()


def stage_ok(stage: str) -> bool:
    return stage_status.get(stage) in {"done", "passed", "ready"}


def load_spec(path: Path = SPEC_PATH) -> dict[str, StageSpec]:
    data = json.loads(path.read_text(encoding="utf-8"))
    stages: dict[str, StageSpec] = {}
    for raw in data.get("stages", []):
        stage = StageSpec(
            id=raw["id"],
            manual_check_item=raw["manual_check_item"],
            automation_status=raw["automation_status"],
            required_inputs=tuple(raw.get("required_inputs", [])),
            required_cloud_resources=tuple(raw.get("required_cloud_resources", [])),
            expected_result=raw["expected_result"],
            validation_method=raw["validation_method"],
            cleanup_behavior=raw["cleanup_behavior"],
            dependency_stages=tuple(raw.get("dependency_stages", [])),
            failure_classification=raw["failure_classification"],
            safe_for_daily_run=bool(raw["safe_for_daily_run"]),
        )
        stages[stage.id] = stage
    return stages


def spec_constants(path: Path = SPEC_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("constants", {})


def max_subnet_candidate_attempts() -> int:
    value = spec_constants().get("MAX_SUBNET_CANDIDATE_ATTEMPTS")
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, attempts)


def runnable_spec(stage: StageSpec) -> tuple[bool, str]:
    if stage.automation_status != "automated":
        return False, f"Spec status is {stage.automation_status}, not automated"
    if not stage.safe_for_daily_run:
        return False, "Spec marks stage unsafe for daily run"
    cleanup = stage.cleanup_behavior.lower()
    if (
        stage.required_cloud_resources
        and "destroy" not in cleanup
        and "no resources" not in cleanup
        and "retain" not in cleanup
        and "quota cleanup" not in cleanup
    ):
        return False, "Spec does not define safe cleanup"
    return True, "spec permits execution"


def cloud_context() -> dict[str, str]:
    return {
        "tenant": env("FPTCLOUD_TENANT_NAME"),
        "region": env("FPTCLOUD_REGION"),
        "vpc_id": run_context.get("effective_vpc_id") or effective_config()["vpc_id"],
    }


def vpc_lookup_key() -> str:
    values = [value.strip() for value in (env("VPC_IDS") or env("VPC_ID")).split(",") if value.strip()]
    return env("HC_VPC_NAME") or env("VPC_NAME") or env("VPC_ID") or (values[0] if values else "")


def update_vpc_context(*, discovered_vpc_id: str = "") -> dict[str, str]:
    explicit = env("HC_VPC_ID")
    if discovered_vpc_id:
        run_context["discovered_vpc_id"] = discovered_vpc_id
    discovered = run_context.get("discovered_vpc_id", "")
    effective = explicit or discovered
    source = "explicit" if explicit else ("discovered" if discovered else "unresolved")
    run_context.update(
        {
            "vpc_name": vpc_lookup_key(),
            "explicit_vpc_id": explicit,
            "effective_vpc_id": effective,
            "vpc_id_source": source,
        }
    )
    return run_context


def vpc_diagnostics_message(prefix: str = "VPC resolution") -> str:
    ctx = update_vpc_context()
    return (
        f"{prefix}: "
        f"vpc_name={ctx.get('vpc_name') or '<unset>'}; "
        f"explicit_vpc_id={ctx.get('explicit_vpc_id') or '<unset>'}; "
        f"discovered_vpc_id={ctx.get('discovered_vpc_id') or '<unset>'}; "
        f"effective_vpc_id={ctx.get('effective_vpc_id') or '<unset>'}; "
        f"vpc_id_source={ctx.get('vpc_id_source') or 'unresolved'}"
    )


def rolling_strategy_constants() -> dict[str, Any]:
    """Read ROLLING_INSTANCE_STRATEGY from health-check.json constants (C-014)."""
    return dict(spec_constants().get("ROLLING_INSTANCE_STRATEGY") or {})


def target_vpc_entries() -> list[tuple[str, str]]:
    """Return ordered list of (vpc_name, raw_entry) pairs from VPC_IDS env var.

    The raw_entry is the VPC name/identifier used for provider discovery.
    Returns at least the primary VPC from vpc_lookup_key() so the single-VPC
    path is preserved when VPC_IDS has only one entry.
    """
    raw = env("VPC_IDS") or env("VPC_ID") or ""
    entries = [v.strip() for v in raw.split(",") if v.strip()]
    if not entries:
        primary = vpc_lookup_key()
        return [(primary, primary)] if primary else []
    return [(e, e) for e in entries]


def build_hc_instance_tags(*, vpc_name: str, os_label: str, created_at: str) -> dict[str, str]:
    """Build the required health-check instance tags (spec §6.1, FR-017)."""
    return {
        "managed_by": "health-check",
        "health_check": "true",
        "hc_run_id": RUN_ID,
        "hc_created_at": created_at,
        "hc_vpc_name": vpc_name,
        "hc_os_label": os_label,
    }


def is_quota_error(classification: str) -> bool:
    return classification in ("instance_storage_quota_exceeded", "instance_quota_exceeded")


def write_import_workspace(instance_id: str, vpc_id: str, workspace: Path) -> Path:
    """Write a minimal Terraform workspace for import + targeted destroy.

    The workspace contains only fptcloud_instance.target (no module, no tags).
    Only fptcloud_instance is imported — tags are left as orphans (spec §7.3).
    """
    workspace.mkdir(parents=True, exist_ok=True)
    module_source = (MODULES / "vm").resolve().as_posix()
    (workspace / "main.tf").write_text(
        f"""
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    fptcloud = {{
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }}
  }}
}}

provider "fptcloud" {{}}

variable "vpc_id" {{}}
variable "name" {{}}
variable "image_name" {{ default = "placeholder" }}
variable "flavor_name" {{ default = "placeholder" }}
variable "storage_policy_id" {{ default = "placeholder" }}
variable "disk_gb" {{ default = 40 }}
variable "subnet_id" {{ default = "placeholder" }}
variable "status" {{ default = "POWERED_ON" }}
variable "password" {{ default = null; sensitive = true }}
variable "ssh_key" {{ default = null }}
variable "security_group_ids" {{ default = [] }}
variable "tags" {{ type = map(string); default = {{}} }}

module "this" {{
  source             = "{module_source}"
  vpc_id             = var.vpc_id
  name               = var.name
  image_name         = var.image_name
  flavor_name        = var.flavor_name
  storage_policy_id  = var.storage_policy_id
  disk_gb            = var.disk_gb
  subnet_id          = var.subnet_id
  status             = var.status
  password           = var.password
  ssh_key            = var.ssh_key
  security_group_ids = var.security_group_ids
  tags               = var.tags
}}
""".lstrip(),
        encoding="utf-8",
    )
    (workspace / "terraform.tfvars.json").write_text(
        json.dumps({"vpc_id": vpc_id, "name": "reclaim-placeholder"}, indent=2),
        encoding="utf-8",
    )
    return workspace


def terraform_reclaim_import_destroy(
    instance_id: str,
    vpc_id: str,
    workspace: Path,
    *,
    stage_prefix: str = "compute.reclaim-health-check-instance",
) -> bool:
    """Import an existing HC instance into an ephemeral workspace, then destroy it.

    Returns True on success, False (fail-closed) on any error.
    Governed by C-013 / FR-003: deletion via Terraform import+destroy only.
    No direct REST DELETE is used.
    """
    resources = [f"fptcloud_instance:{instance_id}"]

    init = run(["terraform", "init", "-input=false", "-no-color"], workspace, stage=f"{stage_prefix}-init")
    if init.returncode != 0:
        emit(stage_prefix, "failed",
             f"Classification: health_check_instance_delete_failed; "
             f"reason=terraform_init_failed; instance_id={instance_id}; "
             f"{(init.stderr or init.stdout)[-800:]}", resources)
        return False

    # Try terraform import.  If import itself is not supported, hard-stop (C-013/approval).
    import_result = run(
        ["terraform", "import", "-no-color", "-input=false",
         "module.this.fptcloud_instance.this", instance_id],
        workspace,
        stage=f"{stage_prefix}-import",
    )
    if import_result.returncode != 0:
        stderr = (import_result.stderr or import_result.stdout)
        unsupported = any(kw in stderr.lower() for kw in (
            "import is not supported", "does not support import",
            "not importable", "cannot import",
        ))
        if unsupported:
            emit(stage_prefix, "failed",
                 f"Classification: instance_reclaim_import_unsupported; "
                 f"reason=provider_does_not_support_fptcloud_instance_import; "
                 f"instance_id={instance_id}; hard_stop=true; "
                 f"{stderr[-800:]}", resources)
            emit("reclaim.import_unsupported", "failed",
                 f"instance_id={instance_id}; vpc_id={vpc_id}; "
                 f"no_deletion_performed=true", resources)
            return False
        emit(stage_prefix, "failed",
             f"Classification: health_check_instance_delete_failed; "
             f"reason=terraform_import_failed; instance_id={instance_id}; "
             f"{stderr[-800:]}", resources)
        return False

    # Import succeeded — now destroy only the imported instance resource.
    destroy_result = run(
        ["terraform", "destroy", "-auto-approve", "-no-color", "-input=false",
         "-target=module.this.fptcloud_instance.this"],
        workspace,
        stage=f"{stage_prefix}-destroy",
    )
    if destroy_result.returncode != 0:
        emit(stage_prefix, "failed",
             f"Classification: health_check_instance_delete_failed; "
             f"reason=terraform_destroy_failed; instance_id={instance_id}; "
             f"{(destroy_result.stderr or destroy_result.stdout)[-800:]}", resources)
        return False

    return True


def reclaim_health_check_instance(
    vpc_id: str,
    vpc_name: str,
    current_run_id: str,
) -> tuple[bool, str, str]:
    """Find and delete the oldest reclaimable HC instance in a VPC.

    Returns (success, instance_id, reason).
    Fail-closed: any ambiguity → (False, "", reason).
    Governed by specs §7.2–§7.3, FR-016, FR-017, NFR-013.
    """
    api_url = env("FPTCLOUD_API_URL") or ""
    token = env("FPTCLOUD_TOKEN") or ""
    stage = "compute.reclaim-health-check-instance"
    resources = [f"vpc:{vpc_id}"]

    if not api_url or not token:
        emit(stage, "failed",
             f"Classification: health_check_instance_not_found; "
             f"reason=missing_api_url_or_token; vpc_id={vpc_id}; vpc_name={vpc_name}; "
             f"no_deletion_performed=true", resources)
        return False, "", "missing_api_url_or_token"

    emit(stage, "started",
         f"Listing HC instances in vpc_id={vpc_id}; vpc_name={vpc_name}; "
         f"current_run_id={current_run_id}; read_only_inventory_call=true", resources)

    instances = list_vpc_instances(vpc_id, api_url, token)
    candidate = select_oldest_reclaimable(instances, current_run_id)

    if candidate is None:
        total = len(instances)
        hc_named = sum(1 for i in instances if i.is_hc_name())
        emit(stage, "failed",
             f"Classification: no_reclaimable_health_check_instance; "
             f"vpc_id={vpc_id}; vpc_name={vpc_name}; "
             f"total_instances={total}; hc_named_instances={hc_named}; "
             f"eligible_reclaimable=0; no_deletion_performed=true", resources)
        return False, "", "no_reclaimable_health_check_instance"

    emit(stage, "started",
         f"Selected candidate for reclamation: instance_id={candidate.instance_id}; "
         f"name={candidate.name}; status={candidate.status}; "
         f"created_at={candidate.created_at}; os_label={candidate.os_label}; "
         f"vpc_id={vpc_id}; vpc_name={vpc_name}; "
         f"deletion_mechanism=terraform_import_destroy", resources)

    reclaim_workspace = RUN_ROOT / "reclaim" / candidate.instance_id
    write_import_workspace(candidate.instance_id, vpc_id, reclaim_workspace)

    success = terraform_reclaim_import_destroy(
        candidate.instance_id, vpc_id, reclaim_workspace, stage_prefix=stage
    )
    if not success:
        return False, candidate.instance_id, "terraform_import_destroy_failed"

    emit(stage, "done",
         f"instance.deleted; instance_id={candidate.instance_id}; "
         f"name={candidate.name}; vpc_id={vpc_id}; vpc_name={vpc_name}; "
         f"deletion_mechanism=terraform_import_destroy; "
         f"tags_orphaned=true", resources)
    return True, candidate.instance_id, ""


def classify_error(text: str, resource_type: str) -> str:
    lowered = text.lower()
    if 'error_code":"804007' in lowered or "error_code=804007" in lowered or "804007" in lowered and "overlap" in lowered:
        return "subnet_cidr_overlap"
    if (
        resource_type == "module.subnet"
        and stage_status.get(SUBNET_VALIDATION_STAGE) == "done"
        and ("failed to create a new subnet" in lowered or "fptcloud_subnet" in lowered)
    ):
        return "provider_or_backend_system_error_after_valid_inputs"
    if resource_type == "fptcloud_storage_policy" and "404" in lowered:
        return "provider_endpoint_or_datasource_mismatch"
    if "unknownerror" in lowered or "system error" in lowered:
        return "provider_or_backend_system_error"
    if "region" in lowered and "not enabled" in lowered:
        return "object_storage_region_disabled"
    if ("storage" in lowered or "disk" in lowered) and ("quota" in lowered or "exceed" in lowered or "insufficient" in lowered):
        return "instance_storage_quota_exceeded"
    if "quota" in lowered or "exceed" in lowered or "insufficient" in lowered:
        return "instance_quota_exceeded"
    if "image" in lowered and ("not found" in lowered or "no match" in lowered or "invalid" in lowered or "unresolved" in lowered):
        return "instance_image_unresolved"
    if "flavor" in lowered and ("not found" in lowered or "no match" in lowered or "invalid" in lowered or "unresolved" in lowered):
        return "instance_flavor_unresolved"
    if "password policy" in lowered or "password_policy" in lowered or "exceeded password" in lowered:
        return "instance_password_policy_invalid"
    if "password" in lowered and ("missing" in lowered or "required" in lowered):
        return "instance_password_missing"
    if "fptcloud_instance" in lowered or "module.vm" in lowered or "module.this.fptcloud_instance" in lowered:
        return "instance_provider_error"
    if "subnet id is required" in lowered:
        return "blocked_missing_subnet_id"
    if "missing required" in lowered:
        return "configuration_missing"
    return "unknown"


def conflicting_subnet_name(text: str) -> str:
    match = re.search(r"\bin\s+([^,]+?)\s+subnet\b", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def existing_subnet_cidrs() -> list[str]:
    return [value.strip() for value in env("HC_EXISTING_SUBNET_CIDRS").split(",") if value.strip()]


def cidr_overlap(candidate: str, existing: list[str]) -> tuple[str, str]:
    try:
        candidate_network = ipaddress.ip_network(candidate, strict=True)
    except ValueError as exc:
        return "", f"candidate subnet CIDR is invalid: {exc}"
    for raw in existing:
        try:
            existing_network = ipaddress.ip_network(raw, strict=True)
        except ValueError as exc:
            return raw, f"HC_EXISTING_SUBNET_CIDRS contains invalid CIDR {raw}: {exc}"
        if candidate_network.overlaps(existing_network):
            return raw, ""
    return "", ""


def next_subnet_candidate(cidr: str) -> str:
    network = ipaddress.ip_network(cidr, strict=True)
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError("only IPv4 subnet candidate generation is supported")
    step = network.num_addresses * 10
    next_address = int(network.network_address) + step
    if next_address > int(ipaddress.IPv4Address("255.255.255.255")):
        raise ValueError("candidate subnet generation exceeded IPv4 address space")
    return str(ipaddress.ip_network(f"{ipaddress.IPv4Address(next_address)}/{network.prefixlen}", strict=True))


def gateway_for_candidate(start_cidr: str, start_gateway: str, selected_cidr: str) -> str:
    start_network = ipaddress.ip_network(start_cidr, strict=True)
    selected_network = ipaddress.ip_network(selected_cidr, strict=True)
    gateway_ip = ipaddress.ip_address(start_gateway)
    offset = int(gateway_ip) - int(start_network.network_address)
    if offset <= 0 or offset >= start_network.num_addresses - 1:
        return str(next(selected_network.hosts()))
    return str(ipaddress.ip_address(int(selected_network.network_address) + offset))


@dataclass(frozen=True)
class SubnetCandidateSelection:
    selected_cidr: str
    selected_gateway: str
    candidate_attempt_count: int
    rejected_cidrs: list[str]
    overlap_reason: str
    exhausted: bool = False
    error: str = ""
    conflict_source: str = ""
    conflicting_subnet: str = ""


def select_additional_subnet_candidate(
    start_cidr: str,
    start_gateway: str,
    existing: list[str],
    max_attempts: int,
    prior_rejected: list[str] | None = None,
    prior_sources: list[str] | None = None,
    prior_conflicting_subnets: list[str] | None = None,
) -> SubnetCandidateSelection:
    rejected: list[str] = list(prior_rejected or [])
    sources: list[str] = list(prior_sources or [])
    conflicting_subnets: list[str] = list(prior_conflicting_subnets or [])
    candidate = start_cidr
    overlap_reason = ""
    for attempt in range(1, max_attempts + 1):
        if candidate in rejected:
            candidate_source = sources[rejected.index(candidate)] if candidate in rejected and rejected.index(candidate) < len(sources) else "runtime_conflict"
            candidate_conflict = conflicting_subnets[rejected.index(candidate)] if candidate in rejected and rejected.index(candidate) < len(conflicting_subnets) else ""
            overlap_reason = f"{candidate} already rejected from {candidate_source}"
            if candidate_conflict:
                overlap_reason += f" by {candidate_conflict}"
            if attempt < max_attempts:
                try:
                    candidate = next_subnet_candidate(candidate)
                    continue
                except ValueError as exc:
                    return SubnetCandidateSelection("", "", attempt, rejected, overlap_reason, exhausted=True, error=str(exc))
            return SubnetCandidateSelection("", "", attempt, rejected, overlap_reason, exhausted=True)
        overlap, error = cidr_overlap(candidate, existing)
        if error:
            return SubnetCandidateSelection("", "", attempt, rejected, overlap_reason, error=error)
        if not overlap:
            gateway = gateway_for_candidate(start_cidr, start_gateway, candidate)
            return SubnetCandidateSelection(candidate, gateway, attempt, rejected, overlap_reason)
        rejected.append(candidate)
        sources.append("preflight_inventory")
        conflicting_subnets.append("")
        overlap_reason = f"{candidate} overlaps existing subnet CIDR {overlap}"
        if attempt < max_attempts:
            try:
                candidate = next_subnet_candidate(candidate)
            except ValueError as exc:
                return SubnetCandidateSelection("", "", attempt, rejected, overlap_reason, exhausted=True, error=str(exc))
    return SubnetCandidateSelection("", "", max_attempts, rejected, overlap_reason, exhausted=True)


def discover_existing_subnets(stage: StageSpec | None) -> None:
    if not stage:
        return
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["subnet-inventory"])
        return
    existing_subnet_inventory.clear()
    for cidr in existing_subnet_cidrs():
        existing_subnet_inventory.append(
            {
                "name": "operator-provided",
                "id": "",
                "cidr": cidr,
                "gateway": "",
                "vpc_id": run_context.get("effective_vpc_id", ""),
            }
        )
    if existing_subnet_inventory:
        emit(
            stage.id,
            "done",
            (
                "Loaded existing subnet inventory from HC_EXISTING_SUBNET_CIDRS; "
                f"cidrs={', '.join(item['cidr'] for item in existing_subnet_inventory)}; "
                "provider_listing=not_available_in_runner"
            ),
            ["HC_EXISTING_SUBNET_CIDRS"],
        )
    else:
        emit(
            stage.id,
            "done",
            "No existing subnet inventory configured; provider/API listing is not available in this runner, so overlap can only be classified from provider errors.",
            ["subnet-inventory"],
        )


def select_additional_subnet_vars(vars: dict[str, Any], state: CandidateState | None = None) -> tuple[dict[str, Any], str, CandidateState]:
    cidr = state.start_cidr if state else str(vars.get("cidr") or "")
    gateway = state.start_gateway if state else str(vars.get("gateway_ip") or "")
    max_attempts = state.max_attempts if state else max_subnet_candidate_attempts()
    existing = [item["cidr"] for item in existing_subnet_inventory if item.get("cidr")]
    selected = dict(vars)
    candidate_state = state or CandidateState(cidr, gateway, max_attempts)
    if not existing and not candidate_state.rejected_cidrs:
        emit(
            "network.select-additional-subnet-cidr",
            "done",
            f"total_attempts=1; rejected_cidrs=[]; selected_cidr={cidr or '<unset>'}; overlap_reason=<none>; inventory=unavailable",
            ["subnet-candidate-selection"],
        )
        return selected, "", candidate_state
    selection = select_additional_subnet_candidate(
        cidr,
        gateway,
        existing,
        max_attempts,
        list(candidate_state.rejected_cidrs),
        list(candidate_state.conflict_sources),
        list(candidate_state.conflicting_subnets),
    )
    rejected = ", ".join(selection.rejected_cidrs)
    if selection.error and not selection.exhausted:
        return selected, f"Classification: configuration_invalid; {selection.error}; attempted_cidr={cidr or '<unset>'}; attempted_gateway={gateway or '<unset>'}", candidate_state
    prior_source_count = len(candidate_state.conflict_sources)
    added_rejections = max(0, len(selection.rejected_cidrs) - prior_source_count)
    updated_state = CandidateState(
        cidr,
        gateway,
        max_attempts,
        tuple(selection.rejected_cidrs),
        tuple(candidate_state.conflict_sources + (("preflight_inventory",) * added_rejections)),
        tuple(candidate_state.conflicting_subnets + (("",) * added_rejections)),
    )
    conflict_sources = ", ".join(updated_state.conflict_sources)
    conflicting_subnets = ", ".join(value for value in updated_state.conflicting_subnets if value)
    if selection.exhausted:
        emit(
            "network.select-additional-subnet-cidr",
            "skipped",
            (
                f"total_attempts={selection.candidate_attempt_count}; rejected_cidrs=[{rejected}]; "
                f"conflict_sources=[{conflict_sources}]; "
                "selected_cidr=<none>; "
                f"overlap_reason={selection.overlap_reason or selection.error or '<none>'}"
            ),
            ["subnet-candidate-selection"],
        )
        return selected, (
            "Classification: subnet_cidr_exhausted; "
            f"candidate_attempt_count={selection.candidate_attempt_count}; "
            f"rejected_cidrs=[{rejected}]; "
            f"conflict_sources=[{conflict_sources}]; "
            f"overlap_reason={selection.overlap_reason or selection.error or '<none>'}; "
            "skipping before Terraform apply"
        ), updated_state
    selected["cidr"] = selection.selected_cidr
    selected["gateway_ip"] = selection.selected_gateway
    emit(
        "network.select-additional-subnet-cidr",
        "done",
        (
            f"total_attempts={selection.candidate_attempt_count}; rejected_cidrs=[{rejected}]; "
            f"conflict_sources=[{conflict_sources}]; "
            f"conflicting_subnets=[{conflicting_subnets}]; "
            f"selected_cidr={selection.selected_cidr}; selected_gateway={selection.selected_gateway}; "
            f"overlap_reason={selection.overlap_reason or '<none>'}"
        ),
        ["subnet-candidate-selection"],
    )
    return selected, "", updated_state


def append_provider_overlap(state: CandidateState, cidr: str, conflicting_subnet: str) -> CandidateState:
    if cidr in state.rejected_cidrs:
        return state
    return CandidateState(
        state.start_cidr,
        state.start_gateway,
        state.max_attempts,
        state.rejected_cidrs + (cidr,),
        state.conflict_sources + ("provider_error",),
        state.conflicting_subnets + (conflicting_subnet,),
    )


def vpc_identifier_type(value: str) -> str:
    if not value:
        return "unset"
    if looks_uuid(value):
        return "uuid-shaped"
    return "display-name-or-non-uuid"


def subnet_vars(suffix: str) -> dict[str, Any]:
    return {
        "name": env("HC_SUBNET_NAME", f"hc-net-{suffix}"),
        "cidr": env("HC_SUBNET_CIDR", "172.26.222.0/24"),
        "gateway_ip": env("HC_SUBNET_GATEWAY", "172.26.222.1"),
        "type": env("HC_SUBNET_TYPE", "NAT_ROUTED"),
        "vpc_id": update_vpc_context()["effective_vpc_id"],
    }


def validate_subnet_inputs(vars: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    name = str(vars.get("name") or "")
    cidr = str(vars.get("cidr") or "")
    gateway = str(vars.get("gateway_ip") or "")
    vpc_id = str(vars.get("vpc_id") or "")
    subnet_type = str(vars.get("type") or "")

    if not name:
        errors.append("subnet_name is empty")
    elif len(name) > 63:
        warnings.append("subnet_name is longer than 63 characters; provider/API name limits are not documented locally")
    if name and not all(ch.isalnum() or ch in "-_" for ch in name):
        warnings.append("subnet_name contains characters outside letters, digits, hyphen, and underscore")

    network = None
    try:
        network = ipaddress.ip_network(cidr, strict=True)
    except ValueError as exc:
        errors.append(f"subnet_cidr is invalid: {exc}")

    try:
        gateway_ip = ipaddress.ip_address(gateway)
    except ValueError as exc:
        gateway_ip = None
        errors.append(f"subnet_gateway is invalid: {exc}")

    if network and gateway_ip:
        if gateway_ip not in network:
            errors.append("subnet_gateway must belong to subnet_cidr")
        elif gateway_ip == network.network_address or gateway_ip == network.broadcast_address:
            errors.append("subnet_gateway must not be the network or broadcast address")

    if not vpc_id:
        errors.append("HC_VPC_ID/effective vpc_id is empty")
    elif vpc_identifier_type(vpc_id) != "uuid-shaped":
        warnings.append("VPC identifier is not UUID-shaped; provider docs call this field vpc_id")

    if subnet_type not in {"NAT_ROUTED", "ISOLATED"}:
        warnings.append("subnet type is not one of provider-described values NAT_ROUTED or ISOLATED")

    if not env("HC_VPC_CIDR"):
        warnings.append("Cannot validate subnet CIDR is inside VPC CIDR because HC_VPC_CIDR is not configured")
    else:
        try:
            vpc_network = ipaddress.ip_network(env("HC_VPC_CIDR"), strict=True)
            if network and not network.subnet_of(vpc_network):
                errors.append("subnet_cidr is not inside HC_VPC_CIDR")
        except ValueError as exc:
            errors.append(f"HC_VPC_CIDR is invalid: {exc}")

    warnings.append("Cannot validate overlap with existing FPT Cloud subnets without a working subnet listing API")
    warnings.append("Cannot locally prove VPC region/tenant match; provider uses FPTCLOUD_REGION and FPTCLOUD_TENANT_NAME")
    warnings.append("Provider schema for fptcloud_subnet requires vpc_id, name, cidr, gateway_ip, and type; VPC ID flavor is not clarified beyond 'vpc id'")
    return not errors, errors, warnings


def validate_subnet_stage(stage: StageSpec, vars: dict[str, Any]) -> None:
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["local.subnet-input-validation"])
        return
    valid, errors, warnings = validate_subnet_inputs(vars)
    details = [
        f"subnet_name={vars.get('name')}",
        f"subnet_cidr={vars.get('cidr')}",
        f"subnet_gateway={vars.get('gateway_ip')}",
        f"vpc_id={vars.get('vpc_id')}",
        f"vpc_identifier_type={vpc_identifier_type(str(vars.get('vpc_id') or ''))}",
        f"region={env('FPTCLOUD_REGION') or '<unset>'}",
        f"tenant={env('FPTCLOUD_TENANT_NAME') or '<unset>'}",
        f"terraform_vars={json.dumps(vars, sort_keys=True)}",
    ]
    if warnings:
        details.append(f"warnings={'; '.join(warnings)}")
    if valid:
        emit(stage.id, "done", "Local subnet input validation passed; " + "; ".join(details), ["local.subnet-input-validation"])
    else:
        emit(stage.id, "failed", "Local subnet input validation failed: " + "; ".join(errors + details), ["local.subnet-input-validation"])


def latest_create_subnet_error() -> dict[str, Any]:
    for run_dir in sorted((ROOT / "runs").glob("hc-*"), key=lambda path: path.stat().st_mtime, reverse=True):
        error_path = run_dir / "error_queue.json"
        if not error_path.exists():
            continue
        try:
            errors = json.loads(error_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for item in errors:
            if item.get("check") == SUBNET_CREATE_STAGE:
                reason = str(item.get("reason", ""))
                return {
                    "run": str(run_dir),
                    "workspace": item.get("workspace", ""),
                    "resources": item.get("resources", []),
                    "reason": reason,
                    "classification": (
                        "provider_or_backend_system_error_after_valid_inputs"
                        if "provider_or_backend_system_error_after_valid_inputs" in reason
                        else classify_error(reason, "module.subnet")
                    ),
                }
    return {}


def copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return
    for item in src.rglob("*"):
        if item.is_file():
            target = dst / item.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def zip_directory(source: Path, archive_path: Path) -> None:
    if archive_path.exists():
        archive_path.unlink()
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if path.is_file() and path != archive_path:
                archive.write(path, path.relative_to(source.parent))


def collect_subnet_create_evidence(stage: StageSpec, vars: dict[str, Any], diagnostics_data: dict[str, Any]) -> None:
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["provider-support-evidence"])
        return

    valid, errors, warnings = validate_subnet_inputs(vars)
    if not valid:
        emit(stage.id, "failed", "Evidence collection blocked by invalid local inputs: " + "; ".join(errors), ["provider-support-evidence"])
        return

    check = Check(name=stage.id, module="subnet", vars=vars, required_vars=("vpc_id",))
    workspace = write_workspace(check)
    evidence_dir = RUN_ROOT / "evidence" / safe_name(stage.id)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    init = run(["terraform", "init", "-input=false", "-no-color"], workspace, timeout=300, stage=f"{stage.id}-init")
    plan = None
    show = None
    if init.returncode == 0:
        plan = run(
            ["terraform", "plan", "-out=tfplan", "-detailed-exitcode", "-no-color", "-input=false"],
            workspace,
            timeout=300,
            stage=f"{stage.id}-plan",
        )
        if plan.returncode in (0, 2):
            show = run(["terraform", "show", "-json", "-no-color", "tfplan"], workspace, timeout=120, stage=f"{stage.id}-show-plan")

    latest_error = latest_create_subnet_error()
    summary = {
        "generated_at": now(),
        "stage": stage.id,
        "created_cloud_resources": False,
        "apply_was_run": False,
        "effective_config": effective_config(),
        "provider": diagnostics_data.get("provider", {}),
        "provider_config": diagnostics_data.get("provider_config", {}),
        "terraform_provider_lock_file": diagnostics_data.get("provider", {}).get("lock_file", ""),
        "terraform_module_path": str(MODULES / "subnet"),
        "sanitized_terraform_variables": vars,
        "validation": {"passed": True, "errors": errors, "warnings": warnings},
        "terraform": {
            "init_returncode": init.returncode,
            "plan_returncode": plan.returncode if plan else None,
            "show_plan_returncode": show.returncode if show else None,
            "plan_succeeded": bool(plan and plan.returncode in (0, 2)),
        },
        "latest_create_subnet_error": latest_error,
        "exact_error_classification": latest_error.get("classification", "no_create_subnet_error_found"),
    }
    (evidence_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (evidence_dir / "stage_events.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
    (evidence_dir / "input_diagnostics.json").write_text(json.dumps(diagnostics_data, indent=2), encoding="utf-8")
    lock_file = Path(str(summary["terraform_provider_lock_file"]))
    if lock_file.exists():
        copy_tree(lock_file, evidence_dir / "terraform.lock.hcl")
    copy_tree(MODULES / "subnet", evidence_dir / "module_subnet")
    copy_tree(workspace / "logs", evidence_dir / "terraform_logs")
    for tf_log in workspace.glob("*.tf.log"):
        copy_tree(tf_log, evidence_dir / "tf_log" / tf_log.name)
    if latest_error.get("workspace"):
        latest_workspace = Path(str(latest_error["workspace"]))
        copy_tree(latest_workspace / "logs", evidence_dir / "latest_failed_apply_logs")
        for tf_log in latest_workspace.glob("*.tf.log"):
            copy_tree(tf_log, evidence_dir / "latest_failed_apply_tf_log" / tf_log.name)
    archive_path = RUN_ROOT / f"{safe_name(stage.id)}-evidence.zip"
    zip_directory(evidence_dir, archive_path)
    emit(
        stage.id,
        "done",
        f"Evidence bundle created at {archive_path}; plan_only=true; classification={summary['exact_error_classification']}",
        [str(archive_path)],
    )


def classify_context(
    *,
    stage: str,
    resource_type: str,
    address: str,
    module_path: Path,
    reason: str,
    vars: dict[str, Any] | None = None,
) -> FailureContext:
    context = cloud_context()
    vars = vars or {}
    return FailureContext(
        stage=stage,
        resource_type=resource_type,
        address=address,
        module_path=str(module_path),
        tenant=context["tenant"],
        region=context["region"],
        vpc_id=context["vpc_id"],
        reason=reason,
        classification=classify_error(reason, resource_type),
        attempted_cidr=str(vars.get("cidr") or ""),
        attempted_gateway=str(vars.get("gateway_ip") or ""),
        conflicting_subnet=conflicting_subnet_name(reason),
    )


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


def write_queues() -> None:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    (RUN_ROOT / "pending_queue.json").write_text(
        json.dumps([asdict(item) for item in pending_queue], indent=2),
        encoding="utf-8",
    )
    (RUN_ROOT / "error_queue.json").write_text(
        json.dumps([asdict(item) for item in error_queue], indent=2),
        encoding="utf-8",
    )


def write_log() -> None:
    # Governed by specs/health-check.json LOG_FORMAT_POLICY.
    # Primary: JSON log written to runs/<run_id>/log.json and root log.json alias.
    write_queues()
    serialised = json.dumps(events, indent=2, ensure_ascii=False)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    (RUN_ROOT / "log.json").write_text(serialised, encoding="utf-8")
    JSON_LOG_PATH.write_text(serialised, encoding="utf-8")

    # Derived HTML view — rendered from events for human reading in browser.
    rows = "\n".join(
        "<tr>"
        f"<td>{escape(event['timestamp'])}</td>"
        f"<td>{escape(event['stage'])}</td>"
        f"<td><span class=\"badge {status_class(event['status'])}\">{escape(event['status'])}</span></td>"
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
    html = Template(TEMPLATE.read_text(encoding="utf-8")).safe_substitute(
        rows=rows,
        summary=summary,
        timing=timing_summary(),
    )
    LOG_PATH.write_text(html, encoding="utf-8")


def queue_pending(check: str, workspace: Path, resources: list[str], reason: str) -> None:
    pending_queue.append(QueueItem(check, str(workspace), resources, reason, now()))
    emit(check, "queued", f"Queued for pending readiness: {reason}", resources)


def queue_error(check: str, workspace: Path, resources: list[str], reason: str) -> None:
    error_queue.append(QueueItem(check, str(workspace), resources, reason, now()))
    emit(check, "failed", reason, resources)


class ResourceLock:
    def __init__(self, name: str) -> None:
        self.name = name
        self.path = LOCK_ROOT / f"{safe_name(name)}.lock"
        self.acquired = False

    def __enter__(self) -> ResourceLock:
        LOCK_ROOT.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(f"resource group is already locked: {self.name}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{RUN_ID}\n{now()}\n")
        self.acquired = True
        emit(f"{self.name}:lock", "locked", "Resource-group lock acquired", [str(self.path)])
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            emit(f"{self.name}:lock", "unlocked", "Resource-group lock released", [str(self.path)])


def run(
    cmd: list[str],
    cwd: Path,
    timeout: int = 900,
    stage: str = "terraform",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ)
    if extra_env:
        run_env.update(extra_env)
    if env("HC_TF_LOG"):
        run_env["TF_LOG"] = env("HC_TF_LOG")
    if env("HC_TF_LOG_PATH"):
        run_env["TF_LOG_PATH"] = env("HC_TF_LOG_PATH")
    elif env("HC_TF_LOG"):
        run_env["TF_LOG_PATH"] = str(cwd / f"{safe_name(stage)}.tf.log")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    log_dir = cwd / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(stage)
    (log_dir / f"{base}.stdout.log").write_text(redact_text(result.stdout or ""), encoding="utf-8")
    (log_dir / f"{base}.stderr.log").write_text(redact_text(result.stderr or ""), encoding="utf-8")
    return result


def run_instance_terraform(cmd: list[str], cwd: Path, *, timeout: int = 900, stage: str = "terraform") -> subprocess.CompletedProcess[str]:
    terraform_env = {"TF_VAR_password": GENERATED_INSTANCE_PASSWORD} if GENERATED_INSTANCE_PASSWORD else None
    return run(cmd, cwd, timeout=timeout, stage=stage, extra_env=terraform_env)


def write_workspace(check: Check) -> Path:
    workspace = RUN_ROOT / check.name
    workspace.mkdir(parents=True, exist_ok=True)
    module_source = (MODULES / check.module).resolve().as_posix()
    var_blocks = "\n".join(f'variable "{key}" {{}}' for key in sorted(check.vars))
    args = "\n".join(f"  {key} = var.{key}" for key in sorted(check.vars))
    (workspace / "main.tf").write_text(
        f"""
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    fptcloud = {{
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }}
  }}
}}

provider "fptcloud" {{}}

{var_blocks}

module "this" {{
  source = "{module_source}"
{args}
}}
""".lstrip(),
        encoding="utf-8",
    )
    (workspace / "terraform.tfvars.json").write_text(
        json.dumps(check.vars, indent=2),
        encoding="utf-8",
    )
    return workspace


def write_workspace_at(check: Check, workspace: Path) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    module_source = (MODULES / check.module).resolve().as_posix()
    var_blocks = "\n".join(
        'variable "password" {\n  default = null\n  sensitive = true\n}'
        if key == "password"
        else 'variable "tags" {\n  type    = map(string)\n  default = {}\n}'
        if key == "tags"
        else f'variable "{key}" {{}}'
        for key in sorted(check.vars)
    )
    args = "\n".join(f"  {key} = var.{key}" for key in sorted(check.vars))
    (workspace / "main.tf").write_text(
        f"""
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    fptcloud = {{
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }}
  }}
}}

provider "fptcloud" {{}}

{var_blocks}

module "this" {{
  source = "{module_source}"
{args}
}}
""".lstrip(),
        encoding="utf-8",
    )
    (workspace / "terraform.tfvars.json").write_text(
        json.dumps({key: value for key, value in check.vars.items() if key != "password"}, indent=2),
        encoding="utf-8",
    )
    return workspace


def planned_resources(workspace: Path) -> list[str]:
    result = run(["terraform", "show", "-json", "-no-color", "tfplan"], workspace, stage="show-plan")
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout or "{}")
    resources = []
    for change in data.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if "create" in actions or "update" in actions:
            resources.append(change.get("address", "unknown"))
    return resources


def state_resources(workspace: Path) -> list[str]:
    result = run(["terraform", "state", "list"], workspace, timeout=120, stage="state-list")
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def state_json(workspace: Path) -> dict[str, Any]:
    result = run(["terraform", "show", "-json", "-no-color"], workspace, timeout=120, stage="show-state")
    if result.returncode != 0:
        return {}
    return json.loads(result.stdout or "{}")


def module_resources(data: dict[str, Any]) -> list[dict[str, Any]]:
    root = data.get("values", {}).get("root_module", {})
    found: list[dict[str, Any]] = []

    def walk(module: dict[str, Any]) -> None:
        found.extend(module.get("resources", []))
        for child in module.get("child_modules", []):
            walk(child)

    walk(root)
    return found


def resource_statuses(workspace: Path) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for resource in module_resources(state_json(workspace)):
        address = str(resource.get("address", "unknown"))
        values = resource.get("values", {})
        raw = values.get("status")
        if raw is None:
            raw = values.get("state")
        if raw is None:
            statuses[address] = "NO_STATUS"
        elif isinstance(raw, bool):
            statuses[address] = "READY" if raw else "PENDING"
        else:
            statuses[address] = str(raw).upper()
    return statuses


def readiness(workspace: Path) -> tuple[bool, str, list[str]]:
    resources = state_resources(workspace)
    if not resources:
        return True, "no managed resources in state", []
    statuses = resource_statuses(workspace)
    unknown_or_pending = [
        f"{resource}={status}"
        for resource, status in statuses.items()
        if status in PENDING_STATUSES or status not in READY_STATUSES | {"NO_STATUS"}
    ]
    if unknown_or_pending:
        return False, "; ".join(unknown_or_pending), resources
    return True, "resources are ready", resources


def preflight(check: Check) -> tuple[bool, str]:
    missing_env = [name for name in check.required_env if not env(name)]
    if missing_env:
        return False, f"Missing required environment values: {', '.join(missing_env)}"
    blocked = [name for name in check.blocked_by if not stage_ok(name)]
    if blocked:
        return False, f"Blocked by incomplete dependency stage(s): {', '.join(blocked)}"
    if check.module == "object_storage" and not check.vars.get("region_name"):
        return False, "No enabled object-storage region configured; set HC_ENABLED_OBJECT_REGIONS"
    missing_vars = [
        name
        for name in check.required_vars
        if check.vars.get(name) in (None, "", [], {})
    ]
    if missing_vars:
        return False, f"Missing required preflight values: {', '.join(missing_vars)}"
    return True, "preflight passed"


def input_configured(requirement: str) -> bool:
    if requirement == "generated_instance_password":
        return bool(GENERATED_INSTANCE_PASSWORD)
    if requirement == "effective_vpc_id":
        return bool(run_context.get("effective_vpc_id"))
    if requirement == "effective_subnet_id":
        return bool(run_context.get("effective_subnet_id"))
    if requirement == "effective_storage_policy_id":
        return bool(run_context.get("effective_storage_policy_id"))
    if requirement == "discovered_instance_images":
        images = run_context.get("discovered_instance_images") or {}
        if env_bool_default("HC_REQUIRE_ALL_INSTANCE_IMAGES", False):
            return all(images.get(label) for label, _var_name in INSTANCE_IMAGE_MATRIX)
        return bool(images)
    if requirement == "discovered_instance_flavor":
        return bool(run_context.get("discovered_instance_flavor_name") or run_context.get("discovered_instance_flavor"))
    if requirement == "validated_instance_hostnames":
        return bool(run_context.get("validated_instance_hostnames"))
    if requirement == "selected_round_images":
        return bool(selected_round_labels())
    if " or " in requirement:
        return any(input_configured(part.strip()) for part in requirement.split(" or "))
    return bool(env(requirement))


def password_policy_result(password: str) -> dict[str, bool]:
    special_set = set(PASSWORD_SPECIALS)
    allowed_chars = set(ascii_lowercase + ascii_lowercase.upper() + digits + PASSWORD_SPECIALS)
    return {
        "password_generated": bool(password),
        "password_redacted": True,
        "length_ok": len(password) >= PASSWORD_MIN_LENGTH,
        "no_spaces": " " not in password,
        "uppercase_present": any(char.isupper() for char in password),
        "lowercase_present": any(char.islower() for char in password),
        "number_present": any(char.isdigit() for char in password),
        "allowed_special_present": any(char in special_set for char in password),
        "no_disallowed_specials": all(char in allowed_chars for char in password),
    }


def password_policy_valid(password: str) -> bool:
    result = password_policy_result(password)
    return all(result.values())


def validate_instance_password_policy_stage(stage: StageSpec | None) -> None:
    if not stage:
        return
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["generated_instance_password"])
        return
    result = password_policy_result(GENERATED_INSTANCE_PASSWORD)
    message = "; ".join(f"{key}={value}" for key, value in result.items())
    if all(result.values()):
        emit(stage.id, "done", message, ["generated_instance_password"])
        return
    emit(
        stage.id,
        "skipped",
        f"Classification: instance_password_policy_invalid; {message}; Terraform apply not called",
        ["generated_instance_password"],
    )


def spec_preflight(stage: StageSpec) -> tuple[bool, str]:
    ok, reason = runnable_spec(stage)
    if not ok:
        return False, reason
    missing = [name for name in stage.required_inputs if not input_configured(name)]
    if missing:
        return False, f"Missing required spec inputs: {', '.join(missing)}"
    blocked = [name for name in stage.dependency_stages if not stage_ok(name)]
    if blocked:
        return False, f"Blocked by incomplete dependency stage(s): {', '.join(blocked)}"
    return True, "spec preflight passed"


def discover_vpc(stage: StageSpec | None) -> str:
    update_vpc_context()
    if not stage:
        return run_context["effective_vpc_id"]
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["data.fptcloud_vpc.this"])
        return run_context["effective_vpc_id"]
    missing = [name for name in stage.required_inputs if not env(name)]
    if missing:
        emit(stage.id, "skipped", f"Missing required spec inputs: {', '.join(missing)}; {vpc_diagnostics_message()}", ["data.fptcloud_vpc.this"])
        return run_context["effective_vpc_id"]

    explicit = run_context["explicit_vpc_id"]
    lookup = run_context["vpc_name"]
    if not lookup:
        if explicit:
            emit(stage.id, "done", f"Using explicit HC_VPC_ID; no VPC lookup key configured; {vpc_diagnostics_message()}", ["HC_VPC_ID"])
        else:
            emit(stage.id, "skipped", f"No VPC ID can be resolved because no VPC lookup key is configured; {vpc_diagnostics_message()}", ["data.fptcloud_vpc.this"])
        return run_context["effective_vpc_id"]

    workspace = RUN_ROOT / safe_name(stage.id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "main.tf").write_text(
        f"""
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    fptcloud = {{
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }}
  }}
}}

provider "fptcloud" {{}}

data "fptcloud_vpc" "this" {{
  name = "{lookup}"
}}

output "value" {{
  value = data.fptcloud_vpc.this.id
}}
""".lstrip(),
        encoding="utf-8",
    )
    init = run(["terraform", "init", "-input=false", "-no-color"], workspace, timeout=300, stage=f"{stage.id}-init")
    if init.returncode != 0:
        reason = (init.stderr or init.stdout)[-1200:]
        context = classify_context(stage=stage.id, resource_type="fptcloud_vpc", address="data.fptcloud_vpc.this", module_path=workspace, reason=reason)
        queue_error(stage.id, workspace, ["data.fptcloud_vpc.this"], format_failure(context))
        if explicit:
            stage_status[stage.id] = "done"
        return run_context["effective_vpc_id"]
    apply = run(["terraform", "apply", "-auto-approve", "-no-color", "-input=false"], workspace, timeout=300, stage=f"{stage.id}-apply")
    if apply.returncode != 0:
        reason = (apply.stderr or apply.stdout)[-1200:]
        context = classify_context(stage=stage.id, resource_type="fptcloud_vpc", address="data.fptcloud_vpc.this", module_path=workspace, reason=reason)
        queue_error(stage.id, workspace, ["data.fptcloud_vpc.this"], format_failure(context))
        if explicit:
            stage_status[stage.id] = "done"
        return run_context["effective_vpc_id"]
    output = run(["terraform", "output", "-raw", "value"], workspace, timeout=120, stage=f"{stage.id}-output")
    discovered = output.stdout.strip() if output.returncode == 0 else ""
    update_vpc_context(discovered_vpc_id=discovered)
    if explicit and discovered and explicit != discovered:
        emit(stage.id, "done", f"WARNING: explicit HC_VPC_ID differs from discovered VPC ID; using explicit value. {vpc_diagnostics_message()}", ["HC_VPC_ID", "data.fptcloud_vpc.this"])
    elif run_context["effective_vpc_id"]:
        emit(stage.id, "done", f"VPC ID resolution succeeded. {vpc_diagnostics_message()}", ["data.fptcloud_vpc.this"])
    else:
        emit(stage.id, "skipped", f"VPC ID resolution did not return an ID. {vpc_diagnostics_message()}", ["data.fptcloud_vpc.this"])
    return run_context["effective_vpc_id"]


def discover_value(name: str, source: str, expression: str, vpc_id: str, stage_id: str | None = None) -> str:
    stage_name = stage_id or f"discover-{name}"
    workspace = RUN_ROOT / safe_name(stage_name)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "main.tf").write_text(
        f"""
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    fptcloud = {{
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }}
  }}
}}

provider "fptcloud" {{}}

data "{source}" "this" {{
  vpc_id = "{vpc_id}"
}}

output "value" {{
  value = {expression}
}}
""".lstrip(),
        encoding="utf-8",
    )
    init = run(["terraform", "init", "-input=false", "-no-color"], workspace, timeout=300, stage=f"{stage_name}-init")
    if init.returncode != 0:
        reason = (init.stderr or init.stdout)[-1000:]
        context = classify_context(
            stage=stage_name,
            resource_type=source,
            address=f"data.{source}.this",
            module_path=workspace,
            reason=reason,
        )
        queue_error(stage_name, workspace, [source], format_failure(context))
        return ""
    apply = run(
        ["terraform", "apply", "-auto-approve", "-no-color", "-input=false"],
        workspace,
        timeout=300,
        stage=f"{stage_name}-apply",
    )
    if apply.returncode != 0:
        reason = (apply.stderr or apply.stdout)[-1000:]
        context = classify_context(
            stage=stage_name,
            resource_type=source,
            address=f"data.{source}.this",
            module_path=workspace,
            reason=reason,
        )
        queue_error(stage_name, workspace, [source], format_failure(context))
        return ""
    output = run(["terraform", "output", "-raw", "value"], workspace, timeout=120, stage=f"{stage_name}-output")
    value = output.stdout.strip() if output.returncode == 0 else ""
    status = "done" if value else "skipped"
    emit(stage_name, status, f"Discovered {name}: {value or 'not found'}", [source])
    return value


def discover_filtered_value(
    *,
    name: str,
    source: str,
    collection: str,
    output_attr: str,
    filter_key: str,
    filter_value: str,
    vpc_id: str,
    stage_id: str,
) -> str:
    workspace = RUN_ROOT / safe_name(stage_id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "main.tf").write_text(
        f"""
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    fptcloud = {{
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }}
  }}
}}

provider "fptcloud" {{}}

data "{source}" "this" {{
  vpc_id = "{vpc_id}"
  filter {{
    key = "{filter_key}"
    values = ["{filter_value}"]
  }}
}}

output "value" {{
  value = try(data.{source}.this.{collection}[0].{output_attr}, "")
}}
""".lstrip(),
        encoding="utf-8",
    )
    init = run(["terraform", "init", "-input=false", "-no-color"], workspace, timeout=300, stage=f"{stage_id}-init")
    if init.returncode != 0:
        reason = (init.stderr or init.stdout)[-1000:]
        context = classify_context(stage=stage_id, resource_type=source, address=f"data.{source}.this", module_path=workspace, reason=reason)
        queue_error(stage_id, workspace, [source], format_failure(context))
        return ""
    apply = run(["terraform", "apply", "-auto-approve", "-no-color", "-input=false"], workspace, timeout=300, stage=f"{stage_id}-apply")
    if apply.returncode != 0:
        reason = (apply.stderr or apply.stdout)[-1000:]
        context = classify_context(stage=stage_id, resource_type=source, address=f"data.{source}.this", module_path=workspace, reason=reason)
        queue_error(stage_id, workspace, [source], format_failure(context))
        return ""
    output = run(["terraform", "output", "-raw", "value"], workspace, timeout=120, stage=f"{stage_id}-output")
    value = output.stdout.strip() if output.returncode == 0 else ""
    emit(stage_id, "done" if value else "failed", f"Resolved {name} by {filter_key}: {value or 'not found'}", [source])
    return value


def discover_data_collection(stage_id: str, source: str, collection: str, vpc_id: str) -> tuple[list[dict[str, Any]], str]:
    workspace = RUN_ROOT / safe_name(stage_id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "main.tf").write_text(
        f"""
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    fptcloud = {{
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }}
  }}
}}

provider "fptcloud" {{}}

data "{source}" "this" {{
  vpc_id = "{vpc_id}"
}}

output "value" {{
  value = jsonencode(data.{source}.this.{collection})
}}
""".lstrip(),
        encoding="utf-8",
    )
    init = run(["terraform", "init", "-input=false", "-no-color"], workspace, timeout=300, stage=f"{stage_id}-init")
    if init.returncode != 0:
        return [], (init.stderr or init.stdout)[-1000:]
    plan = run(["terraform", "plan", "-out=tfplan", "-no-color", "-input=false"], workspace, timeout=300, stage=f"{stage_id}-plan")
    if plan.returncode != 0:
        return [], (plan.stderr or plan.stdout)[-1000:]
    output = run(["terraform", "show", "-json", "-no-color", "tfplan"], workspace, timeout=120, stage=f"{stage_id}-show-plan")
    if output.returncode != 0:
        return [], (output.stderr or output.stdout)[-1000:]
    try:
        plan_json = json.loads(output.stdout or "{}")
        raw_value = plan_json.get("planned_values", {}).get("outputs", {}).get("value", {}).get("value", "[]")
        values = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except json.JSONDecodeError as exc:
        return [], f"Could not parse {source} output: {exc}"
    return [item for item in values if isinstance(item, dict)], ""


def normalized_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def image_matches(label: str, image: dict[str, Any]) -> bool:
    haystack = normalized_text(f"{image.get('name', '')} {image.get('catalog', '')}")
    if image.get("is_gpu") is True:
        return False
    requirements = {
        "windows-2012": ("windows", "2012"),
        "windows-2016": ("windows", "2016"),
        "windows-2019": ("windows", "2019"),
        "windows-2022": ("windows", "2022"),
        "ubuntu-16-04": ("ubuntu", "1604"),
        "ubuntu-18-04": ("ubuntu", "1804"),
        "ubuntu-20-04": ("ubuntu", "2004"),
        "ubuntu-22-04": ("ubuntu", "2204"),
    }
    tokens = requirements[label]
    if tokens[0] not in haystack:
        return False
    version = tokens[1]
    return version in haystack or version[:2] + "04" in haystack


def image_patterns(label: str) -> list[str]:
    patterns = {
        "windows-2012": ["windows.*2012", "windows server 2012"],
        "windows-2016": ["windows.*2016", "windows server 2016"],
        "windows-2019": ["windows.*2019", "windows server 2019"],
        "windows-2022": ["windows.*2022", "windows server 2022"],
        "ubuntu-16-04": ["Ubuntu-16-04", "ubuntu-16.04", "Ubuntu 16.04", "Ubuntu Server 16.04", "ubuntu.*1604"],
        "ubuntu-18-04": ["Ubuntu-18-04", "ubuntu-18.04", "Ubuntu 18.04", "Ubuntu Server 18.04", "ubuntu.*1804"],
        "ubuntu-20-04": ["Ubuntu-20-04", "ubuntu-20.04", "Ubuntu 20.04", "Ubuntu Server 20.04", "ubuntu.*2004"],
        "ubuntu-22-04": ["Ubuntu-22-04", "ubuntu-22.04", "Ubuntu 22.04", "Ubuntu Server 22.04", "ubuntu.*2204"],
    }
    return patterns[label]


def ubuntu_candidate_names(images: list[dict[str, Any]]) -> list[str]:
    names = []
    for image in images:
        text = f"{image.get('name', '')} {image.get('catalog', '')}"
        if "ubuntu" in text.lower() and image.get("name"):
            names.append(str(image.get("name")))
    return sorted(set(names))


def select_image_candidate(label: str, images: list[dict[str, Any]]) -> tuple[str, list[str]]:
    candidates = [image for image in images if image_matches(label, image)]
    names = sorted(str(image.get("name") or "") for image in candidates if image.get("name"))
    return (names[-1] if names else "", names)


def discover_instance_images(stage: StageSpec | None) -> None:
    if not stage:
        return
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", f"provider_capability=supported; {reason}", ["data.fptcloud_image.this"])
        return
    vpc_id = str(run_context.get("effective_vpc_id") or "")
    resolved: dict[str, str] = {}
    sources: dict[str, str] = {}
    details: list[str] = ["provider_capability=supported"]
    unresolved: list[str] = []
    unavailable: list[str] = []
    for label, var_name in INSTANCE_IMAGE_MATRIX:
        if env(var_name):
            resolved[label] = env(var_name)
            sources[label] = "explicit_env"
    provider_images: list[dict[str, Any]] = []
    if len(resolved) < len(INSTANCE_IMAGE_MATRIX):
        provider_images, error = discover_data_collection(stage.id, "fptcloud_image", "images", vpc_id)
        if error:
            context = classify_context(stage=stage.id, resource_type="fptcloud_image", address="data.fptcloud_image.this", module_path=RUN_ROOT / safe_name(stage.id), reason=error)
            queue_error(stage.id, RUN_ROOT / safe_name(stage.id), ["data.fptcloud_image.this"], format_failure(context))
            return
    ubuntu_candidates = ubuntu_candidate_names(provider_images)
    for label, _var_name in INSTANCE_IMAGE_MATRIX:
        candidates: list[str] = []
        if label not in resolved:
            selected, candidates = select_image_candidate(label, provider_images)
            if selected:
                resolved[label] = selected
                sources[label] = "provider_datasource"
            else:
                sources[label] = "unresolved"
                unresolved.append(label)
                if label in {"ubuntu-16-04", "ubuntu-18-04"}:
                    unavailable.append(label)
        status = "resolved" if resolved.get(label) else ("image_unavailable_in_region" if label in unavailable else "unresolved")
        details.append(
            f"{label}:status={status}; source={sources[label]}; image={resolved.get(label) or '<unresolved>'}; "
            f"candidate_count={len(candidates)}; patterns_tried={','.join(image_patterns(label))}"
        )
    run_context["discovered_instance_images"] = resolved
    run_context["instance_image_sources"] = sources
    run_context["unavailable_instance_images"] = unavailable
    resolved_count = len([label for label, _var_name in INSTANCE_IMAGE_MATRIX if resolved.get(label)])
    unresolved_count = len(INSTANCE_IMAGE_MATRIX) - resolved_count
    require_all = env_bool_default("HC_REQUIRE_ALL_INSTANCE_IMAGES", False)
    summary = (
        f"resolution_status={'resolved' if unresolved_count == 0 else 'partial'}; "
        f"resolved_count={resolved_count}; unresolved_count={unresolved_count}; require_all_images={require_all}; "
        f"ubuntu_candidate_images={json.dumps(ubuntu_candidates)}"
    )
    if unresolved:
        status = "skipped" if require_all else "done"
        message = (
            f"Classification: instance_image_unresolved; {summary}; {' | '.join(details)}; "
            f"unresolved={', '.join(unresolved)}; unavailable={', '.join(unavailable) or '<none>'}"
        )
        emit(stage.id, status, message, ["data.fptcloud_image.this"])
        if not require_all:
            stage_status[stage.id] = "done"
        return
    stage_status[stage.id] = "done"
    emit(stage.id, "done", f"{summary}; {' | '.join(details)}", ["data.fptcloud_image.this"])


def flavor_matches(flavor: dict[str, Any]) -> bool:
    cpu = flavor.get("cpu")
    memory = flavor.get("memory_mb")
    flavor_type = str(flavor.get("type") or "").upper()
    gpu = flavor.get("gpu_memory_gb")
    return cpu == 2 and memory == 2048 and (not flavor_type or flavor_type == "VM_SIZE") and gpu in (None, 0)


def select_flavor_candidate(flavors: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    candidates = [flavor for flavor in flavors if flavor_matches(flavor)]
    names = sorted(str(flavor.get("name") or "") for flavor in candidates if flavor.get("name"))
    if not names:
        return None, []
    selected_name = names[0]
    for flavor in candidates:
        if flavor.get("name") == selected_name:
            return flavor, names
    return None, names


def discover_instance_flavor(stage: StageSpec | None) -> None:
    if not stage:
        return
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", f"provider_capability=supported; {reason}", ["data.fptcloud_flavor.this"])
        return
    vpc_id = str(run_context.get("effective_vpc_id") or "")
    source = "unresolved"
    selected_name = ""
    selected_id = ""
    candidate_count = 0
    if env("HC_FLAVOR_ID"):
        selected_name = discover_filtered_value(name="flavor", source="fptcloud_flavor", collection="flavors", output_attr="name", filter_key="id", filter_value=env("HC_FLAVOR_ID"), vpc_id=vpc_id, stage_id="compute.resolve-instance-flavor")
        selected_id = env("HC_FLAVOR_ID")
        source = "explicit_env" if selected_name else "unresolved"
    elif env("HC_FLAVOR_NAME"):
        selected_name = env("HC_FLAVOR_NAME")
        source = "explicit_env"
    else:
        flavors, error = discover_data_collection(stage.id, "fptcloud_flavor", "flavors", vpc_id)
        if error:
            context = classify_context(stage=stage.id, resource_type="fptcloud_flavor", address="data.fptcloud_flavor.this", module_path=RUN_ROOT / safe_name(stage.id), reason=error)
            queue_error(stage.id, RUN_ROOT / safe_name(stage.id), ["data.fptcloud_flavor.this"], format_failure(context))
            return
        selected, candidates = select_flavor_candidate(flavors)
        candidate_count = len(candidates)
        if selected:
            selected_name = str(selected.get("name") or "")
            selected_id = str(selected.get("id") or "")
            source = "provider_datasource"
    run_context["discovered_instance_flavor"] = selected_id or selected_name
    run_context["discovered_instance_flavor_name"] = selected_name
    run_context["instance_flavor_source"] = source
    message = (
        f"provider_capability=supported; flavor_status={'resolved' if selected_name else 'unresolved'}; "
        f"source={source}; flavor_name={selected_name or '<unresolved>'}; flavor_id={selected_id or '<unresolved>'}; "
        f"target_cpu=2; target_memory_mb=2048; candidate_count={candidate_count}"
    )
    if not selected_name:
        emit(stage.id, "skipped", f"Classification: instance_flavor_unresolved; {message}", ["data.fptcloud_flavor.this"])
        return
    stage_status[stage.id] = "done"
    emit(stage.id, "done", message, ["data.fptcloud_flavor.this"])


def collected_storage_policies() -> list[dict[str, str]]:
    policies = spec_constants().get("COLLECTED_INSTANCE_STORAGE_POLICIES", [])
    return [dict(policy) for policy in policies if isinstance(policy, dict)]


def preferred_instance_storage_policy_name() -> str:
    selection = spec_constants().get("INSTANCE_STORAGE_POLICY_SELECTION", {})
    configured = str(selection.get("preferred_name") or "Premium-SSD") if isinstance(selection, dict) else "Premium-SSD"
    return env("HC_INSTANCE_STORAGE_POLICY_NAME") or configured


def storage_policy_name_matches(policy: dict[str, Any], requested_name: str) -> bool:
    return str(policy.get("name") or "") == requested_name


def discovered_storage_policies() -> list[dict[str, str]]:
    policies = run_context.get("discovered_storage_policies") or []
    return [dict(policy) for policy in policies if isinstance(policy, dict)]


def available_storage_policies() -> list[dict[str, str]]:
    collected = collected_storage_policies()
    provider_policies = discovered_storage_policies()
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, str]] = []
    for policy in [*provider_policies, *collected]:
        key = (
            str(policy.get("name") or "").lower(),
            str(policy.get("id") or "").lower(),
            str(policy.get("id_db") or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(policy)
    return merged


def storage_policy_by(field: str, value: str) -> dict[str, str]:
    if not value:
        return {}
    for policy in available_storage_policies():
        if str(policy.get(field) or "").lower() == value.lower():
            return policy
    return {}


def storage_policy_by_id(value: str) -> dict[str, str]:
    return storage_policy_by("id", value) or storage_policy_by("id_db", value)


def provider_uses_storage_policy_db_id() -> bool:
    return False


def normalized_storage_policy(policy: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(policy.get("name") or ""),
        "id": str(policy.get("id") or ""),
        "id_db": str(policy.get("id_db") or policy.get("idDB") or policy.get("db_id") or ""),
    }


def storage_policy_candidate_names(policies: list[dict[str, str]]) -> list[str]:
    return [str(policy.get("name") or "") for policy in policies if policy.get("name")]


def discover_storage_policy_stage(stage: StageSpec | None, explicit_storage_policy_id: str, vpc_id: str) -> str:
    if not stage:
        return explicit_storage_policy_id
    requested_name = preferred_instance_storage_policy_name()
    if explicit_storage_policy_id:
        ok, reason = runnable_spec(stage)
        emit(
            stage.id,
            "done" if ok else "skipped",
            (
                f"Using explicit HC_STORAGE_POLICY_ID for non-instance storage discovery; "
                f"storage_policy_requested={requested_name}; "
                f"instance_storage_policy_selection=preferred_exact_name; "
                f"provider_field_used=storage_policy_id"
            )
            if ok
            else reason,
            ["HC_STORAGE_POLICY_ID"],
        )
        return explicit_storage_policy_id

    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["data.fptcloud_storage_policy.this"])
        return ""
    if not vpc_id:
        emit(stage.id, "skipped", f"No VPC ID can be resolved for provider discovery; {vpc_diagnostics_message()}", ["data.fptcloud_storage_policy.this"])
        return ""

    emit(stage.id, "started", f"Using effective_vpc_id={vpc_id} for storage policy discovery", ["data.fptcloud_storage_policy.this"])
    raw_policies, error = discover_data_collection(stage.id, "fptcloud_storage_policy", "storage_policies", vpc_id)
    if error:
        context = classify_context(
            stage=stage.id,
            resource_type="fptcloud_storage_policy",
            address="data.fptcloud_storage_policy.this",
            module_path=RUN_ROOT / safe_name(stage.id),
            reason=error,
        )
        queue_error(stage.id, RUN_ROOT / safe_name(stage.id), ["data.fptcloud_storage_policy.this"], format_failure(context))
        return ""

    policies = [normalized_storage_policy(policy) for policy in raw_policies]
    policies = [policy for policy in policies if policy.get("name") or policy.get("id") or policy.get("id_db")]
    run_context["discovered_storage_policies"] = policies
    selected = next(
        (policy for policy in available_storage_policies() if storage_policy_name_matches(policy, requested_name)),
        {},
    )
    source = "provider_exact_name" if selected else "provider_inventory"
    if selected:
        run_context["effective_storage_policy_id"] = selected.get("id", "")
        run_context["storage_policy_id_source"] = source
    stage_status[stage.id] = "done"
    emit(
        stage.id,
        "done",
        (
            f"storage_policy_requested={requested_name}; "
            f"exact_match_found={bool(selected)}; "
            f"selected_storage_policy_name={selected.get('name') or '<unresolved>'}; "
            f"selected_storage_policy_id={selected.get('id') or '<unresolved>'}; "
            f"selected_storage_policy_db_id={selected.get('id_db') or '<unresolved>'}; "
            f"provider_field_used=storage_policy_id; "
            f"storage_policy_source={source}; "
            f"candidate_names={json.dumps(storage_policy_candidate_names(policies), sort_keys=True)}"
        ),
        ["data.fptcloud_storage_policy.this"],
    )
    return selected.get("id", "")


def storage_policy_fallback_spec() -> dict[str, Any]:
    raw = spec_constants().get("INSTANCE_STORAGE_POLICY_FALLBACK", {})
    return dict(raw) if isinstance(raw, dict) else {}


def fallback_storage_policy_allowed(classification: str, current_name: str, fallback_attempts: int) -> bool:
    if is_quota_error(classification):
        return False
    spec = storage_policy_fallback_spec()
    try:
        max_attempts = int(spec.get("max_fallback_attempts", 0))
    except (TypeError, ValueError):
        max_attempts = 0
    return (
        bool(spec.get("enabled"))
        and classification == spec.get("on_classification")
        and current_name == spec.get("from")
        and fallback_attempts < max_attempts
        and bool(storage_policy_by("name", str(spec.get("to") or "")))
    )


def select_instance_storage_policy(discovered_policy_id: str = "") -> dict[str, str]:
    provider_field_used = "storage_policy_id"
    requested_name = preferred_instance_storage_policy_name()
    policy = next(
        (candidate for candidate in available_storage_policies() if storage_policy_name_matches(candidate, requested_name)),
        {},
    )
    selected_name = str(policy.get("name") or "")
    selected_id = str(policy.get("id") or "")
    selected_db_id = str(policy.get("id_db") or "")
    provider_value = selected_id
    source = "preferred_exact_name" if provider_value else "unresolved"
    return {
        "requested_name": requested_name,
        "name": selected_name,
        "id": selected_id,
        "id_db": selected_db_id,
        "provider_value": provider_value,
        "source": source if provider_value else "unresolved",
        "provider_field_used": provider_field_used,
        "classification": "" if provider_value else "storage_policy_preferred_not_found",
    }


def apply_selected_storage_policy(selection: dict[str, str], *, quota_status: str = "not_available") -> None:
    run_context["storage_policy_requested"] = selection.get("requested_name", "")
    run_context["selected_storage_policy_name"] = selection.get("name", "")
    run_context["selected_storage_policy_id"] = selection.get("id", "")
    run_context["selected_storage_policy_db_id"] = selection.get("id_db", "")
    run_context["selected_storage_policy_provider_field_used"] = selection.get("provider_field_used", "storage_policy_id")
    run_context["selected_storage_policy_quota_status"] = quota_status
    run_context["effective_storage_policy_id"] = selection.get("provider_value", "")
    run_context["storage_policy_id_source"] = selection.get("source", "unresolved")


def validate_instance_storage_policy_stage(stage: StageSpec | None, discovered_policy_id: str) -> str:
    if not stage:
        return discovered_policy_id
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["storage-policy-selection"])
        return discovered_policy_id
    blocked = [name for name in stage.dependency_stages if name != INSTANCE_STORAGE_POLICY_VALIDATE_STAGE and not stage_ok(name)]
    if blocked:
        emit(stage.id, "skipped", f"Classification: instance_missing_required_inputs; Blocked by incomplete dependency stage(s): {', '.join(blocked)}", ["storage-policy-selection"])
        return ""
    disk_size, disk_error = root_disk_size()
    selection = select_instance_storage_policy(discovered_policy_id)
    apply_selected_storage_policy(selection)
    message = (
        f"storage_policy_requested={selection.get('requested_name') or '<unset>'}; "
        f"selected_storage_policy_name={selection.get('name') or '<unresolved>'}; "
        f"selected_storage_policy_id={selection.get('id') or '<unresolved>'}; "
        f"selected_storage_policy_db_id={selection.get('id_db') or '<unresolved>'}; "
        f"storage_policy_source={selection.get('source', 'unresolved')}; "
        f"provider_field_used={selection.get('provider_field_used')}; "
        f"disk_size_gb={disk_size or '<invalid>'}; quota_status={run_context.get('selected_storage_policy_quota_status')}"
    )
    if disk_error or not selection.get("provider_value"):
        errors = [error for error in (disk_error, "storage policy unresolved" if not selection.get("provider_value") else "") if error]
        classification = selection.get("classification") or "instance_missing_required_inputs"
        emit(stage.id, "skipped", f"Classification: {classification}; errors={'; '.join(errors)}; {message}", ["storage-policy-selection"])
        return ""
    stage_status[stage.id] = "done"
    emit(stage.id, "done", message, ["storage-policy-selection"])
    return selection.get("provider_value", "")


def instance_name(suffix: str) -> str:
    return f"{env('HC_INSTANCE_NAME_PREFIX', 'hc-vm')}-{suffix}"


def matrix_instance_name(label: str, suffix: str) -> str:
    return f"{env('HC_INSTANCE_NAME_PREFIX', 'hc-vm')}-{safe_name(label)}-{INSTANCE_RUN_SUFFIX}"


def os_family(label: str) -> str:
    return "windows" if label.startswith("windows-") else "linux"


def label_version_token(label: str) -> str:
    return "".join(part for part in label.split("-") if part.isdigit())


def hostname_random(length: int = 6) -> str:
    alphabet = ascii_lowercase + digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def guest_hostname_for_label(label: str) -> str:
    token = label_version_token(label)
    if os_family(label) == "windows":
        # hcw + four-digit year + six random chars = 13 chars, below Windows' 15-char limit.
        return f"hcw{token[:4]}{hostname_random(6)}"
    short_label = label.replace("ubuntu-", "ubuntu").replace("-", "")
    return f"hcl-{short_label}-{hostname_random(6)}"


def hostname_policy_for_label(label: str) -> dict[str, Any]:
    policy = spec_constants().get("INSTANCE_HOSTNAME_POLICY", {})
    if isinstance(policy, dict):
        value = policy.get(os_family(label), {})
        if isinstance(value, dict):
            return value
    return {}


def validate_guest_hostname(label: str, hostname: str) -> list[str]:
    policy = hostname_policy_for_label(label)
    max_length = int(policy.get("max_length") or (15 if os_family(label) == "windows" else 63))
    pattern = str(policy.get("pattern") or r"^[A-Za-z][A-Za-z0-9-]*[A-Za-z0-9]$")
    errors: list[str] = []
    if len(hostname) > max_length:
        errors.append(f"hostname length {len(hostname)} exceeds max {max_length}")
    if not re.fullmatch(pattern, hostname):
        errors.append(f"hostname does not match pattern {pattern}")
    return errors


def resolved_instance_labels() -> list[str]:
    discovered_images = dict(run_context.get("discovered_instance_images") or {})
    require_all_images = env_bool_default("HC_REQUIRE_ALL_INSTANCE_IMAGES", False)
    labels: list[str] = []
    for label, var_name in INSTANCE_IMAGE_MATRIX:
        if env(var_name) or discovered_images.get(label):
            labels.append(label)
        elif require_all_images:
            labels.append(label)
    return labels


def validate_instance_hostname_stage(stage: StageSpec | None, suffix: str) -> None:
    run_context["validated_instance_hostnames"] = {}
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["hostname-selection"])
        return
    blocked = [name for name in stage.dependency_stages if name != INSTANCE_HOSTNAME_VALIDATE_STAGE and not stage_ok(name)]
    if blocked:
        emit(stage.id, "skipped", f"Classification: instance_hostname_invalid; Blocked by incomplete dependency stage(s): {', '.join(blocked)}", ["hostname-selection"])
        return
    validated: dict[str, dict[str, Any]] = {}
    details: list[str] = []
    all_errors: list[str] = []
    for label in resolved_instance_labels():
        resource_name = matrix_instance_name(label, suffix)
        guest_hostname = guest_hostname_for_label(label)
        errors = validate_guest_hostname(label, guest_hostname)
        valid = not errors
        entry = {
            "os_label": label,
            "resource_name": resource_name,
            "guest_hostname": guest_hostname,
            "selected_hostname": guest_hostname,
            "hostname_length": len(guest_hostname),
            "hostname_valid": valid,
            "hostname_validation_status": "passed" if valid else "failed",
            "validation_errors": errors,
        }
        validated[label] = entry
        if errors:
            all_errors.extend(f"{label}: {error}" for error in errors)
        details.append(
            f"os_label={label}; resource_name={resource_name}; guest_hostname={guest_hostname}; "
            f"selected_hostname={guest_hostname}; hostname_length={len(guest_hostname)}; "
            f"hostname_valid={valid}; validation_errors={','.join(errors) if errors else '<none>'}"
        )
    run_context["validated_instance_hostnames"] = validated
    message = f"validated_instance_hostnames={json.dumps(validated, sort_keys=True)}; " + " | ".join(details)
    if all_errors:
        emit(stage.id, "skipped", f"Classification: instance_hostname_invalid; {message}", ["hostname-selection"])
        return
    stage_status[stage.id] = "done"
    emit(stage.id, "done", message, ["hostname-selection"])


def root_disk_size_source() -> str:
    if env("HC_INSTANCE_DISK_SIZE_GB"):
        return "HC_INSTANCE_DISK_SIZE_GB"
    if env("HC_ROOT_DISK_SIZE"):
        return "HC_ROOT_DISK_SIZE"
    return "default"


def root_disk_size() -> tuple[int | None, str]:
    # Governed by specs/health-check.json INSTANCE_BATCHING_POLICY and INSTANCE_QUOTA_INSPECTION_POLICY.
    policy_default = instance_batching_policy().get(
        "HC_INSTANCE_DISK_SIZE_GB",
        spec_constants().get("INSTANCE_QUOTA_INSPECTION_POLICY", {}).get("HC_INSTANCE_DISK_SIZE_GB_default", 40),
    )
    raw = env("HC_INSTANCE_DISK_SIZE_GB") or env("HC_ROOT_DISK_SIZE") or str(policy_default)
    try:
        value = int(raw)
    except ValueError:
        return None, f"{root_disk_size_source()} must be an integer"
    if value < 10:
        return None, f"{root_disk_size_source()} must be at least 10"
    return value, ""


def official_instance_disk_size_gb() -> int:
    # Governed by specs/health-check.json INSTANCE_BATCHING_POLICY.
    try:
        return int(instance_batching_policy().get("HC_INSTANCE_DISK_SIZE_GB", 40) or 40)
    except (TypeError, ValueError):
        return 40


def reduced_disk_test(disk_size: int | None = None) -> bool:
    value = disk_size if disk_size is not None else root_disk_size()[0]
    official = official_instance_disk_size_gb()
    return bool(value and value != official)


def not_available_quota_report(target_disk_size: int | None = None) -> dict[str, Any]:
    disk_size = target_disk_size if target_disk_size is not None else root_disk_size()[0]
    return {
        "quota_source": "unsupported_or_not_found",
        "quota_status": "not_available",
        "quota_precheck": "disabled",
        "quota_assumption": "assume_sufficient",
        "quota_exceeded_action": "stop_and_wait_for_user",
        "tenant_quota": "not_available",
        "vpc_quota": "not_available",
        "used_storage_gb": "not_available",
        "remaining_storage_gb": "not_available",
        "storage_policy_quota_gb": "not_available",
        "storage_policy_used_gb": "not_available",
        "vm_count_quota": "not_available",
        "vm_count_used": "not_available",
        "vm_count_remaining": "not_available",
        "cpu_quota": "not_available",
        "cpu_used": "not_available",
        "cpu_remaining": "not_available",
        "ram_quota_mb": "not_available",
        "ram_used_mb": "not_available",
        "ram_remaining_mb": "not_available",
        "disk_quota_gb": "not_available",
        "existing_instances_consuming_storage": "not_available",
        "existing_volumes_consuming_storage": "not_available",
        "target_requested_disk_size_gb": disk_size or "not_available",
        "requested_instance_count": "not_available",
        "requested_total_storage_gb": "not_available",
        "requested_cpu": "not_available",
        "requested_ram_mb": "not_available",
        "reduced_disk_test": reduced_disk_test(disk_size),
        "provider_schema_quota_fields": [],
        "provider_instance_inventory": "single_lookup_only",
        "provider_storage_inventory": "single_lookup_only",
    }


def optimistic_quota_report(target_disk_size: int | None = None) -> dict[str, Any]:
    report = not_available_quota_report(target_disk_size)
    report.update(
        {
            "quota_source": "optimistic_apply_only",
            "quota_status": "assumed_sufficient",
            "quota_precheck": "disabled",
            "quota_assumption": "assume_sufficient",
            "quota_exceeded_action": "stop_and_wait_for_user",
            "stop_on_quota_exceeded": True,
            "run_status": run_context.get("run_status", "running"),
            "user_action_required": bool(run_context.get("user_action_required", False)),
        }
    )
    return report


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def find_export_value(data: Any, candidates: tuple[str, ...]) -> Any:
    wanted = {normalized_key(candidate) for candidate in candidates}

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            for key, item in value.items():
                if normalized_key(str(key)) in wanted:
                    return item
            for item in value.values():
                found = walk(item)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found is not None:
                    return found
        return None

    return walk(data)


def load_quota_export(path_value: str) -> tuple[dict[str, Any], str]:
    if not path_value:
        return {}, ""
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    try:
        return json.loads(path.read_text(encoding="utf-8")), str(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"{path}: {exc}"


def apply_quota_export(report: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    # Governed by specs/health-check.json compute.inspect-instance-quota.
    mappings: dict[str, tuple[str, ...]] = {
        "tenant_quota": ("tenant_quota", "tenantStorageQuota", "tenant_storage_quota"),
        "vpc_quota": ("vpc_quota", "vpcStorageQuota", "vpc_storage_quota"),
        "used_storage_gb": ("used_storage_gb", "usedStorageGb", "storage_used_gb", "storageUsedGb"),
        "remaining_storage_gb": ("remaining_storage_gb", "remainingStorageGb", "storage_remaining_gb", "free_storage_gb"),
        "storage_policy_quota_gb": ("storage_policy_quota_gb", "storagePolicyQuotaGb", "policy_quota_gb"),
        "storage_policy_used_gb": ("storage_policy_used_gb", "storagePolicyUsedGb", "policy_used_gb"),
        "vm_count_quota": ("vm_count_quota", "vmQuota", "instance_quota"),
        "vm_count_used": ("vm_count_used", "vmUsed", "instance_count_used"),
        "vm_count_remaining": ("vm_count_remaining", "vmRemaining", "instance_count_remaining"),
        "cpu_quota": ("cpu_quota", "cpuQuota", "vcpu_quota"),
        "cpu_used": ("cpu_used", "cpuUsed", "vcpu_used"),
        "cpu_remaining": ("cpu_remaining", "cpuRemaining", "vcpu_remaining"),
        "ram_quota_mb": ("ram_quota_mb", "ramQuotaMb", "memory_quota_mb"),
        "ram_used_mb": ("ram_used_mb", "ramUsedMb", "memory_used_mb"),
        "ram_remaining_mb": ("ram_remaining_mb", "ramRemainingMb", "memory_remaining_mb"),
        "disk_quota_gb": ("disk_quota_gb", "diskQuotaGb"),
        "existing_instances_consuming_storage": ("existing_instances_consuming_storage", "instances", "vms", "virtual_machines"),
        "existing_volumes_consuming_storage": ("existing_volumes_consuming_storage", "volumes", "disks", "storages"),
    }
    found_any = False
    updated = dict(report)
    for field, candidates in mappings.items():
        value = find_export_value(data, candidates)
        if value is not None:
            updated[field] = value
            found_any = True
    if found_any:
        updated["quota_source"] = "HC_QUOTA_EXPORT_JSON"
        updated["quota_status"] = "available"
    return updated


def inspect_provider_quota_capabilities() -> dict[str, Any]:
    # Spec-approved local schema inspection; no cloud/API call is made.
    capabilities = {
        "provider_schema_quota_fields": [],
        "provider_instance_inventory": "not_checked",
        "provider_storage_inventory": "not_checked",
    }
    try:
        raw = subprocess.check_output(
            ["terraform", "providers", "schema", "-json"],
            cwd=MODULES / "vm",
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        schema = json.loads(raw)["provider_schemas"]["registry.terraform.io/fpt-corp/fptcloud"]
    except (KeyError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return capabilities
    quota_fields: list[str] = []
    for schema_group in ("resource_schemas", "data_source_schemas"):
        for name, value in schema.get(schema_group, {}).items():
            attrs = (value.get("block") or {}).get("attributes", {})
            for attr in attrs:
                if "quota" in attr.lower():
                    quota_fields.append(f"{schema_group}.{name}.{attr}")
    data_sources = schema.get("data_source_schemas", {})
    instance_attrs = (data_sources.get("fptcloud_instance", {}).get("block") or {}).get("attributes", {})
    storage_attrs = (data_sources.get("fptcloud_storage", {}).get("block") or {}).get("attributes", {})
    capabilities["provider_schema_quota_fields"] = quota_fields
    capabilities["provider_instance_inventory"] = "single_lookup_only" if instance_attrs else "not_available"
    capabilities["provider_storage_inventory"] = "single_lookup_only" if storage_attrs else "not_available"
    return capabilities


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


def quota_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, "", "not_available"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if match:
            return float(match.group(0))
    return None


def inspect_instance_quota_stage(stage: StageSpec | None) -> None:
    run_context["instance_quota"] = optimistic_quota_report()
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["quota-inspection"])
        return
    blocked = [name for name in stage.dependency_stages if name != INSTANCE_QUOTA_INSPECT_STAGE and not stage_ok(name)]
    if blocked:
        emit(stage.id, "skipped", f"Classification: instance_storage_quota_exceeded; Blocked by incomplete dependency stage(s): {', '.join(blocked)}", ["quota-inspection"])
        return
    disk_size, disk_error = root_disk_size()
    report = optimistic_quota_report(disk_size)
    if disk_error:
        report["quota_input_error"] = disk_error
    run_context["instance_quota"] = report
    stage_status[stage.id] = "done"
    emit(stage.id, "done", f"quota_inspection=disabled; {quota_report_message(report)}", ["quota-inspection"])


def validate_instance_quota_stage(stage: StageSpec | None) -> None:
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["quota-validation"])
        return
    blocked = [name for name in stage.dependency_stages if name != INSTANCE_QUOTA_VALIDATE_STAGE and not stage_ok(name)]
    if blocked:
        emit(stage.id, "skipped", f"Classification: instance_storage_quota_exceeded_preflight; Blocked by incomplete dependency stage(s): {', '.join(blocked)}", ["quota-validation"])
        return
    disk_size, disk_error = root_disk_size()
    report = optimistic_quota_report(disk_size)
    if disk_error:
        report["quota_input_error"] = disk_error
    run_context["instance_quota"] = report
    stage_status[stage.id] = "done"
    emit(stage.id, "done", f"preflight_decision=disabled_allow_apply; {quota_report_message(report)}", ["quota-validation"])


def instance_base_inputs(suffix: str, subnet_id: str, storage_policy_id: str) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    discovered_images = dict(run_context.get("discovered_instance_images") or {})
    image_sources = dict(run_context.get("instance_image_sources") or {})
    validated_hostnames = dict(run_context.get("validated_instance_hostnames") or {})
    flavor_name_value = str(run_context.get("discovered_instance_flavor_name") or env("HC_FLAVOR_NAME"))
    flavor_source = str(run_context.get("instance_flavor_source") or ("explicit_env" if env("HC_FLAVOR_NAME") or env("HC_FLAVOR_ID") else "unresolved"))
    disk_size, disk_error = root_disk_size()
    errors: list[str] = []
    require_all_images = env_bool_default("HC_REQUIRE_ALL_INSTANCE_IMAGES", False)
    if not GENERATED_INSTANCE_PASSWORD:
        errors.append("generated_instance_password is required")
    missing_images = [
        label
        for label, var_name in INSTANCE_IMAGE_MATRIX
        if not (env(var_name) or discovered_images.get(label))
    ]
    for label in missing_images:
        if require_all_images:
            errors.append(f"discovered image for {label} is required")
    if not flavor_name_value:
        errors.append("discovered_instance_flavor is required")
    if not run_context.get("effective_vpc_id"):
        errors.append("effective_vpc_id is required")
    if not subnet_id:
        errors.append("effective_subnet_id is required")
    if not storage_policy_id:
        errors.append("effective_storage_policy_id is required")
    if not validated_hostnames:
        errors.append("validated_instance_hostnames is required")
    if disk_error:
        errors.append(disk_error)
    keep_instance = keep_instance_enabled()
    cleanup_on_quota_exceeded = cleanup_on_quota_exceeded_enabled()
    quota_report = dict(run_context.get("instance_quota") or optimistic_quota_report(disk_size))
    security_group_id = env("HC_SECURITY_GROUP_ID")
    instances: list[dict[str, Any]] = []
    for label, var_name in INSTANCE_IMAGE_MATRIX:
        if not require_all_images and not (env(var_name) or discovered_images.get(label)):
            continue
        hostname_entry = dict(validated_hostnames.get(label) or {})
        resource_name = str(hostname_entry.get("resource_name") or matrix_instance_name(label, suffix))
        guest_hostname = str(hostname_entry.get("guest_hostname") or "")
        if not resource_name:
            errors.append(f"resource_name could not be generated for {label}")
        if not guest_hostname:
            errors.append(f"guest_hostname is required for {label}")
        if hostname_entry and not hostname_entry.get("hostname_valid"):
            errors.append(f"guest_hostname for {label} failed validation")
        instances.append(
            {
                "label": label,
                "image_env": var_name,
                "resource_name": resource_name,
                "guest_hostname": guest_hostname,
                "hostname": hostname_entry,
                "vars": {
                    "name": guest_hostname,
                    "vpc_id": run_context.get("effective_vpc_id", ""),
                    "image_name": env(var_name) or discovered_images.get(label, ""),
                    "flavor_name": flavor_name_value,
                    "storage_policy_id": storage_policy_id,
                    "disk_gb": disk_size or official_instance_disk_size_gb(),
                    "subnet_id": subnet_id,
                    "status": "POWERED_ON",
                    "password": GENERATED_INSTANCE_PASSWORD,
                    "ssh_key": env("HC_SSH_KEY") or None,
                    "security_group_ids": [security_group_id] if security_group_id else [],
                    "tags": build_hc_instance_tags(
                        vpc_name=run_context.get("vpc_name") or vpc_lookup_key(),
                        os_label=label,
                        created_at=now(),
                    ),
                },
            }
        )
    sample_vars = instances[0]["vars"] if instances else {}
    sample_hostname = dict((instances[0].get("hostname") if instances else {}) or {})
    diagnostics = {
        "instance_name": sample_vars.get("name", ""),
        "resource_name": sample_hostname.get("resource_name", ""),
        "guest_hostname": sample_hostname.get("guest_hostname", ""),
        "selected_hostname": sample_hostname.get("selected_hostname", ""),
        "hostname_length": sample_hostname.get("hostname_length", 0),
        "hostname_validation_status": sample_hostname.get("hostname_validation_status", "unresolved"),
        "validated_instance_hostnames": validated_hostnames,
        "matrix_count": len(INSTANCE_IMAGE_MATRIX),
        "images": {label: {"env_var": var_name, "value": env(var_name) or discovered_images.get(label, ""), "source": image_sources.get(label, "explicit_env" if env(var_name) else "unresolved")} for label, var_name in INSTANCE_IMAGE_MATRIX},
        "effective_vpc_id": sample_vars.get("vpc_id", ""),
        "effective_subnet_id": subnet_id,
        "subnet_source": run_context.get("subnet_id_source", "unresolved"),
        "effective_storage_policy_id": storage_policy_id,
        "storage_policy_source": run_context.get("storage_policy_id_source", "unresolved"),
        "storage_policy_requested": run_context.get("storage_policy_requested", preferred_instance_storage_policy_name()),
        "selected_storage_policy_name": run_context.get("selected_storage_policy_name", ""),
        "selected_storage_policy_id": run_context.get("selected_storage_policy_id", ""),
        "selected_storage_policy_db_id": run_context.get("selected_storage_policy_db_id", ""),
        "provider_field_used": run_context.get("selected_storage_policy_provider_field_used", "storage_policy_id"),
        "storage_policy_quota_status": run_context.get("selected_storage_policy_quota_status", "not_available"),
        "image_source": "discovered" if not missing_images else "unresolved",
        "flavor_input": flavor_name_value,
        "flavor_source": flavor_source,
        "ssh_key_present": bool(env("HC_SSH_KEY")),
        "password_generated": bool(GENERATED_INSTANCE_PASSWORD),
        "password_redacted": True,
        "disk_size": disk_size or official_instance_disk_size_gb(),
        "disk_size_source": root_disk_size_source(),
        "reduced_disk_test": reduced_disk_test(disk_size),
        "keep_instance": keep_instance,
        "cleanup_on_quota_exceeded": cleanup_on_quota_exceeded,
        "cleanup_policy": "retain_by_default",
        "quota_precheck": quota_report.get("quota_precheck", "disabled"),
        "quota_assumption": quota_report.get("quota_assumption", "assume_sufficient"),
        "quota_exceeded_action": quota_report.get("quota_exceeded_action", "stop_and_wait_for_user"),
        "quota_source": quota_report.get("quota_source", "unsupported_or_not_found"),
        "quota_status": quota_report.get("quota_status", "not_available"),
        "remaining_storage_gb": quota_report.get("remaining_storage_gb", "not_available"),
        "target_requested_disk_size_gb": quota_report.get("target_requested_disk_size_gb", disk_size or "not_available"),
        "run_status": quota_report.get("run_status", run_context.get("run_status", "running")),
        "user_action_required": quota_report.get("user_action_required", run_context.get("user_action_required", False)),
        "require_all_images": require_all_images,
        "resolved_count": len(instances),
        "unresolved_count": len(INSTANCE_IMAGE_MATRIX) - len(instances),
    }
    return {"instances": instances}, diagnostics, errors


def validate_instance_stage(stage: StageSpec | None, suffix: str, subnet_id: str, storage_policy_id: str) -> None:
    instance_validation.update({"valid": False, "vars": {}, "diagnostics": {}, "errors": []})
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["module.vm"])
        return
    blocked = [name for name in stage.dependency_stages if name != INSTANCE_VALIDATE_STAGE and not stage_ok(name)]
    if blocked:
        emit(stage.id, "skipped", f"Classification: instance_missing_required_inputs; Blocked by incomplete dependency stage(s): {', '.join(blocked)}", ["module.vm"])
        return
    vars, diagnostics, errors = instance_base_inputs(suffix, subnet_id, storage_policy_id)
    if errors:
        classification = "instance_missing_required_inputs"
        if any("generated_instance_password" in error for error in errors):
            classification = "instance_password_missing"
        elif any("image" in error.lower() for error in errors):
            classification = "instance_image_unresolved"
        elif any("flavor" in error.lower() for error in errors):
            classification = "instance_flavor_unresolved"
        elif any("hostname" in error.lower() or "resource_name" in error.lower() for error in errors):
            classification = "instance_hostname_invalid"
        instance_validation.update({"valid": False, "vars": vars, "diagnostics": diagnostics, "errors": errors})
        emit(
            stage.id,
            "skipped",
            (
                f"Classification: {classification}; errors={'; '.join(errors)}; "
                f"matrix_count={diagnostics['matrix_count']}; image_source={diagnostics['image_source']}; flavor_source={diagnostics['flavor_source']}; "
                f"subnet_source={diagnostics['subnet_source']}; storage_policy_source={diagnostics['storage_policy_source']}; "
                f"storage_policy_requested={diagnostics['storage_policy_requested']}; "
                f"selected_storage_policy_name={diagnostics['selected_storage_policy_name'] or '<unresolved>'}; "
                f"selected_storage_policy_id={diagnostics['selected_storage_policy_id'] or '<unresolved>'}; "
                f"selected_storage_policy_db_id={diagnostics['selected_storage_policy_db_id'] or '<unresolved>'}; "
                f"provider_field_used={diagnostics['provider_field_used']}; storage_policy_quota_status={diagnostics['storage_policy_quota_status']}; "
                f"quota_precheck={diagnostics['quota_precheck']}; quota_assumption={diagnostics['quota_assumption']}; "
                f"quota_exceeded_action={diagnostics['quota_exceeded_action']}; "
                f"quota_source={diagnostics['quota_source']}; quota_status={diagnostics['quota_status']}; "
                f"run_status={diagnostics['run_status']}; user_action_required={diagnostics['user_action_required']}; "
                f"remaining_storage_gb={diagnostics['remaining_storage_gb']}; target_requested_disk_size_gb={diagnostics['target_requested_disk_size_gb']}; "
                f"resource_name={diagnostics.get('resource_name') or '<unresolved>'}; "
                f"guest_hostname={diagnostics.get('guest_hostname') or '<unresolved>'}; "
                f"selected_hostname={diagnostics.get('selected_hostname') or '<unresolved>'}; "
                f"hostname_length={diagnostics.get('hostname_length')}; "
                f"hostname_validation_status={diagnostics.get('hostname_validation_status')}; "
                f"instance_name_pattern={env('HC_INSTANCE_NAME_PREFIX', 'hc-vm')}-<os>-{suffix[-8:]}; disk_size={diagnostics['disk_size']}; "
                f"disk_size_source={diagnostics['disk_size_source']}; reduced_disk_test={diagnostics['reduced_disk_test']}; "
                f"password_generated={diagnostics['password_generated']}; password_redacted={diagnostics['password_redacted']}; ssh_key_present={diagnostics['ssh_key_present']}; "
                f"{cleanup_policy_summary()}; "
                f"terraform_vars={json.dumps(redacted_vars(vars), sort_keys=True)}"
            ),
            ["module.this.fptcloud_instance.this"],
        )
        return
    instance_validation.update({"valid": True, "vars": vars, "diagnostics": diagnostics, "errors": []})
    stage_status[stage.id] = "done"
    emit(
        stage.id,
        "done",
        (
            "Instance input validation passed; "
            f"effective_vpc_id={diagnostics['effective_vpc_id']}; "
            f"effective_subnet_id={diagnostics['effective_subnet_id']}; "
            f"effective_storage_policy_id={diagnostics['effective_storage_policy_id']}; "
            f"matrix_count={diagnostics['matrix_count']}; image_source={diagnostics['image_source']}; flavor_source={diagnostics['flavor_source']}; "
            f"subnet_source={diagnostics['subnet_source']}; storage_policy_source={diagnostics['storage_policy_source']}; "
            f"storage_policy_requested={diagnostics['storage_policy_requested']}; "
            f"selected_storage_policy_name={diagnostics['selected_storage_policy_name'] or '<unresolved>'}; "
            f"selected_storage_policy_id={diagnostics['selected_storage_policy_id'] or '<unresolved>'}; "
            f"selected_storage_policy_db_id={diagnostics['selected_storage_policy_db_id'] or '<unresolved>'}; "
            f"provider_field_used={diagnostics['provider_field_used']}; storage_policy_quota_status={diagnostics['storage_policy_quota_status']}; "
            f"quota_precheck={diagnostics['quota_precheck']}; quota_assumption={diagnostics['quota_assumption']}; "
            f"quota_exceeded_action={diagnostics['quota_exceeded_action']}; "
            f"quota_source={diagnostics['quota_source']}; quota_status={diagnostics['quota_status']}; "
            f"run_status={diagnostics['run_status']}; user_action_required={diagnostics['user_action_required']}; "
            f"remaining_storage_gb={diagnostics['remaining_storage_gb']}; target_requested_disk_size_gb={diagnostics['target_requested_disk_size_gb']}; "
            f"resource_name={diagnostics.get('resource_name') or '<unresolved>'}; "
            f"guest_hostname={diagnostics.get('guest_hostname') or '<unresolved>'}; "
            f"selected_hostname={diagnostics.get('selected_hostname') or '<unresolved>'}; "
            f"hostname_length={diagnostics.get('hostname_length')}; "
            f"hostname_validation_status={diagnostics.get('hostname_validation_status')}; "
            f"instance_name_pattern={env('HC_INSTANCE_NAME_PREFIX', 'hc-vm')}-<os>-{suffix[-8:]}; disk_size={diagnostics['disk_size']}; "
            f"disk_size_source={diagnostics['disk_size_source']}; reduced_disk_test={diagnostics['reduced_disk_test']}; "
            f"password_generated={diagnostics['password_generated']}; password_redacted={diagnostics['password_redacted']}; ssh_key_present={diagnostics['ssh_key_present']}; "
            f"{cleanup_policy_summary()}; "
            f"terraform_vars={json.dumps(redacted_vars(vars), sort_keys=True)}"
        ),
        ["module.this.fptcloud_instance.this"],
    )


def selected_round_labels() -> list[str]:
    round_info = dict(run_context.get("selected_instance_round") or {})
    return [str(label) for label in round_info.get("selected_images", [])]


def selected_round_instances() -> list[dict[str, Any]]:
    instances = list((instance_validation.get("vars") or {}).get("instances") or [])
    selected = set(selected_round_labels())
    if not selected:
        return instances
    return [item for item in instances if str(item.get("label") or "") in selected]


def select_instance_round_stage(stage: StageSpec | None) -> None:
    # Governed by specs/health-check.json INSTANCE_BATCHING_POLICY and compute.select-instance-round.
    run_context["selected_instance_round"] = {}
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["instance-round-selection"])
        return
    blocked = [name for name in stage.dependency_stages if name != INSTANCE_ROUND_SELECT_STAGE and not stage_ok(name)]
    if blocked:
        emit(stage.id, "skipped", f"Classification: instance_image_unresolved; Blocked by incomplete dependency stage(s): {', '.join(blocked)}", ["instance-round-selection"])
        return
    instances = list((instance_validation.get("vars") or {}).get("instances") or [])
    discovered_images = dict(run_context.get("discovered_instance_images") or {})
    resolved_labels = [str(item.get("label") or "") for item in instances if item.get("label")]
    unavailable = [
        label
        for label, _var_name in INSTANCE_IMAGE_MATRIX
        if label not in resolved_labels and not discovered_images.get(label)
    ]
    order = instance_selection_order()
    selected = [label for label in order if label in resolved_labels]
    disk_size, _disk_error = root_disk_size()
    per_apply = instances_per_apply()
    apply_count = min(per_apply, len(selected))
    round_info = {
        "instances_per_apply": per_apply,
        "stop_on_quota_exceeded": stop_on_quota_exceeded_enabled(),
        "selected_images": selected,
        "successful_images": [],
        "failed_image": "",
        "failure_reason": "",
        "remaining_images_not_attempted": [],
        "unavailable_images": unavailable,
        "apply_requested_instance_count": apply_count,
        "apply_requested_storage_gb": apply_count * int(disk_size or 0),
        "apply_requested_cpu": apply_count * 2,
        "apply_requested_ram_mb": apply_count * 2048,
    }
    run_context["selected_instance_round"] = round_info
    if not selected:
        emit(stage.id, "skipped", f"Classification: instance_image_unresolved; selected_images=[]; unavailable_images={json.dumps(unavailable)}", ["instance-round-selection"])
        return
    stage_status[stage.id] = "done"
    emit(
        stage.id,
        "done",
        (
            f"instances_per_apply={per_apply}; stop_on_quota_exceeded={stop_on_quota_exceeded_enabled()}; "
            f"selected_images={json.dumps(selected)}; successful_images=[]; failed_image=<none>; "
            f"failure_reason=<none>; remaining_images_not_attempted=[]; unavailable_images={json.dumps(unavailable)}; "
            f"apply_requested_instance_count={round_info['apply_requested_instance_count']}; "
            f"apply_requested_storage_gb={round_info['apply_requested_storage_gb']}; "
            f"apply_requested_cpu={round_info['apply_requested_cpu']}; "
            f"apply_requested_ram_mb={round_info['apply_requested_ram_mb']}"
        ),
        ["instance-round-selection"],
    )


def validate_instance_network_stage(stage: StageSpec | None) -> None:
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["module.this.fptcloud_instance.this"])
        return
    blocked = [name for name in stage.dependency_stages if name != INSTANCE_NETWORK_VALIDATE_STAGE and not stage_ok(name)]
    if blocked:
        emit(stage.id, "skipped", f"Classification: instance_network_attachment_missing; Blocked by incomplete dependency stage(s): {', '.join(blocked)}", ["module.this.fptcloud_instance.this"])
        return
    vars_payload = dict(instance_validation.get("vars") or {})
    instances = selected_round_instances()
    errors: list[str] = []
    if not run_context.get("effective_vpc_id"):
        errors.append("effective_vpc_id is required")
    if not run_context.get("effective_subnet_id"):
        errors.append("effective_subnet_id is required")
    if not run_context.get("effective_storage_policy_id"):
        errors.append("effective_storage_policy_id is required")
    if not instances:
        errors.append("prepared instance Terraform variables are required")
    for item in instances:
        label = str(item.get("label") or "unknown")
        item_vars = dict(item.get("vars") or {})
        if not item_vars.get("vpc_id"):
            errors.append(f"{label}: vpc_id network attachment is required")
        if not item_vars.get("subnet_id"):
            errors.append(f"{label}: subnet_id network attachment is required")
        if not item_vars.get("storage_policy_id"):
            errors.append(f"{label}: storage_policy_id is required")
    names = ", ".join(str((item.get("vars") or {}).get("name") or item.get("label") or "") for item in instances)
    first_vars = dict((instances[0].get("vars") if instances else {}) or {})
    public_ip_configured = bool(first_vars.get("public_ip") or env("HC_PUBLIC_IP"))
    security_group_configured = bool(first_vars.get("security_group_ids") or env("HC_SECURITY_GROUP_ID"))
    message = (
        f"generated_suffix={INSTANCE_RUN_SUFFIX}; effective_vpc_id={run_context.get('effective_vpc_id') or '<unset>'}; "
        f"effective_subnet_id={run_context.get('effective_subnet_id') or '<unset>'}; "
        f"effective_storage_policy_id={run_context.get('effective_storage_policy_id') or '<unset>'}; "
        f"network_attachment_fields=vpc_id,subnet_id; subnet_vpc_membership=not_verified; "
        f"security_group_configured={security_group_configured}; public_ip_configured={public_ip_configured}; "
        f"connection_test_policy=manual_verification_required; instance_names={names or '<none>'}; "
        f"terraform_network_vars={json.dumps(redacted_vars({'vpc_id': first_vars.get('vpc_id'), 'subnet_id': first_vars.get('subnet_id'), 'storage_policy_id': first_vars.get('storage_policy_id'), 'security_group_ids': list(first_vars.get('security_group_ids') or [])}), sort_keys=True)}"
    )
    if errors:
        emit(stage.id, "skipped", f"Classification: instance_network_attachment_missing; errors={'; '.join(errors)}; {message}", ["module.this.fptcloud_instance.this"])
        return
    stage_status[stage.id] = "done"
    emit(stage.id, "done", f"Network attachment validation passed; {message}", ["module.this.fptcloud_instance.this"])


def resolve_instance_image_flavor(vars: dict[str, Any], diagnostics: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    resolved = dict(vars)
    updated = dict(diagnostics)
    vpc_id = str(resolved.get("vpc_id") or "")
    updated["image_source"] = "name" if resolved.get("image_name") else "unresolved"
    if env("HC_FLAVOR_ID"):
        flavor_name_value = discover_filtered_value(
            name="flavor",
            source="fptcloud_flavor",
            collection="flavors",
            output_attr="name",
            filter_key="id",
            filter_value=env("HC_FLAVOR_ID"),
            vpc_id=vpc_id,
            stage_id="compute.resolve-instance-flavor",
        )
        if not flavor_name_value:
            updated["flavor_source"] = "unresolved"
            return resolved, updated, "Classification: instance_flavor_unresolved; HC_FLAVOR_ID did not resolve to a flavor name"
        resolved["flavor_name"] = flavor_name_value
        updated["flavor_source"] = "explicit_id"
    elif env("HC_FLAVOR_NAME"):
        flavor_name_value = discover_filtered_value(
            name="flavor",
            source="fptcloud_flavor",
            collection="flavors",
            output_attr="name",
            filter_key="name",
            filter_value=env("HC_FLAVOR_NAME"),
            vpc_id=vpc_id,
            stage_id="compute.resolve-instance-flavor",
        )
        if flavor_name_value:
            resolved["flavor_name"] = flavor_name_value
            updated["flavor_source"] = "discovered"
        else:
            updated["flavor_source"] = "name"
    return resolved, updated, ""


def destroy(workspace: Path, name: str) -> None:
    resources = state_resources(workspace)
    emit(f"{name}:destroy", "started", "Destroying created resources", resources)
    result = run(
        ["terraform", "destroy", "-auto-approve", "-no-color", "-input=false"],
        workspace,
        stage=f"{name}-destroy",
    )
    if result.returncode == 0:
        emit(f"{name}:destroy", "destroyed", "Destroy completed", resources)
    else:
        queue_error(f"{name}:destroy", workspace, resources, (result.stderr or result.stdout)[-1200:])


def instance_id_from_state(workspace: Path) -> str:
    for resource in module_resources(state_json(workspace)):
        if resource.get("type") == "fptcloud_instance":
            return str(resource.get("values", {}).get("id") or "")
    return ""


def instance_state_values(workspace: Path) -> dict[str, Any]:
    for resource in module_resources(state_json(workspace)):
        if resource.get("type") == "fptcloud_instance":
            values = resource.get("values", {})
            return dict(values) if isinstance(values, dict) else {}
    return {}


def cleanup_safety_errors(
    workspace: Path,
    *,
    classification: str,
    instance_id: str,
    expected_instance_name: str,
    resource_name: str,
) -> list[str]:
    errors: list[str] = []
    values = instance_state_values(workspace)
    state_name = str(values.get("name") or "")
    if classification not in QUOTA_CLEANUP_CLASSIFICATIONS:
        errors.append("delete reason is not quota cleanup")
    if not instance_id:
        errors.append("instance_id is missing")
    if not values:
        errors.append("instance is not managed by the current run workspace")
    if state_name != expected_instance_name:
        errors.append("instance state name does not match current run workspace inputs")
    if INSTANCE_RUN_SUFFIX not in resource_name:
        errors.append("instance resource_name does not match current run suffix")
    if RUN_ROOT not in workspace.resolve().parents and workspace.resolve() != RUN_ROOT:
        errors.append("workspace is outside the current run root")
    return errors


def cleanup_instance(
    workspace: Path,
    name: str,
    *,
    classification: str,
    expected_instance_name: str,
    resource_name: str,
) -> bool:
    # Governed by specs/health-check.json compute.cleanup-instance and INSTANCE_CLEANUP_POLICY.
    instance_id = instance_id_from_state(workspace)
    resources = state_resources(workspace)
    if not cleanup_on_quota_exceeded_enabled():
        skipped = "HC_CLEANUP_ON_QUOTA_EXCEEDED is false"
        emit(
            f"{name}:cleanup",
            "skipped",
            f"Classification: instance_retained_by_policy; {cleanup_policy_summary(classification=classification, retained_instance_ids=[instance_id] if instance_id else [], skipped_delete_reason=skipped)}",
            resources,
        )
        emit(
            INSTANCE_CLEANUP_STAGE,
            "skipped",
            f"Classification: instance_retained_by_policy; {cleanup_policy_summary(classification=classification, retained_instance_ids=[instance_id] if instance_id else [], skipped_delete_reason=skipped)}",
            resources,
        )
        return False

    safety_errors = cleanup_safety_errors(
        workspace,
        classification=classification,
        instance_id=instance_id,
        expected_instance_name=expected_instance_name,
        resource_name=resource_name,
    )
    if safety_errors:
        skipped = "; ".join(safety_errors)
        emit(
            f"{name}:cleanup",
            "skipped",
            f"Classification: instance_cleanup_not_allowed; {cleanup_policy_summary(classification=classification, retained_instance_ids=[instance_id] if instance_id else [], skipped_delete_reason=skipped)}",
            resources,
        )
        emit(
            INSTANCE_CLEANUP_STAGE,
            "skipped",
            f"Classification: instance_cleanup_not_allowed; {cleanup_policy_summary(classification=classification, retained_instance_ids=[instance_id] if instance_id else [], skipped_delete_reason=skipped)}",
            resources,
        )
        return False

    emit(
        f"{name}:cleanup",
        "started",
        f"Destroying only current-run instance for quota cleanup; discovered/shared resources are preserved; {cleanup_policy_summary(classification=classification, delete_allowed=True, delete_reason='quota cleanup')}",
        resources,
    )
    emit(
        INSTANCE_CLEANUP_STAGE,
        "started",
        f"Destroying only current-run instance for quota cleanup; {cleanup_policy_summary(classification=classification, delete_allowed=True, delete_reason='quota cleanup')}",
        resources,
    )
    result = run_instance_terraform(["terraform", "destroy", "-auto-approve", "-no-color", "-input=false"], workspace, stage=f"{name}-cleanup")
    if result.returncode == 0:
        emit(
            f"{name}:cleanup",
            "destroyed",
            f"Instance quota cleanup completed; {cleanup_policy_summary(classification=classification, delete_allowed=True, delete_reason='quota cleanup', deleted_instance_ids=[instance_id])}",
            resources,
        )
        emit(
            INSTANCE_CLEANUP_STAGE,
            "destroyed",
            f"Instance quota cleanup completed; {cleanup_policy_summary(classification=classification, delete_allowed=True, delete_reason='quota cleanup', deleted_instance_ids=[instance_id])}",
            resources,
        )
        return True
    queue_error(f"{name}:cleanup", workspace, resources, f"Classification: instance_cleanup_failed; {cleanup_policy_summary(classification=classification, retained_instance_ids=[instance_id] if instance_id else [], skipped_delete_reason='terraform destroy failed')}; {(result.stderr or result.stdout)[-1200:]}")
    queue_error(INSTANCE_CLEANUP_STAGE, workspace, resources, f"Classification: instance_cleanup_failed; {cleanup_policy_summary(classification=classification, retained_instance_ids=[instance_id] if instance_id else [], skipped_delete_reason='terraform destroy failed')}; {(result.stderr or result.stdout)[-1200:]}")
    return False


def retain_instance(
    stage: str,
    *,
    label: str,
    instance_id: str,
    classification: str,
    failed: bool,
    resources: list[str],
) -> None:
    retained = [instance_id] if instance_id else []
    retained_classification = "retained_failed_instance" if failed else "instance_retained_by_policy"
    skipped_reason = (
        "HC_CLEANUP_ON_QUOTA_EXCEEDED is false"
        if classification in QUOTA_CLEANUP_CLASSIFICATIONS and not cleanup_on_quota_exceeded_enabled()
        else "retain by default"
    )
    emit(
        f"{stage}:cleanup",
        "skipped",
        (
            f"Classification: {retained_classification}; os_label={label}; "
            f"{cleanup_policy_summary(classification=classification, retained_instance_ids=retained, skipped_delete_reason=skipped_reason)}"
        ),
        resources,
    )
    emit(
        INSTANCE_CLEANUP_STAGE,
        "skipped",
        (
            f"Classification: {retained_classification}; os_label={label}; "
            f"{cleanup_policy_summary(classification=classification, retained_instance_ids=retained, skipped_delete_reason=skipped_reason)}"
        ),
        resources,
    )


def wait_for_pending() -> None:
    deadline = time.time() + PENDING_TIMEOUT_SECONDS
    while pending_queue and time.time() < deadline:
        item = pending_queue.pop(0)
        workspace = Path(item.workspace)
        ready, reason, resources = readiness(workspace)
        if ready:
            emit(item.check, "ready", f"Pending task completed: {reason}", resources or item.resources)
            continue
        resources = resources or item.resources
        pending_queue.append(QueueItem(item.check, item.workspace, resources, reason, now()))
        emit(item.check, "waiting", f"Still pending: {reason}", resources)
        time.sleep(PENDING_POLL_SECONDS)

    while pending_queue:
        item = pending_queue.pop(0)
        queue_error(item.check, Path(item.workspace), item.resources, f"Pending timeout: {item.reason}")


def execute(check: Check) -> None:
    if check.name == "network.additional-subnet":
        execute_additional_subnet(check)
        return
    if check.name == INSTANCE_CREATE_STAGE:
        execute_instance_create(check)
        return

    resources = [f"module.{check.module}"]
    ok, reason = preflight(check)
    if not ok:
        emit(check.name, "skipped", reason, resources)
        return

    workspace = write_workspace(check)
    module_path = MODULES / check.module
    try:
        with ResourceLock(check.name):
            if "vpc_id" in check.vars:
                emit(
                    f"{check.name}:context",
                    "done",
                    f"Using effective_vpc_id={check.vars.get('vpc_id') or '<unset>'}; vpc_id_source={run_context.get('vpc_id_source', 'unresolved')}",
                    resources,
                )
            emit(check.name, "started", "Initializing Terraform workspace", resources)
            init = run(["terraform", "init", "-input=false", "-no-color"], workspace, stage=f"{check.name}-init")
            if init.returncode != 0:
                context = classify_context(
                    stage=check.name,
                    resource_type=f"module.{check.module}",
                    address="terraform.init",
                    module_path=module_path,
                    reason=(init.stderr or init.stdout)[-1200:],
                    vars=check.vars,
                )
                queue_error(check.name, workspace, resources, format_failure(context))
                return

            plan = run(
                ["terraform", "plan", "-out=tfplan", "-detailed-exitcode", "-no-color", "-input=false"],
                workspace,
                stage=f"{check.name}-plan",
            )
            planned = planned_resources(workspace)
            if plan.returncode not in (0, 2):
                context = classify_context(
                    stage=check.name,
                    resource_type=f"module.{check.module}",
                    address=", ".join(planned) or "terraform.plan",
                    module_path=module_path,
                    reason=(plan.stderr or plan.stdout)[-1200:],
                    vars=check.vars,
                )
                queue_error(check.name, workspace, planned or resources, format_failure(context))
                destroy(workspace, check.name)
                return
            emit(check.name, "pending", "Plan completed; resources will be created/updated", planned or resources)
            if check.module == "subnet":
                emit(
                    f"{check.name}:inputs",
                    "done",
                    (
                        "Subnet apply inputs: "
                        f"subnet_name={check.vars.get('name')}; "
                        f"subnet_cidr={check.vars.get('cidr')}; "
                        f"subnet_gateway={check.vars.get('gateway_ip')}; "
                        f"vpc_id={check.vars.get('vpc_id')}; "
                        f"vpc_identifier_type={vpc_identifier_type(str(check.vars.get('vpc_id') or ''))}; "
                        f"region={env('FPTCLOUD_REGION') or '<unset>'}; "
                        f"tenant={env('FPTCLOUD_TENANT_NAME') or '<unset>'}; "
                        f"terraform_vars={json.dumps(check.vars, sort_keys=True)}"
                    ),
                    ["module.this.fptcloud_subnet.this"],
                )

            attempts = check.retries + 1
            for attempt in range(1, attempts + 1):
                apply = run(
                    ["terraform", "apply", "-auto-approve", "-no-color", "-input=false", "tfplan"],
                    workspace,
                    stage=f"{check.name}-apply-attempt-{attempt}",
                )
                current = state_resources(workspace) or planned or resources
                if apply.returncode == 0:
                    emit(check.name, "passed", f"Apply succeeded on attempt {attempt}; settling briefly", current)
                    time.sleep(SETTLE_SECONDS)
                    ready, reason, current = readiness(workspace)
                    if ready:
                        emit(check.name, "ready", reason, current or planned or resources)
                    else:
                        queue_pending(check.name, workspace, current or planned or resources, reason)
                        wait_for_pending()
                    destroy(workspace, check.name)
                    return

                raw_reason = f"Apply attempt {attempt} failed: {(apply.stderr or apply.stdout)[-1000:]}"
                context = classify_context(
                    stage=check.name,
                    resource_type=f"module.{check.module}",
                    address=", ".join(current) or ", ".join(planned) or "terraform.apply",
                    module_path=module_path,
                    reason=raw_reason,
                    vars=check.vars,
                )
                reason = format_failure(context)
                if attempt < attempts:
                    emit(check.name, "retry", reason, current)
                    time.sleep(10)
                else:
                    queue_error(check.name, workspace, current, reason)
            destroy(workspace, check.name)
    except RuntimeError as exc:
        queue_error(check.name, workspace, resources, str(exc))


def _execute_one_image_attempt(
    check: Check,
    item: dict[str, Any],
    attempt: int,
    module_path: Path,
    provider: dict[str, Any],
    round_info: dict[str, Any],
    successful_images: list[str],
    instances: list[dict[str, Any]],
    index: int,
    created_instance_records: list[dict[str, str]],
) -> _ImageCreateResult:
    """One init/plan/apply attempt for a single image instance.

    Governed by specs/health-check.json INSTANCE_ERROR_QUEUE_RETRY_POLICY.
    Success side-effects (retain, created-instances.json, readiness) are handled internally.
    Quota failure: emits quota events, updates run_context, returns is_quota=True.
    Non-quota retryable failure: returns result without emitting check.name "failed" or
    calling queue_error — caller decides (enqueue for retry vs final fail).
    Resolve failure: emits check.name "failed" inline, returns retryable=False.
    """
    base_resources: list[str] = ["module.this.fptcloud_instance.this"]
    label = str(item.get("label") or "instance")
    diagnostics = dict(instance_validation.get("diagnostics") or {})
    diagnostics["os_label"] = label
    instance_vars = dict(item.get("vars") or {})
    hostname_entry = dict(item.get("hostname") or {})
    workspace_suffix = safe_name(label) if attempt == 1 else f"{safe_name(label)}-retry-{attempt}"

    resolved_vars, diagnostics, resolve_error = resolve_instance_image_flavor(instance_vars, diagnostics)
    if resolve_error:
        emit(check.name, "failed", f"os_label={label}; attempt={attempt}; {resolve_error}; Terraform apply not called", base_resources)
        return _ImageCreateResult(
            label=label, succeeded=False, is_quota=False, retryable=False,
            classification="instance_image_unresolved", error_code="", terraform_error=resolve_error,
            workspace=RUN_ROOT / check.name / workspace_suffix,
            resources=base_resources, context=None, failed_instance_id="",
        )

    matrix_check = Check(name=check.name, module=check.module, vars=resolved_vars, required_vars=check.required_vars)
    workspace = write_workspace_at(matrix_check, RUN_ROOT / check.name / workspace_suffix)
    emit(
        f"{check.name}:inputs",
        "done",
        (
            f"provider={provider.get('source')} {provider.get('version')}; os_label={label}; attempt={attempt}; "
            f"effective_vpc_id={matrix_check.vars.get('vpc_id') or '<unset>'}; "
            f"effective_subnet_id={matrix_check.vars.get('subnet_id') or '<unset>'}; "
            f"effective_storage_policy_id={matrix_check.vars.get('storage_policy_id') or '<unset>'}; "
            f"storage_policy_requested={diagnostics.get('storage_policy_requested') or preferred_instance_storage_policy_name()}; "
            f"selected_storage_policy_name={diagnostics.get('selected_storage_policy_name') or '<unresolved>'}; "
            f"selected_storage_policy_id={diagnostics.get('selected_storage_policy_id') or '<unresolved>'}; "
            f"selected_storage_policy_db_id={diagnostics.get('selected_storage_policy_db_id') or '<unresolved>'}; "
            f"provider_field_used={diagnostics.get('provider_field_used') or '<unset>'}; "
            f"storage_policy_quota_status={diagnostics.get('storage_policy_quota_status', 'not_available')}; "
            f"quota_precheck={diagnostics.get('quota_precheck', 'disabled')}; "
            f"quota_assumption={diagnostics.get('quota_assumption', 'assume_sufficient')}; "
            f"quota_exceeded_action={diagnostics.get('quota_exceeded_action', 'stop_and_wait_for_user')}; "
            f"quota_source={diagnostics.get('quota_source', 'unsupported_or_not_found')}; "
            f"quota_status={diagnostics.get('quota_status', 'not_available')}; "
            f"run_status={run_context.get('run_status', diagnostics.get('run_status', 'running'))}; "
            f"user_action_required={run_context.get('user_action_required', diagnostics.get('user_action_required', False))}; "
            f"remaining_storage_gb={diagnostics.get('remaining_storage_gb', 'not_available')}; "
            f"target_requested_disk_size_gb={diagnostics.get('target_requested_disk_size_gb', matrix_check.vars.get('disk_gb'))}; "
            f"image_used={matrix_check.vars.get('image_name') or '<unset>'}; image_source={diagnostics.get('image_source', 'unresolved')}; "
            f"flavor_used={matrix_check.vars.get('flavor_name') or '<unset>'}; flavor_source={diagnostics.get('flavor_source', 'unresolved')}; "
            f"subnet_source={diagnostics.get('subnet_source', 'unresolved')}; "
            f"storage_policy_source={diagnostics.get('storage_policy_source', 'unresolved')}; "
            f"resource_name={hostname_entry.get('resource_name') or item.get('resource_name') or '<unset>'}; "
            f"guest_hostname={hostname_entry.get('guest_hostname') or item.get('guest_hostname') or '<unset>'}; "
            f"selected_hostname={hostname_entry.get('selected_hostname') or matrix_check.vars.get('name') or '<unset>'}; "
            f"hostname_length={hostname_entry.get('hostname_length', len(str(matrix_check.vars.get('name') or '')))}; "
            f"hostname_validation_status={hostname_entry.get('hostname_validation_status', 'unresolved')}; "
            f"hostname_valid={hostname_entry.get('hostname_valid', False)}; "
            f"validation_errors={','.join(hostname_entry.get('validation_errors') or []) if hostname_entry else '<unresolved>'}; "
            f"disk_size={matrix_check.vars.get('disk_gb')}; disk_size_source={diagnostics.get('disk_size_source', root_disk_size_source())}; "
            f"reduced_disk_test={diagnostics.get('reduced_disk_test', False)}; instance_name={matrix_check.vars.get('name')}; "
            f"generated_suffix={INSTANCE_RUN_SUFFIX}; network_attachment_fields=vpc_id,subnet_id; "
            f"password_generated={bool(GENERATED_INSTANCE_PASSWORD)}; password_redacted=True; "
            f"public_ip_configured={bool(matrix_check.vars.get('public_ip') or env('HC_PUBLIC_IP'))}; "
            f"security_group_configured={bool(matrix_check.vars.get('security_group_ids') or env('HC_SECURITY_GROUP_ID'))}; "
            f"connection_test_policy=manual_verification_required; instance_group_relevance=not_required_for_password_or_network_attachment; "
            f"instances_per_apply={round_info.get('instances_per_apply', instances_per_apply())}; "
            f"stop_on_quota_exceeded={round_info.get('stop_on_quota_exceeded', stop_on_quota_exceeded_enabled())}; "
            f"selected_images={json.dumps(round_info.get('selected_images') or [i.get('label') for i in instances], sort_keys=True)}; "
            f"successful_images={json.dumps(successful_images, sort_keys=True)}; "
            f"failed_image={round_info.get('failed_image') or '<none>'}; "
            f"failure_reason={round_info.get('failure_reason') or '<none>'}; "
            f"remaining_images_not_attempted={json.dumps(round_info.get('remaining_images_not_attempted') or [], sort_keys=True)}; "
            f"unavailable_images={json.dumps(round_info.get('unavailable_images') or [], sort_keys=True)}; "
            f"apply_requested_instance_count={round_info.get('apply_requested_instance_count', 1)}; "
            f"apply_requested_storage_gb={round_info.get('apply_requested_storage_gb', matrix_check.vars.get('disk_gb'))}; "
            f"apply_requested_cpu={round_info.get('apply_requested_cpu', 2)}; "
            f"apply_requested_ram_mb={round_info.get('apply_requested_ram_mb', 2048)}; "
            f"{cleanup_policy_summary()}; "
            f"terraform_vars={json.dumps(redacted_vars(matrix_check.vars), sort_keys=True)}"
        ),
        base_resources,
    )
    emit(check.name, "started", f"os_label={label}; attempt={attempt}; Initializing Terraform workspace", base_resources)

    init = run_instance_terraform(
        ["terraform", "init", "-input=false", "-no-color"],
        workspace,
        stage=f"{check.name}-{label}-init-attempt-{attempt}",
    )
    if init.returncode != 0:
        context = classify_context(
            stage=check.name, resource_type="module.vm", address="terraform.init",
            module_path=module_path, reason=(init.stderr or init.stdout)[-1200:],
            vars=matrix_check.vars,
        )
        return _ImageCreateResult(
            label=label, succeeded=False, is_quota=False, retryable=True,
            classification=context.classification, error_code="",
            terraform_error=(init.stderr or init.stdout)[-1200:],
            workspace=workspace, resources=base_resources, context=context, failed_instance_id="",
        )

    plan = run_instance_terraform(
        ["terraform", "plan", "-out=tfplan", "-detailed-exitcode", "-no-color", "-input=false"],
        workspace,
        stage=f"{check.name}-{label}-plan-attempt-{attempt}",
    )
    planned = planned_resources(workspace)
    if plan.returncode not in (0, 2):
        context = classify_context(
            stage=check.name, resource_type="module.vm",
            address=", ".join(planned) or "terraform.plan",
            module_path=module_path, reason=(plan.stderr or plan.stdout)[-1200:],
            vars=matrix_check.vars,
        )
        return _ImageCreateResult(
            label=label, succeeded=False, is_quota=False, retryable=True,
            classification=context.classification, error_code="",
            terraform_error=(plan.stderr or plan.stdout)[-1200:],
            workspace=workspace, resources=planned or base_resources, context=context, failed_instance_id="",
        )
    emit(check.name, "pending", f"os_label={label}; attempt={attempt}; Plan completed; instance will be created", planned or base_resources)

    apply = run_instance_terraform(
        ["terraform", "apply", "-auto-approve", "-no-color", "-input=false", "tfplan"],
        workspace,
        stage=f"{check.name}-{label}-apply-attempt-{attempt}",
    )
    current = state_resources(workspace) or planned or base_resources

    if apply.returncode == 0:
        created_id = instance_id_from_state(workspace)
        successful_images.append(label)
        if created_id:
            created_instance_records.append({"os_label": label, "instance_id": created_id})
            ids_path = RUN_ROOT / check.name / "created-instances.json"
            ids_path.parent.mkdir(parents=True, exist_ok=True)
            ids_path.write_text(json.dumps(created_instance_records, indent=2, sort_keys=True), encoding="utf-8")
        round_info["successful_images"] = successful_images
        run_context["selected_instance_round"] = round_info
        emit(
            check.name, "passed",
            (
                f"os_label={label}; attempt={attempt}; per_instance_create_result=created; "
                f"successful_images={json.dumps(successful_images)}; instance_id={created_id or '<unknown>'}; "
                f"persisted_instance_ids={json.dumps(created_instance_records, sort_keys=True)}; "
                f"instance_status=created; settling briefly"
            ),
            current,
        )
        time.sleep(SETTLE_SECONDS)
        ready, ready_reason, current = readiness(workspace)
        if ready:
            emit(check.name, "ready", f"os_label={label}; attempt={attempt}; instance_id={created_id or '<unknown>'}; instance_status=ready; {ready_reason}", current or planned or base_resources)
        else:
            queue_pending(check.name, workspace, current or planned or base_resources, f"os_label={label}; attempt={attempt}; {ready_reason}")
            wait_for_pending()
        retain_instance(
            check.name, label=label, instance_id=created_id,
            classification="", failed=False, resources=current or planned or base_resources,
        )
        return _ImageCreateResult(
            label=label, succeeded=True, is_quota=False, retryable=False,
            classification="", error_code="", terraform_error="",
            workspace=workspace, resources=list(current or planned or base_resources),
            context=None, failed_instance_id="",
        )

    # Apply failed — classify and retain.
    raw_apply = (apply.stderr or apply.stdout)[-1000:]
    classification = classify_error(raw_apply, "module.vm")
    context = classify_context(
        stage=check.name, resource_type="module.vm",
        address=", ".join(current) or "terraform.apply",
        module_path=module_path,
        reason=f"Classification: {classification}; Apply failed: {raw_apply}",
        vars=matrix_check.vars,
    )
    failed_instance_id = instance_id_from_state(workspace)
    retain_instance(
        check.name, label=label, instance_id=failed_instance_id,
        classification=classification, failed=True, resources=current,
    )
    # Only update remaining_images_not_attempted on the first pass (attempt 1);
    # retries don't shift what is "remaining" since all images were already attempted.
    remaining = [str(next_item.get("label") or "") for next_item in instances[index + 1:]] if attempt == 1 else []
    round_info.update({
        "successful_images": successful_images,
        "failed_image": label,
        "failure_reason": classification,
        "remaining_images_not_attempted": remaining,
        "stop_on_quota_exceeded": True,
    })
    run_context["selected_instance_round"] = round_info

    if is_quota_error(classification):
        run_context.update({
            "run_status": "blocked_waiting_user_confirmation",
            "run_blocked": True,
            "user_action_required": True,
            "failed_image": label,
            "failure_reason": classification,
            "remaining_images_not_attempted": remaining,
            "stop_on_quota_exceeded": True,
        })
        emit(
            "compute.detect-quota-exceeded",
            "blocked",
            (
                f"quota.exceeded; os_label={label}; attempt={attempt}; classification={classification}; "
                f"quota_precheck=disabled; quota_assumption=assume_sufficient; "
                f"quota_exceeded_action=stop_and_wait_for_user; stop_on_quota_exceeded=True; "
                f"run_status=blocked_waiting_user_confirmation; "
                f"remaining_images_not_attempted={json.dumps(remaining)}; "
                f"user_action_required=True; cleanup_attempted=False; reclaim_attempted=False; "
                f"recover_attempted=False; recreate_attempted=False; retry_attempted=False"
            ),
            current,
        )
        emit(
            check.name,
            "failed",
            (
                f"os_label={label}; attempt={attempt}; per_instance_create_result=failed; "
                f"successful_images={json.dumps(successful_images)}; failed_image={label}; "
                f"failure_reason={classification}; remaining_images_not_attempted={json.dumps(remaining)}; "
                f"quota_precheck=disabled; quota_assumption=assume_sufficient; "
                f"quota_exceeded_action=stop_and_wait_for_user; stop_on_quota_exceeded=True; "
                f"run_status=blocked_waiting_user_confirmation; user_action_required=True; "
                f"retained_or_deleted_result=retained_by_policy; cleanup_attempted=False; "
                f"reclaim_attempted=False; recover_attempted=False; recreate_attempted=False; retry_attempted=False"
            ),
            current,
        )
        return _ImageCreateResult(
            label=label, succeeded=False, is_quota=True, retryable=False,
            classification=classification, error_code="", terraform_error=raw_apply,
            workspace=workspace, resources=list(current), context=context,
            failed_instance_id=failed_instance_id,
        )

    # Non-quota apply failure: return without emitting check.name "failed".
    # Caller emits instance.create_failed_queued (first pass) or instance.retry_* (retry phase).
    return _ImageCreateResult(
        label=label, succeeded=False, is_quota=False, retryable=True,
        classification=classification, error_code="", terraform_error=raw_apply,
        workspace=workspace, resources=list(current), context=context,
        failed_instance_id=failed_instance_id,
    )


def execute_instance_create(check: Check) -> None:
    # Governed by specs/health-check.json INSTANCE_ERROR_QUEUE_RETRY_POLICY and compute.create-instance.
    resources = ["module.this.fptcloud_instance.this"]
    if not instance_validation.get("valid"):
        emit(check.name, "skipped", "Classification: instance_missing_required_inputs; compute.validate-instance-inputs did not pass; Terraform apply not called", resources)
        return
    ok, reason = preflight(check)
    if not ok:
        emit(check.name, "skipped", f"Classification: instance_missing_required_inputs; {reason}; Terraform apply not called", resources)
        return
    module_path = MODULES / check.module
    provider = input_diagnostics()["provider"]

    # In-memory error queue for non-quota instance creation failures (per INSTANCE_ERROR_QUEUE_RETRY_POLICY).
    instance_error_queue: list[dict[str, Any]] = []

    try:
        with ResourceLock(check.name):
            instances = selected_round_instances()
            round_info = dict(run_context.get("selected_instance_round") or {})
            successful_images: list[str] = list(round_info.get("successful_images") or [])
            created_instance_records: list[dict[str, str]] = []
            if not instances:
                emit(check.name, "skipped", "Classification: instance_image_unresolved; selected_round_images is empty; Terraform apply not called", resources)
                return

            # ── First pass ────────────────────────────────────────────────────────────
            # Attempt each image in spec order.  Non-quota failures are enqueued for
            # retry and the loop continues to the next image (INSTANCE_ERROR_QUEUE_RETRY_POLICY).
            for index, item in enumerate(instances):
                label = str(item.get("label") or "instance")
                result = _execute_one_image_attempt(
                    check, item, 1, module_path, provider,
                    round_info, successful_images, instances, index,
                    created_instance_records,
                )
                if result.succeeded:
                    pass  # success side-effects handled inside helper
                elif result.is_quota:
                    return  # run_context already updated; stop entire workflow
                elif not result.retryable:
                    pass  # resolve error: check.name "failed" already emitted; continue to next image
                else:
                    remaining = [str(inst.get("label") or "") for inst in instances[index + 1:]]
                    image_name = str(item.get("vars", {}).get("image_name") or "")
                    instance_error_queue.append({
                        "item": item,
                        "index": index,
                        "label": label,
                        "image_name": image_name,
                        "classification": result.classification,
                        "error_code": result.error_code,
                        "terraform_error": result.terraform_error,
                        "context": result.context,
                        "workspace": result.workspace,
                        "resources": result.resources,
                    })
                    emit(
                        "instance.create_failed_queued",
                        "queued",
                        (
                            f"image_label={label}; image_name={image_name}; "
                            f"attempt=1; max_attempts={MAX_INSTANCE_CREATE_ATTEMPTS}; "
                            f"classification={result.classification}; "
                            f"error_code={result.error_code or 'n/a'}; "
                            f"terraform_error={result.terraform_error[:500]}; "
                            f"queued=True; "
                            f"remaining_retry_attempts={MAX_INSTANCE_CREATE_ATTEMPTS - 1}; "
                            f"remaining_images_not_attempted={json.dumps(remaining)}"
                        ),
                        result.resources,
                    )

            # ── Retry phase ───────────────────────────────────────────────────────────
            # Process all queued non-quota failures after the full first pass.
            # Each case gets up to MAX_INSTANCE_CREATE_ATTEMPTS total (attempt 1 = first pass).
            initial_queued_count = len(instance_error_queue)
            retry_succeeded_count = 0
            retry_exhausted_count = 0

            for queued in list(instance_error_queue):
                label = queued["label"]
                item = queued["item"]
                index = queued["index"]
                image_name = queued["image_name"]

                for attempt in range(2, MAX_INSTANCE_CREATE_ATTEMPTS + 1):
                    remaining_retries = MAX_INSTANCE_CREATE_ATTEMPTS - attempt
                    queue_pos = instance_error_queue.index(queued) + 1 if queued in instance_error_queue else 0
                    emit(
                        "instance.retry_started",
                        "started",
                        (
                            f"image_label={label}; image_name={image_name}; "
                            f"attempt={attempt}; max_attempts={MAX_INSTANCE_CREATE_ATTEMPTS}; "
                            f"queue_position={queue_pos}"
                        ),
                        queued["resources"],
                    )
                    result = _execute_one_image_attempt(
                        check, item, attempt, module_path, provider,
                        round_info, successful_images, instances, index,
                        created_instance_records,
                    )
                    if result.succeeded:
                        instance_error_queue.remove(queued)
                        retry_succeeded_count += 1
                        emit(
                            "instance.retry_succeeded",
                            "passed",
                            (
                                f"image_label={label}; image_name={image_name}; "
                                f"attempt={attempt}; max_attempts={MAX_INSTANCE_CREATE_ATTEMPTS}; "
                                f"removed_from_queue=True; instance_retained=True"
                            ),
                            result.resources,
                        )
                        break
                    elif result.is_quota:
                        return  # quota during retry: stop entire workflow
                    elif remaining_retries > 0:
                        emit(
                            "instance.retry_failed",
                            "failed",
                            (
                                f"image_label={label}; image_name={image_name}; "
                                f"attempt={attempt}; max_attempts={MAX_INSTANCE_CREATE_ATTEMPTS}; "
                                f"classification={result.classification}; "
                                f"error_code={result.error_code or 'n/a'}; "
                                f"terraform_error={result.terraform_error[:500]}; "
                                f"remaining_retry_attempts={remaining_retries}"
                            ),
                            result.resources,
                        )
                    else:
                        # All MAX_INSTANCE_CREATE_ATTEMPTS exhausted for this image.
                        retry_exhausted_count += 1
                        emit(
                            "instance.retry_exhausted",
                            "failed",
                            (
                                f"image_label={label}; image_name={image_name}; "
                                f"attempt={attempt}; max_attempts={MAX_INSTANCE_CREATE_ATTEMPTS}; "
                                f"classification={result.classification}; "
                                f"error_code={result.error_code or 'n/a'}; "
                                f"terraform_error={result.terraform_error[:500]}; "
                                f"final_failure=True; user_action_required=False"
                            ),
                            result.resources,
                        )
                        failure_reason = format_failure(result.context) if result.context else result.terraform_error
                        queue_error(check.name, result.workspace, result.resources, failure_reason)

            # ── Retry-phase summary ───────────────────────────────────────────────────
            if initial_queued_count > 0:
                remaining_failed = [q["label"] for q in instance_error_queue]
                emit(
                    f"{check.name}:retry-summary",
                    "done",
                    (
                        f"queued_count={initial_queued_count}; "
                        f"retry_succeeded_count={retry_succeeded_count}; "
                        f"retry_exhausted_count={retry_exhausted_count}; "
                        f"remaining_error_count={len(instance_error_queue)}; "
                        f"remaining_failed_images={json.dumps(remaining_failed)}"
                    ),
                    resources,
                )
    except RuntimeError as exc:
        queue_error(check.name, RUN_ROOT / check.name, resources, str(exc))


def execute_additional_subnet(check: Check) -> None:
    resources = [f"module.{check.module}"]
    ok, reason = preflight(check)
    if not ok:
        emit(check.name, "skipped", reason, resources)
        return

    module_path = MODULES / check.module
    candidate_state = CandidateState(
        str(check.vars.get("cidr") or ""),
        str(check.vars.get("gateway_ip") or ""),
        max_subnet_candidate_attempts(),
    )
    current_vars = dict(check.vars)
    workspace = RUN_ROOT / check.name
    try:
        with ResourceLock(check.name):
            while True:
                current_vars, selection_error, candidate_state = select_additional_subnet_vars(current_vars, candidate_state)
                if selection_error:
                    emit(check.name, "skipped", selection_error, ["subnet-candidate-selection"])
                    return

                attempt_number = len(candidate_state.rejected_cidrs) + 1
                emit(
                    f"{check.name}:attempt",
                    "started",
                    (
                        f"attempt={attempt_number}; candidate_cidr={current_vars.get('cidr')}; "
                        f"gateway={current_vars.get('gateway_ip')}; rejected_cidrs=[{', '.join(candidate_state.rejected_cidrs)}]"
                    ),
                    resources,
                )

                attempt_check = Check(
                    name=check.name,
                    module=check.module,
                    vars=current_vars,
                    required_vars=check.required_vars,
                    retries=0,
                )
                workspace = write_workspace(attempt_check)
                emit(
                    f"{check.name}:context",
                    "done",
                    f"Using effective_vpc_id={current_vars.get('vpc_id') or '<unset>'}; vpc_id_source={run_context.get('vpc_id_source', 'unresolved')}",
                    resources,
                )
                if (workspace / ".terraform").exists():
                    emit(check.name, "started", "Reusing initialized Terraform workspace", resources)
                else:
                    emit(check.name, "started", "Initializing Terraform workspace", resources)
                    init = run(["terraform", "init", "-input=false", "-no-color"], workspace, stage=f"{check.name}-init")
                    if init.returncode != 0:
                        context = classify_context(
                            stage=check.name,
                            resource_type=f"module.{check.module}",
                            address="terraform.init",
                            module_path=module_path,
                            reason=(init.stderr or init.stdout)[-1200:],
                            vars=current_vars,
                        )
                        queue_error(check.name, workspace, resources, format_failure(context))
                        return

                plan = run(
                    ["terraform", "plan", "-out=tfplan", "-detailed-exitcode", "-no-color", "-input=false"],
                    workspace,
                    stage=f"{check.name}-plan",
                )
                planned = planned_resources(workspace)
                if plan.returncode not in (0, 2):
                    context = classify_context(
                        stage=check.name,
                        resource_type=f"module.{check.module}",
                        address=", ".join(planned) or "terraform.plan",
                        module_path=module_path,
                        reason=(plan.stderr or plan.stdout)[-1200:],
                        vars=current_vars,
                    )
                    queue_error(check.name, workspace, planned or resources, format_failure(context))
                    destroy(workspace, check.name)
                    return
                emit(check.name, "pending", "Plan completed; resources will be created/updated", planned or resources)
                emit(
                    f"{check.name}:inputs",
                    "done",
                    (
                        "Subnet apply inputs: "
                        f"subnet_name={current_vars.get('name')}; "
                        f"subnet_cidr={current_vars.get('cidr')}; "
                        f"subnet_gateway={current_vars.get('gateway_ip')}; "
                        f"vpc_id={current_vars.get('vpc_id')}; "
                        f"vpc_identifier_type={vpc_identifier_type(str(current_vars.get('vpc_id') or ''))}; "
                        f"region={env('FPTCLOUD_REGION') or '<unset>'}; "
                        f"tenant={env('FPTCLOUD_TENANT_NAME') or '<unset>'}; "
                        f"terraform_vars={json.dumps(current_vars, sort_keys=True)}"
                    ),
                    ["module.this.fptcloud_subnet.this"],
                )

                apply = run(
                    ["terraform", "apply", "-auto-approve", "-no-color", "-input=false", "tfplan"],
                    workspace,
                    stage=f"{check.name}-apply-attempt-{attempt_number}",
                )
                current = state_resources(workspace) or planned or resources
                if apply.returncode == 0:
                    emit(check.name, "passed", f"Apply succeeded on candidate attempt {attempt_number}; settling briefly", current)
                    time.sleep(SETTLE_SECONDS)
                    ready, ready_reason, current = readiness(workspace)
                    if ready:
                        emit(check.name, "ready", ready_reason, current or planned or resources)
                    else:
                        queue_pending(check.name, workspace, current or planned or resources, ready_reason)
                        wait_for_pending()
                    destroy(workspace, check.name)
                    return

                raw_reason = f"Apply attempt {attempt_number} failed: {(apply.stderr or apply.stdout)[-1000:]}"
                context = classify_context(
                    stage=check.name,
                    resource_type=f"module.{check.module}",
                    address=", ".join(current) or ", ".join(planned) or "terraform.apply",
                    module_path=module_path,
                    reason=raw_reason,
                    vars=current_vars,
                )
                reason = format_failure(context)
                if context.classification != "subnet_cidr_overlap":
                    queue_error(check.name, workspace, current, reason)
                    destroy(workspace, check.name)
                    return

                emit(
                    f"{check.name}:overlap-detected",
                    "retry",
                    (
                        f"attempt={attempt_number}; candidate_cidr={current_vars.get('cidr')}; "
                        f"gateway={current_vars.get('gateway_ip')}; conflict_source=provider_error; "
                        f"conflicting_subnet={context.conflicting_subnet or '<unknown>'}; classification=subnet_cidr_overlap"
                    ),
                    current,
                )
                destroy(workspace, check.name)
                candidate_state = append_provider_overlap(candidate_state, str(current_vars.get("cidr") or ""), context.conflicting_subnet)
                if len(candidate_state.rejected_cidrs) >= candidate_state.max_attempts:
                    emit(
                        check.name,
                        "failed",
                        (
                            "Classification: subnet_cidr_exhausted; "
                            f"candidate_attempt_count={len(candidate_state.rejected_cidrs)}; "
                            f"rejected_cidrs=[{', '.join(candidate_state.rejected_cidrs)}]; "
                            "max attempts reached after provider overlap feedback"
                        ),
                        ["subnet-candidate-selection"],
                    )
                    return
    except RuntimeError as exc:
        queue_error(check.name, workspace, resources, str(exc))


def check_from_spec(
    stage: StageSpec,
    *,
    module: str,
    vars: dict[str, Any],
    required_vars: tuple[str, ...] = (),
    retries: int = 0,
    stop_group_on_success: str | None = None,
) -> Check | None:
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, [f"spec.{stage.automation_status}"])
        return None
    return Check(
        name=stage.id,
        module=module,
        vars=vars,
        required_vars=required_vars,
        retries=retries,
        stop_group_on_success=stop_group_on_success,
    )


def select_stages(spec: dict[str, StageSpec], stage_filter: str | None) -> dict[str, StageSpec]:
    if not stage_filter:
        return spec
    if stage_filter not in spec:
        emit("spec", "failed", f"Stage {stage_filter} is not defined in specs/health-check.json")
        return {}
    selected: dict[str, StageSpec] = {}

    def add_with_dependencies(stage_id: str) -> None:
        stage = spec[stage_id]
        for dependency in stage.dependency_stages:
            if dependency in spec:
                add_with_dependencies(dependency)
        selected[stage_id] = stage

    add_with_dependencies(stage_filter)
    return selected


def checks(stage_filter: str | None = None) -> list[Check]:
    suffix = RUN_ID.lower().replace("_", "-")
    spec = select_stages(load_spec(), stage_filter)
    config = effective_config()
    discover_vpc(spec.get(VPC_DISCOVERY_STAGE))
    vpc_id = update_vpc_context()["effective_vpc_id"]
    storage_policy = config["storage_policy_id"]
    subnet_id = config["subnet_id"]
    run_context["effective_storage_policy_id"] = storage_policy
    run_context["storage_policy_id_source"] = "explicit_id" if storage_policy else "unresolved"
    run_context["effective_subnet_id"] = subnet_id
    run_context["subnet_id_source"] = "explicit_id" if subnet_id else "unresolved"
    create_subnet_vars = subnet_vars(suffix)

    validate_stage = spec.get(SUBNET_VALIDATION_STAGE)
    if validate_stage:
        validate_subnet_stage(validate_stage, create_subnet_vars)

    storage_policy = discover_storage_policy_stage(spec.get("compute.discover-storage-policy"), storage_policy, vpc_id)
    storage_policy = validate_instance_storage_policy_stage(spec.get(INSTANCE_STORAGE_POLICY_VALIDATE_STAGE), storage_policy)

    subnet_stage = spec.get("compute.discover-subnet")
    if subnet_stage and subnet_id:
        ok, reason = runnable_spec(subnet_stage)
        emit(
            subnet_stage.id,
            "done" if ok else "skipped",
            "Using explicit HC_SUBNET_ID; Terraform data-source lookup skipped" if ok else reason,
            ["HC_SUBNET_ID"],
        )
    elif subnet_stage:
        ok, reason = spec_preflight(subnet_stage)
        if not ok:
            emit(subnet_stage.id, "skipped", reason, ["data.fptcloud_subnet.this"])
        elif not vpc_id:
            emit(subnet_stage.id, "skipped", f"No VPC ID can be resolved for provider discovery; {vpc_diagnostics_message()}", ["data.fptcloud_subnet.this"])
        else:
            emit(subnet_stage.id, "started", f"Using effective_vpc_id={vpc_id} for subnet discovery", ["data.fptcloud_subnet.this"])
            subnet_id = discover_value(
                "subnet",
                "fptcloud_subnet",
                'try(data.fptcloud_subnet.this.subnets[0].id, "")',
                vpc_id,
                subnet_stage.id,
            )
            if subnet_id:
                stage_status[subnet_stage.id] = "done"
                run_context["effective_subnet_id"] = subnet_id
                run_context["subnet_id_source"] = "discovered"

    discover_instance_images(spec.get(INSTANCE_IMAGE_DISCOVERY_STAGE))
    discover_instance_flavor(spec.get(INSTANCE_FLAVOR_DISCOVERY_STAGE))
    discover_existing_subnets(spec.get(EXISTING_SUBNETS_STAGE))
    validate_instance_password_policy_stage(spec.get(INSTANCE_PASSWORD_POLICY_STAGE))
    validate_instance_hostname_stage(spec.get(INSTANCE_HOSTNAME_VALIDATE_STAGE), suffix)
    validate_instance_stage(spec.get(INSTANCE_VALIDATE_STAGE), suffix, subnet_id, storage_policy)
    select_instance_round_stage(spec.get(INSTANCE_ROUND_SELECT_STAGE))
    validate_instance_network_stage(spec.get(INSTANCE_NETWORK_VALIDATE_STAGE))

    instance_id = env("HC_INSTANCE_ID")
    backup_regions = [
        value.strip()
        for value in env("HC_ENABLED_OBJECT_REGIONS", env("HC_OBJECT_REGION", "")).split(",")
        if value.strip()
    ]
    additional_subnet_vars = {
        "name": f"hc-extra-net-{suffix}",
        "cidr": env("HC_ADDITIONAL_SUBNET_CIDR"),
        "gateway_ip": env("HC_ADDITIONAL_SUBNET_GATEWAY"),
        "type": env("HC_SUBNET_TYPE", "NAT_ROUTED"),
        "vpc_id": vpc_id,
    }
    additional_subnet_check: Check | None = None
    if "network.additional-subnet" in spec:
        additional_subnet_check = check_from_spec(
            spec["network.additional-subnet"],
            module="subnet",
            vars=additional_subnet_vars,
            required_vars=("vpc_id", "cidr", "gateway_ip"),
        )

    candidate_checks: list[Check | None] = [
        check_from_spec(
            spec[INSTANCE_CREATE_STAGE],
            module="vm",
            vars=dict(instance_validation.get("vars") or {}),
            required_vars=("instances",),
        )
        if INSTANCE_CREATE_STAGE in spec
        else None,
        check_from_spec(
            spec[SUBNET_CREATE_STAGE],
            module="subnet",
            vars=create_subnet_vars,
            required_vars=("vpc_id",),
        )
        if SUBNET_CREATE_STAGE in spec
        else None,
        check_from_spec(
            spec["network.security-group"],
            module="security_group",
            vars={
                "name": f"hc-sg-{suffix}",
                "vpc_id": vpc_id,
                "type": "ACL",
                "apply_to": [subnet_id] if subnet_id else [],
                "rules": [
                    {
                        "direction": "INGRESS",
                        "protocol": "TCP",
                        "port_range": "22",
                        "sources": [env("HC_ADMIN_CIDR", "0.0.0.0/0")],
                        "action": "ALLOW",
                        "description": "health check ssh",
                    },
                    {
                        "direction": "INGRESS",
                        "protocol": "TCP",
                        "port_range": "3389",
                        "sources": [env("HC_ADMIN_CIDR", "0.0.0.0/0")],
                        "action": "ALLOW",
                        "description": "health check rdp",
                    }
                ],
            },
            required_vars=("vpc_id", "apply_to"),
        )
        if "network.security-group" in spec
        else None,
        check_from_spec(
            spec["compute.add-disk"],
            module="disk",
            vars={
                "name": f"hc-disk-{suffix}",
                "size_gb": int(env("HC_DISK_SIZE_GB", "40")),
                "vpc_id": vpc_id,
                "storage_policy_id": storage_policy,
                "type": env("HC_DISK_TYPE", "EXTERNAL"),
                "instance_id": instance_id or None,
            },
            required_vars=("vpc_id", "storage_policy_id"),
        )
        if "compute.add-disk" in spec
        else None,
        additional_subnet_check,
    ]
    base_checks = [check for check in candidate_checks if check]

    if not backup_regions:
        backup_stage = spec.get("object-storage.bucket")
        backup_checks = []
        if backup_stage:
            check = check_from_spec(
                backup_stage,
                module="object_storage",
                vars={"region_name": "", "vpc_id": vpc_id},
                required_vars=("vpc_id", "region_name"),
            )
            if check:
                backup_checks.append(check)
    else:
        backup_checks = [
            check
            for region in backup_regions
            for check in [
                check_from_spec(
                    spec["object-storage.bucket"],
                module="object_storage",
                vars={
                    "bucket_name": f"hc-backup-{region.lower().replace('-', '')}-{suffix}",
                    "region_name": region,
                    "vpc_id": vpc_id,
                    "acl": None,
                    "versioning": "Enabled",
                },
                required_vars=("vpc_id",),
                stop_group_on_success="backup",
                )
            ]
            if "object-storage.bucket" in spec and check
        ]
    return base_checks + backup_checks


def main() -> None:
    parser = argparse.ArgumentParser(description="Run spec-gated FPT Cloud health checks.")
    parser.add_argument("--stage", help="Run one stage ID from specs/health-check.json.")
    parser.add_argument("--view", metavar="LOG_JSON", help="Path to a log.json file to render as a filtered table.")
    parser.add_argument("--filter", dest="filter_mode", choices=FILTER_CHOICES, default="summary", help="Filter mode for --view (default: summary).")
    args = parser.parse_args()

    if args.view:
        log_data = json.loads(Path(args.view).read_text(encoding="utf-8-sig"))
        print(render_table(log_data, args.filter_mode))
        return

    if LOG_PATH.exists():
        LOG_PATH.unlink()
    if JSON_LOG_PATH.exists():
        JSON_LOG_PATH.unlink()
    if RUN_ROOT.exists():
        shutil.rmtree(RUN_ROOT)
    run_context.update(
        {
            "run_status": "running",
            "run_blocked": False,
            "user_action_required": False,
            "remaining_images_not_attempted": [],
        }
    )
    emit("run", "started", f"Health-check run {RUN_ID} started")
    emit(
        "environment",
        "done",
        (
            f"cwd={DOTENV_RESULT.get('cwd', str(Path.cwd()))}; "
            f"dotenv_path={DOTENV_RESULT.get('path', str(ROOT / '.env'))}; "
            f"dotenv_found={DOTENV_RESULT['found']}; "
            f"dotenv_loaded={bool(DOTENV_RESULT['loaded'])}; "
            f"dotenv_loaded_count={len(DOTENV_RESULT['loaded'])}; "
            f"dotenv_skipped_existing_count={len(DOTENV_RESULT['skipped_existing'])}; "
            f"{env_presence_report()}"
        ),
    )
    diagnostic_data = input_diagnostics()
    (RUN_ROOT / "input_diagnostics.json").write_text(
        json.dumps(diagnostic_data, indent=2),
        encoding="utf-8",
    )
    provider = diagnostic_data["provider"]
    context = diagnostic_data["provider_config"]
    network_vars = diagnostic_data["stage_inputs"]["network"]["vars"]
    config = effective_config()
    update_vpc_context()
    emit(
        "diagnostics",
        "done",
        (
            f"Provider {provider.get('source')} {provider.get('version')} "
            f"from {provider.get('lock_file')}; "
            f"region={context.get('region')}; region_id={context.get('region_id') or '<unset>'}; "
            f"tenant={context.get('tenant_name')}; tenant_id={context.get('tenant_id') or '<unset>'}; "
            f"vpc_name={run_context.get('vpc_name') or '<unset>'}; "
            f"explicit_vpc_id={run_context.get('explicit_vpc_id') or '<unset>'}; "
            f"discovered_vpc_id={run_context.get('discovered_vpc_id') or '<unset>'}; "
            f"effective_vpc_id={run_context.get('effective_vpc_id') or '<unset>'}; "
            f"vpc_id_source={run_context.get('vpc_id_source') or 'unresolved'}; "
            f"subnet_name={network_vars.get('name')}; cidr={network_vars.get('cidr')}; "
            f"storage_policy_lookup_vpc={run_context.get('effective_vpc_id') or '<unresolved>'}; "
            f"subnet_id={config.get('subnet_id') or '<unset>'}; "
            f"storage_policy_id={config.get('storage_policy_id') or '<unset>'}"
        ),
    )
    completed_groups: set[str] = set()
    blocked = False
    for check in checks(args.stage):
        if check.stop_group_on_success and check.stop_group_on_success in completed_groups:
            emit(
                check.name,
                "skipped",
                f"Skipped because {check.stop_group_on_success} already passed",
                [f"module.{check.module}"],
            )
            continue
        before = len(events)
        execute(check)
        if run_context.get("run_blocked"):
            blocked = True
            emit(
                "run",
                "blocked",
                (
                    f"Health-check run {RUN_ID} blocked; "
                    f"run_status=blocked_waiting_user_confirmation; "
                    f"quota_precheck=disabled; quota_assumption=assume_sufficient; "
                    f"quota_exceeded_action=stop_and_wait_for_user; "
                    f"stop_on_quota_exceeded=True; "
                    f"remaining_images_not_attempted={json.dumps(run_context.get('remaining_images_not_attempted') or [])}; "
                    f"user_action_required=True"
                ),
            )
            break
        if check.stop_group_on_success:
            new_events = events[before:]
            if any(event["stage"] == check.name and event["status"] == "passed" for event in new_events):
                completed_groups.add(check.stop_group_on_success)
    if not blocked:
        wait_for_pending()
        emit("run", "done", f"Health-check run {RUN_ID} finished")


if __name__ == "__main__":
    main()
