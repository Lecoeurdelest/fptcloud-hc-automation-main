"""Instance matrix orchestration: one VM per Terraform apply, optimistic quota.

Implements specs/06-QUOTA-AWARE-ROLLING-STRATEGY.md: quota_precheck=disabled,
quota_assumption=assume_sufficient, quota_exceeded_action=stop_and_wait_for_user.
Provider apply is the authoritative quota check; on quota exceeded the run blocks
and waits for explicit user confirmation with no auto cleanup/recovery/retry.
Also hosts instance input validation, hostname/password policy, round selection,
network validation, and the error-queue retry path.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from string import ascii_lowercase, digits
from typing import Any

from diagnose_health_inputs import diagnostics as input_diagnostics

from healthcheck import cleanup, config, discovery, state
from healthcheck import terraform_executor as tf
from healthcheck.classification import classify_context, classify_error, is_quota_error
from healthcheck.logging import emit, now, queue_error, queue_pending, safe_name
from healthcheck.models import Check, QueueItem, _ImageCreateResult
from healthcheck.reporting import (
    cleanup_policy_summary,
    format_failure,
    quota_report_message,
    redacted_vars,
)
from healthcheck.spec_loader import preflight, runnable_spec, spec_preflight


# ── Tags / names / hostnames ──────────────────────────────────────────────────
def build_hc_instance_tags(*, vpc_name: str, os_label: str, created_at: str) -> dict[str, str]:
    """Build the required health-check instance tags (spec 6.1, FR-017)."""
    return {
        "managed_by": "health-check",
        "health_check": "true",
        "hc_run_id": state.RUN_ID,
        "hc_created_at": created_at,
        "hc_vpc_name": vpc_name,
        "hc_os_label": os_label,
    }


def instance_name(suffix: str) -> str:
    return f"{config.env('HC_INSTANCE_NAME_PREFIX', 'hc-vm')}-{suffix}"


def matrix_instance_name(label: str, suffix: str) -> str:
    return f"{config.env('HC_INSTANCE_NAME_PREFIX', 'hc-vm')}-{safe_name(label)}-{state.INSTANCE_RUN_SUFFIX}"


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
    policy = config.spec_constants().get("INSTANCE_HOSTNAME_POLICY", {})
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
    discovered_images = dict(state.run_context.get("discovered_instance_images") or {})
    require_all_images = config.env_bool_default("HC_REQUIRE_ALL_INSTANCE_IMAGES", False)
    labels: list[str] = []
    for label, var_name in state.INSTANCE_IMAGE_MATRIX:
        if config.env(var_name) or discovered_images.get(label):
            labels.append(label)
        elif require_all_images:
            labels.append(label)
    return labels


# ── Password policy ───────────────────────────────────────────────────────────
def password_policy_result(password: str) -> dict[str, bool]:
    special_set = set(state.PASSWORD_SPECIALS)
    allowed_chars = set(
        ascii_lowercase + ascii_lowercase.upper() + digits + state.PASSWORD_SPECIALS
    )
    return {
        "password_generated": bool(password),
        "password_redacted": True,
        "length_ok": len(password) >= state.PASSWORD_MIN_LENGTH,
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


def validate_instance_password_policy_stage(stage) -> None:
    if not stage:
        return
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["generated_instance_password"])
        return
    result = password_policy_result(state.GENERATED_INSTANCE_PASSWORD)
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


# ── Hostname stage ────────────────────────────────────────────────────────────
def validate_instance_hostname_stage(stage, suffix: str) -> None:
    state.run_context["validated_instance_hostnames"] = {}
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["hostname-selection"])
        return
    blocked = [
        name
        for name in stage.dependency_stages
        if name != state.INSTANCE_HOSTNAME_VALIDATE_STAGE and not stage_ok_for(name)
    ]
    if blocked:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_hostname_invalid; Blocked by incomplete dependency stage(s): {', '.join(blocked)}",
            ["hostname-selection"],
        )
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
    state.run_context["validated_instance_hostnames"] = validated
    message = (
        f"validated_instance_hostnames={json.dumps(validated, sort_keys=True)}; "
        + " | ".join(details)
    )
    if all_errors:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_hostname_invalid; {message}",
            ["hostname-selection"],
        )
        return
    state.stage_status[stage.id] = "done"
    emit(stage.id, "done", message, ["hostname-selection"])


def stage_ok_for(stage: str) -> bool:
    return state.stage_status.get(stage) in {"done", "passed", "ready"}


def select_vpc_stage(stage) -> None:
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["vpc-selection"])
        return
    ctx = discovery.update_vpc_context()
    effective_vpc_id = str(ctx.get("effective_vpc_id") or "")
    vpc_name = str(ctx.get("vpc_name") or "")
    target_vpcs = discovery.target_vpc_entries()
    if not effective_vpc_id:
        emit(
            stage.id,
            "skipped",
            (
                "Classification: vpc_selection_failed; "
                f"target_vpcs={json.dumps([entry for entry, _raw in target_vpcs])}; "
                f"{discovery.vpc_diagnostics_message()}"
            ),
            ["vpc-selection"],
        )
        return
    state.stage_status[stage.id] = "done"
    emit(
        stage.id,
        "done",
        (
            "vpc.selected; "
            f"vpc_name={vpc_name or '<unset>'}; "
            f"effective_vpc_id={effective_vpc_id}; "
            f"vpc_id_source={ctx.get('vpc_id_source') or 'unresolved'}; "
            f"target_vpcs={json.dumps([entry for entry, _raw in target_vpcs])}; "
            "current_vpc_index=0; multi_vpc_iteration=single_vpc_path"
        ),
        ["vpc.selected", effective_vpc_id],
    )


# ── Quota reports (optimistic only) ───────────────────────────────────────────
def not_available_quota_report(target_disk_size: int | None = None) -> dict[str, Any]:
    disk_size = target_disk_size if target_disk_size is not None else config.root_disk_size()[0]
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
        "reduced_disk_test": config.reduced_disk_test(disk_size),
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
            "run_status": state.run_context.get("run_status", "running"),
            "user_action_required": bool(state.run_context.get("user_action_required", False)),
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
        path = state.ROOT / path
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
        "remaining_storage_gb": (
            "remaining_storage_gb",
            "remainingStorageGb",
            "storage_remaining_gb",
            "free_storage_gb",
        ),
        "storage_policy_quota_gb": (
            "storage_policy_quota_gb",
            "storagePolicyQuotaGb",
            "policy_quota_gb",
        ),
        "storage_policy_used_gb": (
            "storage_policy_used_gb",
            "storagePolicyUsedGb",
            "policy_used_gb",
        ),
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
        "existing_instances_consuming_storage": (
            "existing_instances_consuming_storage",
            "instances",
            "vms",
            "virtual_machines",
        ),
        "existing_volumes_consuming_storage": (
            "existing_volumes_consuming_storage",
            "volumes",
            "disks",
            "storages",
        ),
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


def inspect_instance_quota_stage(stage) -> None:
    state.run_context["instance_quota"] = optimistic_quota_report()
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["quota-inspection"])
        return
    blocked = [
        name
        for name in stage.dependency_stages
        if name != state.INSTANCE_QUOTA_INSPECT_STAGE and not stage_ok_for(name)
    ]
    if blocked:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_storage_quota_exceeded; Blocked by incomplete dependency stage(s): {', '.join(blocked)}",
            ["quota-inspection"],
        )
        return
    disk_size, disk_error = config.root_disk_size()
    report = optimistic_quota_report(disk_size)
    if disk_error:
        report["quota_input_error"] = disk_error
    state.run_context["instance_quota"] = report
    state.stage_status[stage.id] = "done"
    emit(
        stage.id,
        "done",
        f"quota_inspection=disabled; {quota_report_message(report)}",
        ["quota-inspection"],
    )


def validate_instance_quota_stage(stage) -> None:
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["quota-validation"])
        return
    blocked = [
        name
        for name in stage.dependency_stages
        if name != state.INSTANCE_QUOTA_VALIDATE_STAGE and not stage_ok_for(name)
    ]
    if blocked:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_storage_quota_exceeded_preflight; Blocked by incomplete dependency stage(s): {', '.join(blocked)}",
            ["quota-validation"],
        )
        return
    disk_size, disk_error = config.root_disk_size()
    report = optimistic_quota_report(disk_size)
    if disk_error:
        report["quota_input_error"] = disk_error
    state.run_context["instance_quota"] = report
    state.stage_status[stage.id] = "done"
    emit(
        stage.id,
        "done",
        f"preflight_decision=disabled_allow_apply; {quota_report_message(report)}",
        ["quota-validation"],
    )


# ── Instance input validation ─────────────────────────────────────────────────
def validate_instance_storage_policy_stage(stage, discovered_policy_id: str) -> str:
    if not stage:
        return discovered_policy_id
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["storage-policy-selection"])
        return discovered_policy_id
    blocked = [
        name
        for name in stage.dependency_stages
        if name != state.INSTANCE_STORAGE_POLICY_VALIDATE_STAGE and not stage_ok_for(name)
    ]
    if blocked:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_missing_required_inputs; Blocked by incomplete dependency stage(s): {', '.join(blocked)}",
            ["storage-policy-selection"],
        )
        return ""
    disk_size, disk_error = config.root_disk_size()
    selection = discovery.select_instance_storage_policy(discovered_policy_id)
    discovery.apply_selected_storage_policy(selection)
    message = (
        f"storage_policy_requested={selection.get('requested_name') or '<unset>'}; "
        f"selected_storage_policy_name={selection.get('name') or '<unresolved>'}; "
        f"selected_storage_policy_id={selection.get('id') or '<unresolved>'}; "
        f"selected_storage_policy_db_id={selection.get('id_db') or '<unresolved>'}; "
        f"storage_policy_source={selection.get('source', 'unresolved')}; "
        f"provider_field_used={selection.get('provider_field_used')}; "
        f"disk_size_gb={disk_size or '<invalid>'}; quota_status={state.run_context.get('selected_storage_policy_quota_status')}"
    )
    if disk_error or not selection.get("provider_value"):
        errors = [
            error
            for error in (
                disk_error,
                "storage policy unresolved" if not selection.get("provider_value") else "",
            )
            if error
        ]
        classification = selection.get("classification") or "instance_missing_required_inputs"
        emit(
            stage.id,
            "skipped",
            f"Classification: {classification}; errors={'; '.join(errors)}; {message}",
            ["storage-policy-selection"],
        )
        return ""
    state.stage_status[stage.id] = "done"
    emit(stage.id, "done", message, ["storage-policy-selection"])
    return selection.get("provider_value", "")


def instance_base_inputs(
    suffix: str, subnet_id: str, storage_policy_id: str
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    discovered_images = dict(state.run_context.get("discovered_instance_images") or {})
    image_sources = dict(state.run_context.get("instance_image_sources") or {})
    validated_hostnames = dict(state.run_context.get("validated_instance_hostnames") or {})
    flavor_name_value = str(
        state.run_context.get("discovered_instance_flavor_name") or config.env("HC_FLAVOR_NAME")
    )
    flavor_source = str(
        state.run_context.get("instance_flavor_source")
        or (
            "explicit_env"
            if config.env("HC_FLAVOR_NAME") or config.env("HC_FLAVOR_ID")
            else "unresolved"
        )
    )
    disk_size, disk_error = config.root_disk_size()
    errors: list[str] = []
    require_all_images = config.env_bool_default("HC_REQUIRE_ALL_INSTANCE_IMAGES", False)
    if not state.GENERATED_INSTANCE_PASSWORD:
        errors.append("generated_instance_password is required")
    missing_images = [
        label
        for label, var_name in state.INSTANCE_IMAGE_MATRIX
        if not (config.env(var_name) or discovered_images.get(label))
    ]
    for label in missing_images:
        if require_all_images:
            errors.append(f"discovered image for {label} is required")
    if not flavor_name_value:
        errors.append("discovered_instance_flavor is required")
    if not state.run_context.get("effective_vpc_id"):
        errors.append("effective_vpc_id is required")
    if not subnet_id:
        errors.append("effective_subnet_id is required")
    if not storage_policy_id:
        errors.append("effective_storage_policy_id is required")
    if not validated_hostnames:
        errors.append("validated_instance_hostnames is required")
    if disk_error:
        errors.append(disk_error)
    phase_values = config.instance_phase_runtime_values()
    constraint_errors = config.validate_phase_constraints(
        state.INSTANCE_CREATE_STAGE, phase_values
    )
    errors.extend(f"TOML constraint failed: {error}" for error in constraint_errors)
    if phase_values["instances_per_apply"] != 1:
        errors.append(
            "instances_per_apply values other than 1 are not supported by the current vm module"
        )
    if not phase_values["attach_subnet"]:
        errors.append("attach_subnet=false is not supported by the current vm module")
    if phase_values["assign_floating_ip"]:
        errors.append("assign_floating_ip=true requires a future floating-ip stage implementation")
    if phase_values["attach_security_group"] and not phase_values["security_group_ids"]:
        errors.append("attach_security_group=true requires security_group_ids or HC_SECURITY_GROUP_ID")
    if phase_values["resize_after_create"]:
        errors.append("resize_after_create=true requires a future resize stage implementation")
    if phase_values["create_snapshot"]:
        errors.append("create_snapshot=true requires a future snapshot stage implementation")
    if phase_values["add_nic"]:
        errors.append("add_nic=true requires a future additional-NIC stage implementation")
    keep_instance = config.keep_instance_enabled()
    cleanup_on_quota_exceeded = config.cleanup_on_quota_exceeded_enabled()
    quota_report = dict(
        state.run_context.get("instance_quota") or optimistic_quota_report(disk_size)
    )
    security_group_ids = config.instance_security_group_ids()
    instances: list[dict[str, Any]] = []
    for label, var_name in state.INSTANCE_IMAGE_MATRIX:
        if not require_all_images and not (config.env(var_name) or discovered_images.get(label)):
            continue
        hostname_entry = dict(validated_hostnames.get(label) or {})
        resource_name = str(
            hostname_entry.get("resource_name") or matrix_instance_name(label, suffix)
        )
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
                    "vpc_id": state.run_context.get("effective_vpc_id", ""),
                    "image_name": config.env(var_name) or discovered_images.get(label, ""),
                    "flavor_name": flavor_name_value,
                    "storage_policy_id": storage_policy_id,
                    "disk_gb": disk_size or config.official_instance_disk_size_gb(),
                    "subnet_id": subnet_id,
                    "status": "POWERED_ON",
                    "password": state.GENERATED_INSTANCE_PASSWORD,
                    "ssh_key": config.env("HC_SSH_KEY") or None,
                    "security_group_ids": security_group_ids,
                    "tags": build_hc_instance_tags(
                        vpc_name=state.run_context.get("vpc_name") or discovery.vpc_lookup_key(),
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
        "hostname_validation_status": sample_hostname.get(
            "hostname_validation_status", "unresolved"
        ),
        "validated_instance_hostnames": validated_hostnames,
        "matrix_count": len(state.INSTANCE_IMAGE_MATRIX),
        "images": {
            label: {
                "env_var": var_name,
                "value": config.env(var_name) or discovered_images.get(label, ""),
                "source": image_sources.get(
                    label, "explicit_env" if config.env(var_name) else "unresolved"
                ),
            }
            for label, var_name in state.INSTANCE_IMAGE_MATRIX
        },
        "effective_vpc_id": sample_vars.get("vpc_id", ""),
        "effective_subnet_id": subnet_id,
        "subnet_source": state.run_context.get("subnet_id_source", "unresolved"),
        "effective_storage_policy_id": storage_policy_id,
        "storage_policy_source": state.run_context.get("storage_policy_id_source", "unresolved"),
        "storage_policy_requested": state.run_context.get(
            "storage_policy_requested", discovery.preferred_instance_storage_policy_name()
        ),
        "selected_storage_policy_name": state.run_context.get("selected_storage_policy_name", ""),
        "selected_storage_policy_id": state.run_context.get("selected_storage_policy_id", ""),
        "selected_storage_policy_db_id": state.run_context.get("selected_storage_policy_db_id", ""),
        "provider_field_used": state.run_context.get(
            "selected_storage_policy_provider_field_used", "storage_policy_id"
        ),
        "storage_policy_quota_status": state.run_context.get(
            "selected_storage_policy_quota_status", "not_available"
        ),
        "image_source": "discovered" if not missing_images else "unresolved",
        "flavor_input": flavor_name_value,
        "flavor_source": flavor_source,
        "ssh_key_present": bool(config.env("HC_SSH_KEY")),
        "password_generated": bool(state.GENERATED_INSTANCE_PASSWORD),
        "password_redacted": True,
        "disk_size": disk_size or config.official_instance_disk_size_gb(),
        "disk_size_source": config.root_disk_size_source(),
        "reduced_disk_test": config.reduced_disk_test(disk_size),
        "keep_instance": keep_instance,
        "cleanup_on_quota_exceeded": cleanup_on_quota_exceeded,
        "cleanup_policy": "retain_by_default",
        "delete_after_create": phase_values["delete_after_create"],
        "toml_config_path": config.RUNTIME_CONFIG_RESULT.get("path", ""),
        "toml_config_loaded": bool(config.RUNTIME_CONFIG_RESULT.get("loaded")),
        "toml_constraint_count": len(config.phase_config(state.INSTANCE_CREATE_STAGE).get("constraints", [])),
        "attach_subnet": phase_values["attach_subnet"],
        "assign_floating_ip": phase_values["assign_floating_ip"],
        "attach_security_group": phase_values["attach_security_group"],
        "resize_after_create": phase_values["resize_after_create"],
        "create_snapshot": phase_values["create_snapshot"],
        "add_nic": phase_values["add_nic"],
        "quota_precheck": quota_report.get("quota_precheck", "disabled"),
        "quota_assumption": quota_report.get("quota_assumption", "assume_sufficient"),
        "quota_exceeded_action": quota_report.get(
            "quota_exceeded_action", "stop_and_wait_for_user"
        ),
        "quota_source": quota_report.get("quota_source", "unsupported_or_not_found"),
        "quota_status": quota_report.get("quota_status", "not_available"),
        "remaining_storage_gb": quota_report.get("remaining_storage_gb", "not_available"),
        "target_requested_disk_size_gb": quota_report.get(
            "target_requested_disk_size_gb", disk_size or "not_available"
        ),
        "run_status": quota_report.get(
            "run_status", state.run_context.get("run_status", "running")
        ),
        "user_action_required": quota_report.get(
            "user_action_required", state.run_context.get("user_action_required", False)
        ),
        "require_all_images": require_all_images,
        "resolved_count": len(instances),
        "unresolved_count": len(state.INSTANCE_IMAGE_MATRIX) - len(instances),
    }
    return {"instances": instances}, diagnostics, errors


def validate_instance_stage(stage, suffix: str, subnet_id: str, storage_policy_id: str) -> None:
    state.instance_validation.update({"valid": False, "vars": {}, "diagnostics": {}, "errors": []})
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["module.vm"])
        return
    blocked = [
        name
        for name in stage.dependency_stages
        if name != state.INSTANCE_VALIDATE_STAGE and not stage_ok_for(name)
    ]
    if blocked:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_missing_required_inputs; Blocked by incomplete dependency stage(s): {', '.join(blocked)}",
            ["module.vm"],
        )
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
        elif any(
            "hostname" in error.lower() or "resource_name" in error.lower() for error in errors
        ):
            classification = "instance_hostname_invalid"
        state.instance_validation.update(
            {"valid": False, "vars": vars, "diagnostics": diagnostics, "errors": errors}
        )
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
                f"instance_name_pattern={config.env('HC_INSTANCE_NAME_PREFIX', 'hc-vm')}-<os>-{suffix[-8:]}; disk_size={diagnostics['disk_size']}; "
                f"disk_size_source={diagnostics['disk_size_source']}; reduced_disk_test={diagnostics['reduced_disk_test']}; "
                f"password_generated={diagnostics['password_generated']}; password_redacted={diagnostics['password_redacted']}; ssh_key_present={diagnostics['ssh_key_present']}; "
                f"toml_config_path={diagnostics['toml_config_path']}; toml_config_loaded={diagnostics['toml_config_loaded']}; "
                f"toml_constraint_count={diagnostics['toml_constraint_count']}; "
                f"delete_after_create={diagnostics['delete_after_create']}; attach_subnet={diagnostics['attach_subnet']}; "
                f"assign_floating_ip={diagnostics['assign_floating_ip']}; attach_security_group={diagnostics['attach_security_group']}; "
                f"resize_after_create={diagnostics['resize_after_create']}; create_snapshot={diagnostics['create_snapshot']}; add_nic={diagnostics['add_nic']}; "
                f"{cleanup_policy_summary()}; "
                f"terraform_vars={json.dumps(redacted_vars(vars), sort_keys=True)}"
            ),
            ["module.this.fptcloud_instance.this"],
        )
        return
    state.instance_validation.update(
        {"valid": True, "vars": vars, "diagnostics": diagnostics, "errors": []}
    )
    state.stage_status[stage.id] = "done"
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
            f"instance_name_pattern={config.env('HC_INSTANCE_NAME_PREFIX', 'hc-vm')}-<os>-{suffix[-8:]}; disk_size={diagnostics['disk_size']}; "
            f"disk_size_source={diagnostics['disk_size_source']}; reduced_disk_test={diagnostics['reduced_disk_test']}; "
            f"password_generated={diagnostics['password_generated']}; password_redacted={diagnostics['password_redacted']}; ssh_key_present={diagnostics['ssh_key_present']}; "
            f"toml_config_path={diagnostics['toml_config_path']}; toml_config_loaded={diagnostics['toml_config_loaded']}; "
            f"toml_constraint_count={diagnostics['toml_constraint_count']}; "
            f"delete_after_create={diagnostics['delete_after_create']}; attach_subnet={diagnostics['attach_subnet']}; "
            f"assign_floating_ip={diagnostics['assign_floating_ip']}; attach_security_group={diagnostics['attach_security_group']}; "
            f"resize_after_create={diagnostics['resize_after_create']}; create_snapshot={diagnostics['create_snapshot']}; add_nic={diagnostics['add_nic']}; "
            f"{cleanup_policy_summary()}; "
            f"terraform_vars={json.dumps(redacted_vars(vars), sort_keys=True)}"
        ),
        ["module.this.fptcloud_instance.this"],
    )


# ── Round selection / network validation ──────────────────────────────────────
def selected_round_labels() -> list[str]:
    round_info = dict(state.run_context.get("selected_instance_round") or {})
    return [str(label) for label in round_info.get("selected_images", [])]


def selected_round_instances() -> list[dict[str, Any]]:
    instances = list((state.instance_validation.get("vars") or {}).get("instances") or [])
    selected = set(selected_round_labels())
    if not selected:
        return instances
    return [item for item in instances if str(item.get("label") or "") in selected]


def select_instance_round_stage(stage) -> None:
    # Governed by specs/health-check.json INSTANCE_BATCHING_POLICY and compute.select-instance-round.
    state.run_context["selected_instance_round"] = {}
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["instance-round-selection"])
        return
    blocked = [
        name
        for name in stage.dependency_stages
        if name != state.INSTANCE_ROUND_SELECT_STAGE and not stage_ok_for(name)
    ]
    if blocked:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_image_unresolved; Blocked by incomplete dependency stage(s): {', '.join(blocked)}",
            ["instance-round-selection"],
        )
        return
    instances = list((state.instance_validation.get("vars") or {}).get("instances") or [])
    discovered_images = dict(state.run_context.get("discovered_instance_images") or {})
    resolved_labels = [str(item.get("label") or "") for item in instances if item.get("label")]
    unavailable = [
        label
        for label, _var_name in state.INSTANCE_IMAGE_MATRIX
        if label not in resolved_labels and not discovered_images.get(label)
    ]
    order = config.instance_selection_order()
    selected = [label for label in order if label in resolved_labels]
    disk_size, _disk_error = config.root_disk_size()
    per_apply = config.instances_per_apply()
    apply_count = min(per_apply, len(selected))
    round_info = {
        "instances_per_apply": per_apply,
        "stop_on_quota_exceeded": config.stop_on_quota_exceeded_enabled(),
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
    state.run_context["selected_instance_round"] = round_info
    if not selected:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_image_unresolved; selected_images=[]; unavailable_images={json.dumps(unavailable)}",
            ["instance-round-selection"],
        )
        return
    state.stage_status[stage.id] = "done"
    emit(
        stage.id,
        "done",
        (
            f"instances_per_apply={per_apply}; stop_on_quota_exceeded={config.stop_on_quota_exceeded_enabled()}; "
            f"selected_images={json.dumps(selected)}; successful_images=[]; failed_image=<none>; "
            f"failure_reason=<none>; remaining_images_not_attempted=[]; unavailable_images={json.dumps(unavailable)}; "
            f"apply_requested_instance_count={round_info['apply_requested_instance_count']}; "
            f"apply_requested_storage_gb={round_info['apply_requested_storage_gb']}; "
            f"apply_requested_cpu={round_info['apply_requested_cpu']}; "
            f"apply_requested_ram_mb={round_info['apply_requested_ram_mb']}"
        ),
        ["instance-round-selection"],
    )


def validate_instance_network_stage(stage) -> None:
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["module.this.fptcloud_instance.this"])
        return
    blocked = [
        name
        for name in stage.dependency_stages
        if name != state.INSTANCE_NETWORK_VALIDATE_STAGE and not stage_ok_for(name)
    ]
    if blocked:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_network_attachment_missing; Blocked by incomplete dependency stage(s): {', '.join(blocked)}",
            ["module.this.fptcloud_instance.this"],
        )
        return
    instances = selected_round_instances()
    errors: list[str] = []
    if not state.run_context.get("effective_vpc_id"):
        errors.append("effective_vpc_id is required")
    if not state.run_context.get("effective_subnet_id"):
        errors.append("effective_subnet_id is required")
    if not state.run_context.get("effective_storage_policy_id"):
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
    names = ", ".join(
        str((item.get("vars") or {}).get("name") or item.get("label") or "") for item in instances
    )
    first_vars = dict((instances[0].get("vars") if instances else {}) or {})
    phase_values = config.instance_phase_runtime_values()
    public_ip_configured = bool(
        first_vars.get("public_ip")
        or config.env("HC_PUBLIC_IP")
        or phase_values["assign_floating_ip"]
    )
    security_group_configured = bool(
        first_vars.get("security_group_ids")
        or config.env("HC_SECURITY_GROUP_ID")
        or phase_values["attach_security_group"]
    )
    message = (
        f"generated_suffix={state.INSTANCE_RUN_SUFFIX}; effective_vpc_id={state.run_context.get('effective_vpc_id') or '<unset>'}; "
        f"effective_subnet_id={state.run_context.get('effective_subnet_id') or '<unset>'}; "
        f"effective_storage_policy_id={state.run_context.get('effective_storage_policy_id') or '<unset>'}; "
        f"network_attachment_fields=vpc_id,subnet_id; subnet_vpc_membership=not_verified; "
        f"security_group_configured={security_group_configured}; public_ip_configured={public_ip_configured}; "
        f"delete_after_create={phase_values['delete_after_create']}; resize_after_create={phase_values['resize_after_create']}; "
        f"create_snapshot={phase_values['create_snapshot']}; add_nic={phase_values['add_nic']}; "
        f"connection_test_policy=manual_verification_required; instance_names={names or '<none>'}; "
        f"terraform_network_vars={json.dumps(redacted_vars({'vpc_id': first_vars.get('vpc_id'), 'subnet_id': first_vars.get('subnet_id'), 'storage_policy_id': first_vars.get('storage_policy_id'), 'security_group_ids': list(first_vars.get('security_group_ids') or [])}), sort_keys=True)}"
    )
    if errors:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_network_attachment_missing; errors={'; '.join(errors)}; {message}",
            ["module.this.fptcloud_instance.this"],
        )
        return
    state.stage_status[stage.id] = "done"
    emit(
        stage.id,
        "done",
        f"Network attachment validation passed; {message}",
        ["module.this.fptcloud_instance.this"],
    )


def resolve_instance_image_flavor(
    vars: dict[str, Any], diagnostics: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str]:
    resolved = dict(vars)
    updated = dict(diagnostics)
    vpc_id = str(resolved.get("vpc_id") or "")
    updated["image_source"] = "name" if resolved.get("image_name") else "unresolved"
    if config.env("HC_FLAVOR_ID"):
        flavor_name_value = discovery.discover_filtered_value(
            name="flavor",
            source="fptcloud_flavor",
            collection="flavors",
            output_attr="name",
            filter_key="id",
            filter_value=config.env("HC_FLAVOR_ID"),
            vpc_id=vpc_id,
            stage_id="compute.resolve-instance-flavor",
        )
        if not flavor_name_value:
            updated["flavor_source"] = "unresolved"
            return (
                resolved,
                updated,
                "Classification: instance_flavor_unresolved; HC_FLAVOR_ID did not resolve to a flavor name",
            )
        resolved["flavor_name"] = flavor_name_value
        updated["flavor_source"] = "explicit_id"
    elif config.env("HC_FLAVOR_NAME"):
        flavor_name_value = discovery.discover_filtered_value(
            name="flavor",
            source="fptcloud_flavor",
            collection="flavors",
            output_attr="name",
            filter_key="name",
            filter_value=config.env("HC_FLAVOR_NAME"),
            vpc_id=vpc_id,
            stage_id="compute.resolve-instance-flavor",
        )
        if flavor_name_value:
            resolved["flavor_name"] = flavor_name_value
            updated["flavor_source"] = "discovered"
        else:
            updated["flavor_source"] = "name"
    return resolved, updated, ""


def wait_for_pending() -> None:
    deadline = time.time() + state.PENDING_TIMEOUT_SECONDS
    while state.pending_queue and time.time() < deadline:
        item = state.pending_queue.pop(0)
        workspace = Path(item.workspace)
        ready, reason, resources = tf.readiness(workspace)
        if ready:
            emit(
                item.check,
                "ready",
                f"Pending task completed: {reason}",
                resources or item.resources,
            )
            continue
        resources = resources or item.resources
        state.pending_queue.append(QueueItem(item.check, item.workspace, resources, reason, now()))
        emit(item.check, "waiting", f"Still pending: {reason}", resources)
        time.sleep(state.PENDING_POLL_SECONDS)

    while state.pending_queue:
        item = state.pending_queue.pop(0)
        queue_error(
            item.check, Path(item.workspace), item.resources, f"Pending timeout: {item.reason}"
        )


# ── Instance creation ─────────────────────────────────────────────────────────
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
    calling queue_error - caller decides (enqueue for retry vs final fail).
    Resolve failure: emits check.name "failed" inline, returns retryable=False.
    """
    base_resources: list[str] = ["module.this.fptcloud_instance.this"]
    label = str(item.get("label") or "instance")
    diagnostics = dict(state.instance_validation.get("diagnostics") or {})
    diagnostics["os_label"] = label
    instance_vars = dict(item.get("vars") or {})
    hostname_entry = dict(item.get("hostname") or {})
    workspace_suffix = safe_name(label) if attempt == 1 else f"{safe_name(label)}-retry-{attempt}"

    resolved_vars, diagnostics, resolve_error = resolve_instance_image_flavor(
        instance_vars, diagnostics
    )
    if resolve_error:
        emit(
            check.name,
            "failed",
            f"os_label={label}; attempt={attempt}; {resolve_error}; Terraform apply not called",
            base_resources,
        )
        return _ImageCreateResult(
            label=label,
            succeeded=False,
            is_quota=False,
            retryable=False,
            classification="instance_image_unresolved",
            error_code="",
            terraform_error=resolve_error,
            workspace=state.RUN_ROOT / check.name / workspace_suffix,
            resources=base_resources,
            context=None,
            failed_instance_id="",
        )

    matrix_check = Check(
        name=check.name, module=check.module, vars=resolved_vars, required_vars=check.required_vars
    )
    workspace = tf.write_workspace_at(matrix_check, state.RUN_ROOT / check.name / workspace_suffix)
    emit(
        f"{check.name}:inputs",
        "done",
        (
            f"provider={provider.get('source')} {provider.get('version')}; os_label={label}; attempt={attempt}; "
            f"effective_vpc_id={matrix_check.vars.get('vpc_id') or '<unset>'}; "
            f"effective_subnet_id={matrix_check.vars.get('subnet_id') or '<unset>'}; "
            f"effective_storage_policy_id={matrix_check.vars.get('storage_policy_id') or '<unset>'}; "
            f"storage_policy_requested={diagnostics.get('storage_policy_requested') or discovery.preferred_instance_storage_policy_name()}; "
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
            f"run_status={state.run_context.get('run_status', diagnostics.get('run_status', 'running'))}; "
            f"user_action_required={state.run_context.get('user_action_required', diagnostics.get('user_action_required', False))}; "
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
            f"disk_size={matrix_check.vars.get('disk_gb')}; disk_size_source={diagnostics.get('disk_size_source', config.root_disk_size_source())}; "
            f"reduced_disk_test={diagnostics.get('reduced_disk_test', False)}; instance_name={matrix_check.vars.get('name')}; "
            f"generated_suffix={state.INSTANCE_RUN_SUFFIX}; network_attachment_fields=vpc_id,subnet_id; "
            f"password_generated={bool(state.GENERATED_INSTANCE_PASSWORD)}; password_redacted=True; "
            f"toml_config_path={diagnostics.get('toml_config_path', '')}; toml_config_loaded={diagnostics.get('toml_config_loaded', False)}; "
            f"toml_constraint_count={diagnostics.get('toml_constraint_count', 0)}; "
            f"delete_after_create={diagnostics.get('delete_after_create', False)}; "
            f"attach_subnet={diagnostics.get('attach_subnet', True)}; "
            f"assign_floating_ip={diagnostics.get('assign_floating_ip', False)}; "
            f"attach_security_group={diagnostics.get('attach_security_group', False)}; "
            f"resize_after_create={diagnostics.get('resize_after_create', False)}; "
            f"create_snapshot={diagnostics.get('create_snapshot', False)}; add_nic={diagnostics.get('add_nic', False)}; "
            f"public_ip_configured={bool(matrix_check.vars.get('public_ip') or config.env('HC_PUBLIC_IP'))}; "
            f"security_group_configured={bool(matrix_check.vars.get('security_group_ids') or config.env('HC_SECURITY_GROUP_ID'))}; "
            f"connection_test_policy=manual_verification_required; instance_group_relevance=not_required_for_password_or_network_attachment; "
            f"instances_per_apply={round_info.get('instances_per_apply', config.instances_per_apply())}; "
            f"stop_on_quota_exceeded={round_info.get('stop_on_quota_exceeded', config.stop_on_quota_exceeded_enabled())}; "
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
    emit(
        check.name,
        "started",
        f"os_label={label}; attempt={attempt}; Initializing Terraform workspace",
        base_resources,
    )

    init = tf.run_instance_terraform(
        ["terraform", "init", "-input=false", "-no-color"],
        workspace,
        stage=f"{check.name}-{label}-init-attempt-{attempt}",
    )
    if init.returncode != 0:
        context = classify_context(
            stage=check.name,
            resource_type="module.vm",
            address="terraform.init",
            module_path=module_path,
            reason=(init.stderr or init.stdout)[-1200:],
            vars=matrix_check.vars,
        )
        return _ImageCreateResult(
            label=label,
            succeeded=False,
            is_quota=False,
            retryable=True,
            classification=context.classification,
            error_code="",
            terraform_error=(init.stderr or init.stdout)[-1200:],
            workspace=workspace,
            resources=base_resources,
            context=context,
            failed_instance_id="",
        )

    plan = tf.run_instance_terraform(
        ["terraform", "plan", "-out=tfplan", "-detailed-exitcode", "-no-color", "-input=false"],
        workspace,
        stage=f"{check.name}-{label}-plan-attempt-{attempt}",
    )
    planned = tf.planned_resources(workspace)
    if plan.returncode not in (0, 2):
        context = classify_context(
            stage=check.name,
            resource_type="module.vm",
            address=", ".join(planned) or "terraform.plan",
            module_path=module_path,
            reason=(plan.stderr or plan.stdout)[-1200:],
            vars=matrix_check.vars,
        )
        return _ImageCreateResult(
            label=label,
            succeeded=False,
            is_quota=False,
            retryable=True,
            classification=context.classification,
            error_code="",
            terraform_error=(plan.stderr or plan.stdout)[-1200:],
            workspace=workspace,
            resources=planned or base_resources,
            context=context,
            failed_instance_id="",
        )
    emit(
        check.name,
        "pending",
        f"os_label={label}; attempt={attempt}; Plan completed; instance will be created",
        planned or base_resources,
    )

    apply = tf.run_instance_terraform(
        ["terraform", "apply", "-auto-approve", "-no-color", "-input=false", "tfplan"],
        workspace,
        stage=f"{check.name}-{label}-apply-attempt-{attempt}",
    )
    current = tf.state_resources(workspace) or planned or base_resources

    if apply.returncode == 0:
        created_id = tf.instance_id_from_state(workspace)
        successful_images.append(label)
        if created_id:
            created_instance_records.append({"os_label": label, "instance_id": created_id})
            ids_path = state.RUN_ROOT / check.name / "created-instances.json"
            ids_path.parent.mkdir(parents=True, exist_ok=True)
            ids_path.write_text(
                json.dumps(created_instance_records, indent=2, sort_keys=True), encoding="utf-8"
            )
        round_info["successful_images"] = successful_images
        state.run_context["selected_instance_round"] = round_info
        emit(
            check.name,
            "passed",
            (
                f"os_label={label}; attempt={attempt}; per_instance_create_result=created; "
                f"successful_images={json.dumps(successful_images)}; instance_id={created_id or '<unknown>'}; "
                f"persisted_instance_ids={json.dumps(created_instance_records, sort_keys=True)}; "
                f"instance_status=created; settling briefly"
            ),
            current,
        )
        time.sleep(state.SETTLE_SECONDS)
        ready, ready_reason, current = tf.readiness(workspace)
        if ready:
            emit(
                check.name,
                "ready",
                f"os_label={label}; attempt={attempt}; instance_id={created_id or '<unknown>'}; instance_status=ready; {ready_reason}",
                current or planned or base_resources,
            )
        else:
            queue_pending(
                check.name,
                workspace,
                current or planned or base_resources,
                f"os_label={label}; attempt={attempt}; {ready_reason}",
            )
            wait_for_pending()
        if config.instance_delete_after_create_enabled():
            cleanup_resources = current or planned or base_resources
            emit(
                f"{check.name}:cleanup",
                "started",
                (
                    f"delete_after_create=True; os_label={label}; "
                    "destroying current-run instance workspace after successful validation"
                ),
                cleanup_resources,
            )
            destroy_result = tf.run_instance_terraform(
                ["terraform", "destroy", "-auto-approve", "-no-color", "-input=false"],
                workspace,
                stage=f"{check.name}-{label}-delete-after-create",
            )
            if destroy_result.returncode == 0:
                emit(
                    f"{check.name}:cleanup",
                    "destroyed",
                    (
                        f"delete_after_create=True; os_label={label}; "
                        f"deleted_instance_ids={json.dumps([created_id] if created_id else [])}"
                    ),
                    cleanup_resources,
                )
            else:
                queue_error(
                    f"{check.name}:cleanup",
                    workspace,
                    cleanup_resources,
                    (
                        "Classification: instance_cleanup_failed; "
                        "delete_after_create=True; terraform destroy failed; "
                        f"{(destroy_result.stderr or destroy_result.stdout)[-1200:]}"
                    ),
                )
        else:
            cleanup.retain_instance(
                check.name,
                label=label,
                instance_id=created_id,
                classification="",
                failed=False,
                resources=current or planned or base_resources,
            )
        return _ImageCreateResult(
            label=label,
            succeeded=True,
            is_quota=False,
            retryable=False,
            classification="",
            error_code="",
            terraform_error="",
            workspace=workspace,
            resources=list(current or planned or base_resources),
            context=None,
            failed_instance_id="",
        )

    # Apply failed - classify and retain.
    raw_apply = (apply.stderr or apply.stdout)[-1000:]
    classification = classify_error(raw_apply, "module.vm")
    context = classify_context(
        stage=check.name,
        resource_type="module.vm",
        address=", ".join(current) or "terraform.apply",
        module_path=module_path,
        reason=f"Classification: {classification}; Apply failed: {raw_apply}",
        vars=matrix_check.vars,
    )
    failed_instance_id = tf.instance_id_from_state(workspace)
    cleanup.retain_instance(
        check.name,
        label=label,
        instance_id=failed_instance_id,
        classification=classification,
        failed=True,
        resources=current,
    )
    # Only update remaining_images_not_attempted on the first pass (attempt 1);
    # retries don't shift what is "remaining" since all images were already attempted.
    remaining = (
        [str(next_item.get("label") or "") for next_item in instances[index + 1 :]]
        if attempt == 1
        else []
    )
    round_info.update(
        {
            "successful_images": successful_images,
            "failed_image": label,
            "failure_reason": classification,
            "remaining_images_not_attempted": remaining,
            "stop_on_quota_exceeded": True,
        }
    )
    state.run_context["selected_instance_round"] = round_info

    if is_quota_error(classification):
        state.run_context.update(
            {
                "run_status": "blocked_waiting_user_confirmation",
                "run_blocked": True,
                "user_action_required": True,
                "failed_image": label,
                "failure_reason": classification,
                "remaining_images_not_attempted": remaining,
                "stop_on_quota_exceeded": True,
            }
        )
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
            label=label,
            succeeded=False,
            is_quota=True,
            retryable=False,
            classification=classification,
            error_code="",
            terraform_error=raw_apply,
            workspace=workspace,
            resources=list(current),
            context=context,
            failed_instance_id=failed_instance_id,
        )

    # Non-quota apply failure: return without emitting check.name "failed".
    # Caller emits instance.create_failed_queued (first pass) or instance.retry_* (retry phase).
    return _ImageCreateResult(
        label=label,
        succeeded=False,
        is_quota=False,
        retryable=True,
        classification=classification,
        error_code="",
        terraform_error=raw_apply,
        workspace=workspace,
        resources=list(current),
        context=context,
        failed_instance_id=failed_instance_id,
    )


def validate_instance_active_stage(stage) -> None:
    if not stage:
        return
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["instance-validation"])
        return
    blocked = [
        name for name in stage.dependency_stages if name != stage.id and not stage_ok_for(name)
    ]
    if blocked:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_validation_failed; Blocked by incomplete dependency stage(s): {', '.join(blocked)}",
            ["instance-validation"],
        )
        return

    round_info = dict(state.run_context.get("selected_instance_round") or {})
    successful_images = [str(label) for label in round_info.get("successful_images") or []]
    created_path = state.RUN_ROOT / state.INSTANCE_CREATE_STAGE / "created-instances.json"
    created_records: list[dict[str, Any]] = []
    if created_path.exists():
        try:
            raw_records = json.loads(created_path.read_text(encoding="utf-8"))
            if isinstance(raw_records, list):
                created_records = [dict(item) for item in raw_records if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError) as exc:
            emit(
                stage.id,
                "skipped",
                f"Classification: instance_validation_failed; cannot_read_created_instances={exc}",
                [str(created_path)],
            )
            return

    if not successful_images and not created_records:
        emit(
            stage.id,
            "skipped",
            (
                "Classification: instance_validation_failed; "
                "no successful instance create result is available; "
                f"run_status={state.run_context.get('run_status') or '<unset>'}; "
                f"failed_image={round_info.get('failed_image') or '<none>'}; "
                f"failure_reason={round_info.get('failure_reason') or '<none>'}"
            ),
            ["instance-validation"],
        )
        return

    missing_ids = [
        str(record.get("os_label") or "<unknown>")
        for record in created_records
        if not str(record.get("instance_id") or "")
    ]
    if missing_ids:
        emit(
            stage.id,
            "skipped",
            (
                "Classification: instance_validation_failed; "
                f"missing_instance_ids={json.dumps(missing_ids)}"
            ),
            ["instance-validation"],
        )
        return

    diagnostics = dict(state.instance_validation.get("diagnostics") or {})
    state.stage_status[stage.id] = "done"
    emit(
        stage.id,
        "done",
        (
            "instance.validated; "
            "provider_observable_validation=True; "
            "boot_probe=manual_verification_required; "
            f"successful_images={json.dumps(successful_images, sort_keys=True)}; "
            f"created_instances={json.dumps(created_records, sort_keys=True)}; "
            f"effective_vpc_id={state.run_context.get('effective_vpc_id') or '<unset>'}; "
            f"effective_subnet_id={state.run_context.get('effective_subnet_id') or '<unset>'}; "
            f"effective_storage_policy_id={state.run_context.get('effective_storage_policy_id') or '<unset>'}; "
            f"password_generated={diagnostics.get('password_generated', bool(state.GENERATED_INSTANCE_PASSWORD))}; "
            "password_redacted=True; "
            "instance_status=ready_or_created_by_provider_apply"
        ),
        ["instance.validated", str(created_path)],
    )


