"""Load and gate the health-check stage catalog (no-spec, no-implementation).

Parses specs/health-check.json into StageSpec objects and enforces the runtime
gates: automation status, daily-run safety, declared cleanup, required inputs,
and dependency completion.
"""

from __future__ import annotations

import json
from pathlib import Path

from healthcheck import config, state
from healthcheck.logging import emit, stage_ok
from healthcheck.models import Check, StageSpec


def load_spec(path: Path = state.SPEC_PATH) -> dict[str, StageSpec]:
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


def input_configured(requirement: str) -> bool:
    if requirement == "generated_instance_password":
        return bool(state.GENERATED_INSTANCE_PASSWORD)
    if requirement == "effective_vpc_id":
        return bool(state.run_context.get("effective_vpc_id"))
    if requirement == "effective_subnet_id":
        return bool(state.run_context.get("effective_subnet_id"))
    if requirement == "effective_storage_policy_id":
        return bool(state.run_context.get("effective_storage_policy_id"))
    if requirement == "discovered_instance_images":
        images = state.run_context.get("discovered_instance_images") or {}
        if config.env_bool_default("HC_REQUIRE_ALL_INSTANCE_IMAGES", False):
            return all(images.get(label) for label, _var_name in state.INSTANCE_IMAGE_MATRIX)
        return bool(images)
    if requirement == "discovered_instance_flavor":
        return bool(
            state.run_context.get("discovered_instance_flavor_name")
            or state.run_context.get("discovered_instance_flavor")
        )
    if requirement == "validated_instance_hostnames":
        return bool(state.run_context.get("validated_instance_hostnames"))
    if requirement == "selected_round_images":
        from healthcheck import instance_runner

        return bool(instance_runner.selected_round_labels())
    if requirement == "phases.object-storage.bucket.region":
        return bool(config.object_storage_regions())
    if requirement == "phases.network.additional-subnet.cidr":
        return bool(config.additional_subnet_cidr())
    if requirement == "phases.network.additional-subnet.gateway_ip":
        return bool(config.additional_subnet_gateway())
    if " or " in requirement:
        return any(input_configured(part.strip()) for part in requirement.split(" or "))
    return bool(config.env(requirement))


def preflight(check: Check) -> tuple[bool, str]:
    missing_env = [name for name in check.required_env if not config.env(name)]
    if missing_env:
        return False, f"Missing required environment values: {', '.join(missing_env)}"
    blocked = [name for name in check.blocked_by if not stage_ok(name)]
    if blocked:
        return False, f"Blocked by incomplete dependency stage(s): {', '.join(blocked)}"
    if check.module == "object_storage" and not check.vars.get("region_name"):
        return False, "No enabled object-storage region configured; set HC_ENABLED_OBJECT_REGIONS, HC_OBJECT_REGION, or phases.object-storage.bucket.region/enabled_regions"
    missing_vars = [
        name for name in check.required_vars if check.vars.get(name) in (None, "", [], {})
    ]
    if missing_vars:
        return False, f"Missing required preflight values: {', '.join(missing_vars)}"
    return True, "preflight passed"


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


def check_from_spec(
    stage: StageSpec,
    *,
    module: str,
    vars: dict,
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
