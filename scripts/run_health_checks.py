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
import zipfile
from dataclasses import asdict, dataclass
from html import escape
from pathlib import Path
from string import Template
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagnose_health_inputs import diagnostics as input_diagnostics, effective_config, looks_uuid  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = time.strftime("hc-%Y%m%d-%H%M%S")
RUN_ROOT = ROOT / "runs" / RUN_ID
LOG_PATH = ROOT / "log.html"
MODULES = ROOT / "modules"
TEMPLATE = ROOT / "src" / "hc" / "reporter" / "html_log.html"
LOCK_ROOT = ROOT / "runs" / ".locks"
SPEC_PATH = ROOT / "specs" / "health-check.json"

SETTLE_SECONDS = int(os.environ.get("HC_SETTLE_SECONDS", "20"))
PENDING_POLL_SECONDS = int(os.environ.get("HC_PENDING_POLL_SECONDS", "15"))
PENDING_TIMEOUT_SECONDS = int(os.environ.get("HC_PENDING_TIMEOUT_SECONDS", "300"))

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


events: list[dict[str, str]] = []
pending_queue: list[QueueItem] = []
error_queue: list[QueueItem] = []
stage_status: dict[str, str] = {}
run_context: dict[str, str] = {
    "vpc_name": "",
    "explicit_vpc_id": "",
    "discovered_vpc_id": "",
    "effective_vpc_id": "",
    "vpc_id_source": "unresolved",
}
existing_subnet_inventory: list[dict[str, str]] = []


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value.lower())


def status_class(status: str) -> str:
    normalized = status.lower()
    if normalized in {"destroyed", "done", "locked", "ok", "passed", "ready", "unlocked"}:
        return "ok"
    if normalized in {"pending", "queued", "retry", "skipped", "waiting"}:
        return "warn"
    if normalized in {"error", "failed"}:
        return "error"
    return "info"


def emit(stage: str, status: str, message: str, resources: list[str] | None = None) -> None:
    detail = message
    if resources:
        detail = f"{message} Resources: {', '.join(resources)}"
    events.append({"time": now(), "stage": stage, "status": status, "message": detail})
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
    if stage.required_cloud_resources and "destroy" not in cleanup and "no resources" not in cleanup:
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
    write_queues()
    rows = "\n".join(
        "<tr>"
        f"<td>{escape(event['time'])}</td>"
        f"<td>{escape(event['stage'])}</td>"
        f"<td><span class=\"badge {status_class(event['status'])}\">{escape(event['status'])}</span></td>"
        f"<td>{escape(event['message'])}</td>"
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
) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ)
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
    (log_dir / f"{base}.stdout.log").write_text(result.stdout or "", encoding="utf-8")
    (log_dir / f"{base}.stderr.log").write_text(result.stderr or "", encoding="utf-8")
    return result


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


def spec_preflight(stage: StageSpec) -> tuple[bool, str]:
    ok, reason = runnable_spec(stage)
    if not ok:
        return False, reason
    missing = [name for name in stage.required_inputs if not env(name)]
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
    create_subnet_vars = subnet_vars(suffix)

    validate_stage = spec.get(SUBNET_VALIDATION_STAGE)
    if validate_stage:
        validate_subnet_stage(validate_stage, create_subnet_vars)

    storage_stage = spec.get("compute.discover-storage-policy")
    if storage_stage and storage_policy:
        ok, reason = runnable_spec(storage_stage)
        emit(
            storage_stage.id,
            "done" if ok else "skipped",
            "Using explicit HC_STORAGE_POLICY_ID; Terraform data-source lookup skipped" if ok else reason,
            ["HC_STORAGE_POLICY_ID"],
        )
    elif storage_stage:
        ok, reason = spec_preflight(storage_stage)
        if not ok:
            emit(storage_stage.id, "skipped", reason, ["data.fptcloud_storage_policy.this"])
        elif not vpc_id:
            emit(storage_stage.id, "skipped", f"No VPC ID can be resolved for provider discovery; {vpc_diagnostics_message()}", ["data.fptcloud_storage_policy.this"])
        else:
            emit(storage_stage.id, "started", f"Using effective_vpc_id={vpc_id} for storage policy discovery", ["data.fptcloud_storage_policy.this"])
            storage_policy = discover_value(
                "storage-policy",
                "fptcloud_storage_policy",
                'try(data.fptcloud_storage_policy.this.storage_policies[0].id, "")',
                vpc_id,
                storage_stage.id,
            )
            if storage_policy:
                stage_status[storage_stage.id] = "done"

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

    discover_existing_subnets(spec.get(EXISTING_SUBNETS_STAGE))

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
    args = parser.parse_args()

    if LOG_PATH.exists():
        LOG_PATH.unlink()
    if RUN_ROOT.exists():
        shutil.rmtree(RUN_ROOT)
    emit("run", "started", f"Health-check run {RUN_ID} started")
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
        if check.stop_group_on_success:
            new_events = events[before:]
            if any(event["stage"] == check.name and event["status"] == "passed" for event in new_events):
                completed_groups.add(check.stop_group_on_success)
    wait_for_pending()
    emit("run", "done", f"Health-check run {RUN_ID} finished")


if __name__ == "__main__":
    main()