def execute_instance_create(check: Check) -> None:
    # Governed by specs/health-check.json INSTANCE_ERROR_QUEUE_RETRY_POLICY and compute.create-instance.
    resources = ["module.this.fptcloud_instance.this"]
    if not state.instance_validation.get("valid"):
        emit(
            check.name,
            "skipped",
            "Classification: instance_missing_required_inputs; compute.validate-instance-inputs did not pass; Terraform apply not called",
            resources,
        )
        return
    ok, reason = preflight(check)
    if not ok:
        emit(
            check.name,
            "skipped",
            f"Classification: instance_missing_required_inputs; {reason}; Terraform apply not called",
            resources,
        )
        return
    module_path = state.MODULES / check.module
    provider = input_diagnostics()["provider"]

    # In-memory error queue for non-quota instance creation failures (per INSTANCE_ERROR_QUEUE_RETRY_POLICY).
    instance_error_queue: list[dict[str, Any]] = []

    try:
        with tf.ResourceLock(check.name):
            instances = selected_round_instances()
            round_info = dict(state.run_context.get("selected_instance_round") or {})
            successful_images: list[str] = list(round_info.get("successful_images") or [])
            created_instance_records: list[dict[str, str]] = []
            if not instances:
                emit(
                    check.name,
                    "skipped",
                    "Classification: instance_image_unresolved; selected_round_images is empty; Terraform apply not called",
                    resources,
                )
                return

            # First pass: attempt each image in spec order.  Non-quota failures are
            # enqueued for retry and the loop continues (INSTANCE_ERROR_QUEUE_RETRY_POLICY).
            for index, item in enumerate(instances):
                label = str(item.get("label") or "instance")
                result = _execute_one_image_attempt(
                    check,
                    item,
                    1,
                    module_path,
                    provider,
                    round_info,
                    successful_images,
                    instances,
                    index,
                    created_instance_records,
                )
                if result.succeeded:
                    pass  # success side-effects handled inside helper
                elif result.is_quota:
                    return  # run_context already updated; stop entire workflow
                elif not result.retryable:
                    pass  # resolve error: check.name "failed" already emitted; continue to next image
                else:
                    remaining = [str(inst.get("label") or "") for inst in instances[index + 1 :]]
                    image_name = str(item.get("vars", {}).get("image_name") or "")
                    instance_error_queue.append(
                        {
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
                        }
                    )
                    emit(
                        "instance.create_failed_queued",
                        "queued",
                        (
                            f"image_label={label}; image_name={image_name}; "
                            f"attempt=1; max_attempts={state.MAX_INSTANCE_CREATE_ATTEMPTS}; "
                            f"classification={result.classification}; "
                            f"error_code={result.error_code or 'n/a'}; "
                            f"terraform_error={result.terraform_error[:500]}; "
                            f"queued=True; "
                            f"remaining_retry_attempts={state.MAX_INSTANCE_CREATE_ATTEMPTS - 1}; "
                            f"remaining_images_not_attempted={json.dumps(remaining)}"
                        ),
                        result.resources,
                    )

            # Retry phase: process all queued non-quota failures after the full first pass.
            # Each case gets up to MAX_INSTANCE_CREATE_ATTEMPTS total (attempt 1 = first pass).
            initial_queued_count = len(instance_error_queue)
            retry_succeeded_count = 0
            retry_exhausted_count = 0

            for queued in list(instance_error_queue):
                label = queued["label"]
                item = queued["item"]
                index = queued["index"]
                image_name = queued["image_name"]

                for attempt in range(2, state.MAX_INSTANCE_CREATE_ATTEMPTS + 1):
                    remaining_retries = state.MAX_INSTANCE_CREATE_ATTEMPTS - attempt
                    queue_pos = (
                        instance_error_queue.index(queued) + 1
                        if queued in instance_error_queue
                        else 0
                    )
                    emit(
                        "instance.retry_started",
                        "started",
                        (
                            f"image_label={label}; image_name={image_name}; "
                            f"attempt={attempt}; max_attempts={state.MAX_INSTANCE_CREATE_ATTEMPTS}; "
                            f"queue_position={queue_pos}"
                        ),
                        queued["resources"],
                    )
                    result = _execute_one_image_attempt(
                        check,
                        item,
                        attempt,
                        module_path,
                        provider,
                        round_info,
                        successful_images,
                        instances,
                        index,
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
                                f"attempt={attempt}; max_attempts={state.MAX_INSTANCE_CREATE_ATTEMPTS}; "
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
                                f"attempt={attempt}; max_attempts={state.MAX_INSTANCE_CREATE_ATTEMPTS}; "
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
                                f"attempt={attempt}; max_attempts={state.MAX_INSTANCE_CREATE_ATTEMPTS}; "
                                f"classification={result.classification}; "
                                f"error_code={result.error_code or 'n/a'}; "
                                f"terraform_error={result.terraform_error[:500]}; "
                                f"final_failure=True; user_action_required=False"
                            ),
                            result.resources,
                        )
                        failure_reason = (
                            format_failure(result.context)
                            if result.context
                            else result.terraform_error
                        )
                        queue_error(check.name, result.workspace, result.resources, failure_reason)

            # Retry-phase summary.
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
        queue_error(check.name, state.RUN_ROOT / check.name, resources, str(exc))
