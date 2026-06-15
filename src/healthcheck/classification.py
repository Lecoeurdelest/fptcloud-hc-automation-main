"""Centralized failure classification.

Maps provider/Terraform error text to the failure classifications declared in
specs/health-check.json. No Terraform execution here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from healthcheck import config, state
from healthcheck.models import FailureContext

QUOTA_CLEANUP_CLASSIFICATIONS = {"instance_quota_exceeded", "instance_storage_quota_exceeded"}


def is_quota_error(classification: str) -> bool:
    return classification in ("instance_storage_quota_exceeded", "instance_quota_exceeded")


def _extract_classification(text: str) -> str:
    """Extract failure classification from 'Classification: value' or 'classification=value'."""
    m = re.search(r"Classification:\s*([A-Za-z_]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|;\s*)classification=([A-Za-z_]+)", text)
    return m.group(1) if m else ""


def classify_error(text: str, resource_type: str) -> str:
    lowered = text.lower()
    if (
        'error_code":"804007' in lowered
        or "error_code=804007" in lowered
        or "804007" in lowered
        and "overlap" in lowered
    ):
        return "subnet_cidr_overlap"
    if (
        resource_type == "module.subnet"
        and state.stage_status.get(state.SUBNET_VALIDATION_STAGE) == "done"
        and ("failed to create a new subnet" in lowered or "fptcloud_subnet" in lowered)
    ):
        return "provider_or_backend_system_error_after_valid_inputs"
    if resource_type == "fptcloud_storage_policy" and "404" in lowered:
        return "provider_endpoint_or_datasource_mismatch"
    if "unknownerror" in lowered or "system error" in lowered:
        return "provider_or_backend_system_error"
    if "region" in lowered and "not enabled" in lowered:
        return "object_storage_region_disabled"
    if ("storage" in lowered or "disk" in lowered) and (
        "quota" in lowered or "exceed" in lowered or "insufficient" in lowered
    ):
        return "instance_storage_quota_exceeded"
    if "quota" in lowered or "exceed" in lowered or "insufficient" in lowered:
        return "instance_quota_exceeded"
    if "image" in lowered and (
        "not found" in lowered
        or "no match" in lowered
        or "invalid" in lowered
        or "unresolved" in lowered
    ):
        return "instance_image_unresolved"
    if "flavor" in lowered and (
        "not found" in lowered
        or "no match" in lowered
        or "invalid" in lowered
        or "unresolved" in lowered
    ):
        return "instance_flavor_unresolved"
    if (
        "password policy" in lowered
        or "password_policy" in lowered
        or "exceeded password" in lowered
    ):
        return "instance_password_policy_invalid"
    if "password" in lowered and ("missing" in lowered or "required" in lowered):
        return "instance_password_missing"
    if (
        "fptcloud_instance" in lowered
        or "module.vm" in lowered
        or "module.this.fptcloud_instance" in lowered
    ):
        return "instance_provider_error"
    if "subnet id is required" in lowered:
        return "blocked_missing_subnet_id"
    if "missing required" in lowered:
        return "configuration_missing"
    return "unknown"


def conflicting_subnet_name(text: str) -> str:
    match = re.search(r"\bin\s+([^,]+?)\s+subnet\b", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def classify_context(
    *,
    stage: str,
    resource_type: str,
    address: str,
    module_path: Path,
    reason: str,
    vars: dict[str, Any] | None = None,
) -> FailureContext:
    context = config.cloud_context()
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
