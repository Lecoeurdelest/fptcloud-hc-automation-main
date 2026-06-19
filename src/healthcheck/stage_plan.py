"""Load runner execution mapping from the health-check JSON spec.

This module is intentionally small: JSON owns which stage is wired to which
implementation module and variable source; implementation modules only know how
to build runtime values for those configured names.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from healthcheck import config, spec_loader, state
from healthcheck.models import Check, StageSpec


@dataclass(frozen=True)
class TerraformCheckSpec:
    stage: str
    module: str
    vars: str
    required_vars: tuple[str, ...] = ()
    per_region: bool = False
    stop_group_on_success: str | None = None


def load_terraform_checks(path: Path = state.SPEC_PATH) -> list[TerraformCheckSpec]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_checks = data.get("runner_plan", {}).get("terraform_checks", [])
    if not isinstance(raw_checks, list):
        raise ValueError("runner_plan.terraform_checks must be a list")

    checks: list[TerraformCheckSpec] = []
    for index, raw in enumerate(raw_checks, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"runner_plan.terraform_checks[{index}] must be an object")
        try:
            stage = str(raw["stage"])
            module = str(raw["module"])
            vars_source = str(raw["vars"])
        except KeyError as exc:
            raise ValueError(
                f"runner_plan.terraform_checks[{index}] missing required field {exc.args[0]!r}"
            ) from exc
        required_vars = raw.get("required_vars", [])
        if not isinstance(required_vars, list):
            raise ValueError(f"{stage}: required_vars must be a list")
        checks.append(
            TerraformCheckSpec(
                stage=stage,
                module=module,
                vars=vars_source,
                required_vars=tuple(str(item) for item in required_vars),
                per_region=bool(raw.get("per_region", False)),
                stop_group_on_success=(
                    str(raw["stop_group_on_success"])
                    if raw.get("stop_group_on_success")
                    else None
                ),
            )
        )
    return checks


def vars_for_source(
    source: str,
    *,
    suffix: str,
    vpc_id: str,
    subnet_id: str,
    storage_policy: str,
    create_subnet_vars: dict[str, Any],
    region: str = "",
) -> dict[str, Any]:
    if source == "instance_validation":
        return dict(state.instance_validation.get("vars") or {})
    if source == "create_subnet":
        return dict(create_subnet_vars)
    if source == "security_group":
        return {
            "name": f"hc-sg-{suffix}",
            "vpc_id": vpc_id,
            "type": "ACL",
            "apply_to": [subnet_id] if subnet_id else [],
            "rules": [
                {
                    "direction": "INGRESS",
                    "protocol": "TCP",
                    "port_range": "22",
                    "sources": [config.env("HC_ADMIN_CIDR", "0.0.0.0/0")],
                    "action": "ALLOW",
                    "description": "health check ssh",
                },
                {
                    "direction": "INGRESS",
                    "protocol": "TCP",
                    "port_range": "3389",
                    "sources": [config.env("HC_ADMIN_CIDR", "0.0.0.0/0")],
                    "action": "ALLOW",
                    "description": "health check rdp",
                },
            ],
        }
    if source == "disk":
        return {
            "name": f"hc-disk-{suffix}",
            "size_gb": int(config.env("HC_DISK_SIZE_GB", "40")),
            "vpc_id": vpc_id,
            "storage_policy_id": storage_policy,
            "type": config.env("HC_DISK_TYPE", "EXTERNAL"),
            "instance_id": config.env("HC_INSTANCE_ID") or None,
        }
    if source == "additional_subnet":
        return {
            "name": f"hc-extra-net-{suffix}",
            "cidr": config.additional_subnet_cidr(),
            "gateway_ip": config.additional_subnet_gateway(),
            "type": config.env("HC_SUBNET_TYPE", "NAT_ROUTED"),
            "vpc_id": vpc_id,
        }
    if source == "object_storage":
        if region:
            bucket_prefix = config.object_storage_bucket_prefix()
            return {
                "bucket_name": f"{bucket_prefix}-{region.lower().replace('-', '')}-{suffix}",
                "region_name": region,
                "vpc_id": vpc_id,
                "acl": None,
                "versioning": "Enabled",
            }
        return {"region_name": "", "vpc_id": vpc_id}
    raise ValueError(f"Unknown runner_plan vars source: {source}")


def check_from_plan(
    item: TerraformCheckSpec,
    *,
    spec: dict[str, StageSpec],
    suffix: str,
    vpc_id: str,
    subnet_id: str,
    storage_policy: str,
    create_subnet_vars: dict[str, Any],
    region: str = "",
) -> Check | None:
    if item.stage not in spec:
        return None
    return spec_loader.check_from_spec(
        spec[item.stage],
        module=item.module,
        vars=vars_for_source(
            item.vars,
            suffix=suffix,
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            storage_policy=storage_policy,
            create_subnet_vars=create_subnet_vars,
            region=region,
        ),
        required_vars=item.required_vars,
        stop_group_on_success=item.stop_group_on_success,
    )


def build_terraform_checks(
    *,
    spec: dict[str, StageSpec],
    suffix: str,
    vpc_id: str,
    subnet_id: str,
    storage_policy: str,
    create_subnet_vars: dict[str, Any],
    backup_regions: list[str],
) -> list[Check]:
    object_regions = backup_regions or config.object_storage_regions()
    checks: list[Check] = []
    for item in load_terraform_checks():
        if item.per_region:
            for region in object_regions or [""]:
                check = check_from_plan(
                    item,
                    spec=spec,
                    suffix=suffix,
                    vpc_id=vpc_id,
                    subnet_id=subnet_id,
                    storage_policy=storage_policy,
                    create_subnet_vars=create_subnet_vars,
                    region=region,
                )
                if check:
                    checks.append(check)
            continue
        check = check_from_plan(
            item,
            spec=spec,
            suffix=suffix,
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            storage_policy=storage_policy,
            create_subnet_vars=create_subnet_vars,
        )
        if check:
            checks.append(check)
    return checks
