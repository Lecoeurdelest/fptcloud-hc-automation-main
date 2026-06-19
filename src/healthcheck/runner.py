"""High-level orchestration: stage selection, execution loop, and CLI.

Thin coordinator that wires the other modules together. Subnet input
validation/evidence and the generic + additional-subnet executors live here
because they are orchestration over the Terraform executor primitives.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import time
from pathlib import Path
from typing import Any

from diagnose_health_inputs import diagnostics as input_diagnostics
from diagnose_health_inputs import effective_config

from healthcheck import classification, config, discovery, instance_runner, object_storage_runner, spec_loader, stage_plan, state
from healthcheck import terraform_executor as tf
from healthcheck.logging import emit, now, queue_error, queue_pending, safe_name
from healthcheck.models import CandidateState, Check
from healthcheck.reporting import FILTER_CHOICES, format_failure, render_table


# ── Subnet input validation / evidence ────────────────────────────────────────
def subnet_vars(suffix: str) -> dict[str, Any]:
    return {
        "name": config.env("HC_SUBNET_NAME", f"hc-net-{suffix}"),
        "cidr": config.env("HC_SUBNET_CIDR", "172.26.222.0/24"),
        "gateway_ip": config.env("HC_SUBNET_GATEWAY", "172.26.222.1"),
        "type": config.env("HC_SUBNET_TYPE", "NAT_ROUTED"),
        "vpc_id": discovery.update_vpc_context()["effective_vpc_id"],
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
        warnings.append(
            "subnet_name is longer than 63 characters; provider/API name limits are not documented locally"
        )
    if name and not all(ch.isalnum() or ch in "-_" for ch in name):
        warnings.append(
            "subnet_name contains characters outside letters, digits, hyphen, and underscore"
        )

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
    elif discovery.vpc_identifier_type(vpc_id) != "uuid-shaped":
        warnings.append("VPC identifier is not UUID-shaped; provider docs call this field vpc_id")

    if subnet_type not in {"NAT_ROUTED", "ISOLATED"}:
        warnings.append(
            "subnet type is not one of provider-described values NAT_ROUTED or ISOLATED"
        )

    if not config.env("HC_VPC_CIDR"):
        warnings.append(
            "Cannot validate subnet CIDR is inside VPC CIDR because HC_VPC_CIDR is not configured"
        )
    else:
        try:
            vpc_network = ipaddress.ip_network(config.env("HC_VPC_CIDR"), strict=True)
            if network and not network.subnet_of(vpc_network):
                errors.append("subnet_cidr is not inside HC_VPC_CIDR")
        except ValueError as exc:
            errors.append(f"HC_VPC_CIDR is invalid: {exc}")

    warnings.append(
        "Cannot validate overlap with existing FPT Cloud subnets without a working subnet listing API"
    )
    warnings.append(
        "Cannot locally prove VPC region/tenant match; provider uses FPTCLOUD_REGION and FPTCLOUD_TENANT_NAME"
    )
    warnings.append(
        "Provider schema for fptcloud_subnet requires vpc_id, name, cidr, gateway_ip, and type; VPC ID flavor is not clarified beyond 'vpc id'"
    )
    return not errors, errors, warnings


def validate_subnet_stage(stage, vars: dict[str, Any]) -> None:
    ok, reason = spec_loader.spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["local.subnet-input-validation"])
        return
    valid, errors, warnings = validate_subnet_inputs(vars)
    details = [
        f"subnet_name={vars.get('name')}",
        f"subnet_cidr={vars.get('cidr')}",
        f"subnet_gateway={vars.get('gateway_ip')}",
        f"vpc_id={vars.get('vpc_id')}",
        f"vpc_identifier_type={discovery.vpc_identifier_type(str(vars.get('vpc_id') or ''))}",
        f"region={config.env('FPTCLOUD_REGION') or '<unset>'}",
        f"tenant={config.env('FPTCLOUD_TENANT_NAME') or '<unset>'}",
        f"terraform_vars={json.dumps(vars, sort_keys=True)}",
    ]
    if warnings:
        details.append(f"warnings={'; '.join(warnings)}")
    if valid:
        emit(
            stage.id,
            "done",
            "Local subnet input validation passed; " + "; ".join(details),
            ["local.subnet-input-validation"],
        )
    else:
        emit(
            stage.id,
            "failed",
            "Local subnet input validation failed: " + "; ".join(errors + details),
            ["local.subnet-input-validation"],
        )


def latest_create_subnet_error() -> dict[str, Any]:
    for run_dir in sorted(
        (state.ROOT / "runs").glob("hc-*"), key=lambda path: path.stat().st_mtime, reverse=True
    ):
        error_path = run_dir / "error_queue.json"
        if not error_path.exists():
            continue
        try:
            errors = json.loads(error_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for item in errors:
            if item.get("check") == state.SUBNET_CREATE_STAGE:
                reason = str(item.get("reason", ""))
                return {
                    "run": str(run_dir),
                    "workspace": item.get("workspace", ""),
                    "resources": item.get("resources", []),
                    "reason": reason,
                    "classification": (
                        "provider_or_backend_system_error_after_valid_inputs"
                        if "provider_or_backend_system_error_after_valid_inputs" in reason
                        else classification.classify_error(reason, "module.subnet")
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
    import zipfile

    if archive_path.exists():
        archive_path.unlink()
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if path.is_file() and path != archive_path:
                archive.write(path, path.relative_to(source.parent))


def collect_subnet_create_evidence(
    stage, vars: dict[str, Any], diagnostics_data: dict[str, Any]
) -> None:
    ok, reason = spec_loader.spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["provider-support-evidence"])
        return

    valid, errors, warnings = validate_subnet_inputs(vars)
    if not valid:
        emit(
            stage.id,
            "failed",
            "Evidence collection blocked by invalid local inputs: " + "; ".join(errors),
            ["provider-support-evidence"],
        )
        return

    check = Check(name=stage.id, module="subnet", vars=vars, required_vars=("vpc_id",))
    workspace = tf.write_workspace(check)
    evidence_dir = state.RUN_ROOT / "evidence" / safe_name(stage.id)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    init = tf.run(
        ["terraform", "init", "-input=false", "-no-color"],
        workspace,
        timeout=300,
        stage=f"{stage.id}-init",
    )
    plan = None
    show = None
    if init.returncode == 0:
        plan = tf.run(
            ["terraform", "plan", "-out=tfplan", "-detailed-exitcode", "-no-color", "-input=false"],
            workspace,
            timeout=300,
            stage=f"{stage.id}-plan",
        )
        if plan.returncode in (0, 2):
            show = tf.run(
                ["terraform", "show", "-json", "-no-color", "tfplan"],
                workspace,
                timeout=120,
                stage=f"{stage.id}-show-plan",
            )

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
        "terraform_module_path": str(state.MODULES / "subnet"),
        "sanitized_terraform_variables": vars,
        "validation": {"passed": True, "errors": errors, "warnings": warnings},
        "terraform": {
            "init_returncode": init.returncode,
            "plan_returncode": plan.returncode if plan else None,
            "show_plan_returncode": show.returncode if show else None,
            "plan_succeeded": bool(plan and plan.returncode in (0, 2)),
        },
        "latest_create_subnet_error": latest_error,
        "exact_error_classification": latest_error.get(
            "classification", "no_create_subnet_error_found"
        ),
    }
    (evidence_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (evidence_dir / "stage_events.json").write_text(
        json.dumps(state.events, indent=2), encoding="utf-8"
    )
    (evidence_dir / "input_diagnostics.json").write_text(
        json.dumps(diagnostics_data, indent=2), encoding="utf-8"
    )
    lock_file = Path(str(summary["terraform_provider_lock_file"]))
    if lock_file.exists():
        copy_tree(lock_file, evidence_dir / "terraform.lock.hcl")
    copy_tree(state.MODULES / "subnet", evidence_dir / "module_subnet")
    copy_tree(workspace / "logs", evidence_dir / "terraform_logs")
    for tf_log in workspace.glob("*.tf.log"):
        copy_tree(tf_log, evidence_dir / "tf_log" / tf_log.name)
    if latest_error.get("workspace"):
        latest_workspace = Path(str(latest_error["workspace"]))
        copy_tree(latest_workspace / "logs", evidence_dir / "latest_failed_apply_logs")
        for tf_log in latest_workspace.glob("*.tf.log"):
            copy_tree(tf_log, evidence_dir / "latest_failed_apply_tf_log" / tf_log.name)
    archive_path = state.RUN_ROOT / f"{safe_name(stage.id)}-evidence.zip"
    zip_directory(evidence_dir, archive_path)
    emit(
        stage.id,
        "done",
        f"Evidence bundle created at {archive_path}; plan_only=true; classification={summary['exact_error_classification']}",
        [str(archive_path)],
    )


# ── Executors ─────────────────────────────────────────────────────────────────
def execute(check: Check) -> None:
    if check.name == "network.additional-subnet":
        execute_additional_subnet(check)
        return
    if check.name == state.INSTANCE_CREATE_STAGE:
        instance_runner.execute_instance_create(check)
        return
    if check.name == "object-storage.bucket":
        object_storage_runner.execute_object_storage(check)
        return

    resources = [f"module.{check.module}"]
    ok, reason = spec_loader.preflight(check)
    if not ok:
        emit(check.name, "skipped", reason, resources)
        return

    workspace = tf.write_workspace(check)
    module_path = state.MODULES / check.module
    try:
        with tf.ResourceLock(check.name):
            if "vpc_id" in check.vars:
                emit(
                    f"{check.name}:context",
                    "done",
                    f"Using effective_vpc_id={check.vars.get('vpc_id') or '<unset>'}; vpc_id_source={state.run_context.get('vpc_id_source', 'unresolved')}",
                    resources,
                )
            emit(check.name, "started", "Initializing Terraform workspace", resources)
            init = tf.run(
                ["terraform", "init", "-input=false", "-no-color"],
                workspace,
                stage=f"{check.name}-init",
            )
            if init.returncode != 0:
                context = classification.classify_context(
                    stage=check.name,
                    resource_type=f"module.{check.module}",
                    address="terraform.init",
                    module_path=module_path,
                    reason=(init.stderr or init.stdout)[-1200:],
                    vars=check.vars,
                )
                queue_error(check.name, workspace, resources, format_failure(context))
                return

            plan = tf.run(
                [
                    "terraform",
                    "plan",
                    "-out=tfplan",
                    "-detailed-exitcode",
                    "-no-color",
                    "-input=false",
                ],
                workspace,
                stage=f"{check.name}-plan",
            )
            planned = tf.planned_resources(workspace)
            if plan.returncode not in (0, 2):
                context = classification.classify_context(
                    stage=check.name,
                    resource_type=f"module.{check.module}",
                    address=", ".join(planned) or "terraform.plan",
                    module_path=module_path,
                    reason=(plan.stderr or plan.stdout)[-1200:],
                    vars=check.vars,
                )
                queue_error(check.name, workspace, planned or resources, format_failure(context))
                tf.destroy(workspace, check.name)
                return
            emit(
                check.name,
                "pending",
                "Plan completed; resources will be created/updated",
                planned or resources,
            )
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
                        f"vpc_identifier_type={discovery.vpc_identifier_type(str(check.vars.get('vpc_id') or ''))}; "
                        f"region={config.env('FPTCLOUD_REGION') or '<unset>'}; "
                        f"tenant={config.env('FPTCLOUD_TENANT_NAME') or '<unset>'}; "
                        f"terraform_vars={json.dumps(check.vars, sort_keys=True)}"
                    ),
                    ["module.this.fptcloud_subnet.this"],
                )

            attempts = check.retries + 1
            for attempt in range(1, attempts + 1):
                apply = tf.run(
                    ["terraform", "apply", "-auto-approve", "-no-color", "-input=false", "tfplan"],
                    workspace,
                    stage=f"{check.name}-apply-attempt-{attempt}",
                )
                current = tf.state_resources(workspace) or planned or resources
                if apply.returncode == 0:
                    emit(
                        check.name,
                        "passed",
                        f"Apply succeeded on attempt {attempt}; settling briefly",
                        current,
                    )
                    time.sleep(state.SETTLE_SECONDS)
                    ready, reason, current = tf.readiness(workspace)
                    if ready:
                        emit(check.name, "ready", reason, current or planned or resources)
                    else:
                        queue_pending(
                            check.name, workspace, current or planned or resources, reason
                        )
                        instance_runner.wait_for_pending()
                    tf.destroy(workspace, check.name)
                    return

                raw_reason = (
                    f"Apply attempt {attempt} failed: {(apply.stderr or apply.stdout)[-1000:]}"
                )
                context = classification.classify_context(
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
            tf.destroy(workspace, check.name)
    except RuntimeError as exc:
        queue_error(check.name, workspace, resources, str(exc))


def execute_additional_subnet(check: Check) -> None:
    resources = [f"module.{check.module}"]
    ok, reason = spec_loader.preflight(check)
    if not ok:
        emit(check.name, "skipped", reason, resources)
        return

    module_path = state.MODULES / check.module
    candidate_state = CandidateState(
        str(check.vars.get("cidr") or ""),
        str(check.vars.get("gateway_ip") or ""),
        config.max_subnet_candidate_attempts(),
    )
    current_vars = dict(check.vars)
    workspace = state.RUN_ROOT / check.name
    try:
        with tf.ResourceLock(check.name):
            while True:
                current_vars, selection_error, candidate_state = (
                    discovery.select_additional_subnet_vars(current_vars, candidate_state)
                )
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
                workspace = tf.write_workspace(attempt_check)
                emit(
                    f"{check.name}:context",
                    "done",
                    f"Using effective_vpc_id={current_vars.get('vpc_id') or '<unset>'}; vpc_id_source={state.run_context.get('vpc_id_source', 'unresolved')}",
                    resources,
                )
                if (workspace / ".terraform").exists():
                    emit(
                        check.name, "started", "Reusing initialized Terraform workspace", resources
                    )
                else:
                    emit(check.name, "started", "Initializing Terraform workspace", resources)
                    init = tf.run(
                        ["terraform", "init", "-input=false", "-no-color"],
                        workspace,
                        stage=f"{check.name}-init",
                    )
                    if init.returncode != 0:
                        context = classification.classify_context(
                            stage=check.name,
                            resource_type=f"module.{check.module}",
                            address="terraform.init",
                            module_path=module_path,
                            reason=(init.stderr or init.stdout)[-1200:],
                            vars=current_vars,
                        )
                        queue_error(check.name, workspace, resources, format_failure(context))
                        return

                plan = tf.run(
                    [
                        "terraform",
                        "plan",
                        "-out=tfplan",
                        "-detailed-exitcode",
                        "-no-color",
                        "-input=false",
                    ],
                    workspace,
                    stage=f"{check.name}-plan",
                )
                planned = tf.planned_resources(workspace)
                if plan.returncode not in (0, 2):
                    context = classification.classify_context(
                        stage=check.name,
                        resource_type=f"module.{check.module}",
                        address=", ".join(planned) or "terraform.plan",
                        module_path=module_path,
                        reason=(plan.stderr or plan.stdout)[-1200:],
                        vars=current_vars,
                    )
                    queue_error(
                        check.name, workspace, planned or resources, format_failure(context)
                    )
                    tf.destroy(workspace, check.name)
                    return
                emit(
                    check.name,
                    "pending",
                    "Plan completed; resources will be created/updated",
                    planned or resources,
                )
                emit(
                    f"{check.name}:inputs",
                    "done",
                    (
                        "Subnet apply inputs: "
                        f"subnet_name={current_vars.get('name')}; "
                        f"subnet_cidr={current_vars.get('cidr')}; "
                        f"subnet_gateway={current_vars.get('gateway_ip')}; "
                        f"vpc_id={current_vars.get('vpc_id')}; "
                        f"vpc_identifier_type={discovery.vpc_identifier_type(str(current_vars.get('vpc_id') or ''))}; "
                        f"region={config.env('FPTCLOUD_REGION') or '<unset>'}; "
                        f"tenant={config.env('FPTCLOUD_TENANT_NAME') or '<unset>'}; "
                        f"terraform_vars={json.dumps(current_vars, sort_keys=True)}"
                    ),
                    ["module.this.fptcloud_subnet.this"],
                )

                apply = tf.run(
                    ["terraform", "apply", "-auto-approve", "-no-color", "-input=false", "tfplan"],
                    workspace,
                    stage=f"{check.name}-apply-attempt-{attempt_number}",
                )
                current = tf.state_resources(workspace) or planned or resources
                if apply.returncode == 0:
                    emit(
                        check.name,
                        "passed",
                        f"Apply succeeded on candidate attempt {attempt_number}; settling briefly",
                        current,
                    )
                    time.sleep(state.SETTLE_SECONDS)
                    ready, ready_reason, current = tf.readiness(workspace)
                    if ready:
                        emit(check.name, "ready", ready_reason, current or planned or resources)
                    else:
                        queue_pending(
                            check.name, workspace, current or planned or resources, ready_reason
                        )
                        instance_runner.wait_for_pending()
                    tf.destroy(workspace, check.name)
                    return

                raw_reason = f"Apply attempt {attempt_number} failed: {(apply.stderr or apply.stdout)[-1000:]}"
                context = classification.classify_context(
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
                    tf.destroy(workspace, check.name)
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
                tf.destroy(workspace, check.name)
                candidate_state = discovery.append_provider_overlap(
                    candidate_state, str(current_vars.get("cidr") or ""), context.conflicting_subnet
                )
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


# ── Stage assembly ────────────────────────────────────────────────────────────
def checks(stage_filter: str | None = None) -> list[Check]:
    suffix = state.RUN_ID.lower().replace("_", "-")
    spec = spec_loader.select_stages(spec_loader.load_spec(), stage_filter)
    config_eff = effective_config()
    discovery.discover_vpc(spec.get(state.VPC_DISCOVERY_STAGE))
    vpc_id = discovery.update_vpc_context()["effective_vpc_id"]
    storage_policy = config_eff["storage_policy_id"]
    subnet_id = config_eff["subnet_id"]
    state.run_context["effective_storage_policy_id"] = storage_policy
    state.run_context["storage_policy_id_source"] = (
        "explicit_id" if storage_policy else "unresolved"
    )
    state.run_context["effective_subnet_id"] = subnet_id
    state.run_context["subnet_id_source"] = "explicit_id" if subnet_id else "unresolved"
    create_subnet_vars = subnet_vars(suffix)

    validate_stage = spec.get(state.SUBNET_VALIDATION_STAGE)
    if validate_stage:
        validate_subnet_stage(validate_stage, create_subnet_vars)

    storage_policy = discovery.discover_storage_policy_stage(
        spec.get("compute.discover-storage-policy"), storage_policy, vpc_id
    )
    storage_policy = instance_runner.validate_instance_storage_policy_stage(
        spec.get(state.INSTANCE_STORAGE_POLICY_VALIDATE_STAGE), storage_policy
    )

    subnet_stage = spec.get("compute.discover-subnet")
    if subnet_stage and subnet_id:
        ok, reason = spec_loader.runnable_spec(subnet_stage)
        emit(
            subnet_stage.id,
            "done" if ok else "skipped",
            "Using explicit HC_SUBNET_ID; Terraform data-source lookup skipped" if ok else reason,
            ["HC_SUBNET_ID"],
        )
    elif subnet_stage:
        ok, reason = spec_loader.spec_preflight(subnet_stage)
        if not ok:
            emit(subnet_stage.id, "skipped", reason, ["data.fptcloud_subnet.this"])
        elif not vpc_id:
            emit(
                subnet_stage.id,
                "skipped",
                f"No VPC ID can be resolved for provider discovery; {discovery.vpc_diagnostics_message()}",
                ["data.fptcloud_subnet.this"],
            )
        else:
            emit(
                subnet_stage.id,
                "started",
                f"Using effective_vpc_id={vpc_id} for subnet discovery",
                ["data.fptcloud_subnet.this"],
            )
            subnet_id = discovery.discover_value(
                "subnet",
                "fptcloud_subnet",
                'try(data.fptcloud_subnet.this.subnets[0].id, "")',
                vpc_id,
                subnet_stage.id,
            )
            if subnet_id:
                state.stage_status[subnet_stage.id] = "done"
                state.run_context["effective_subnet_id"] = subnet_id
                state.run_context["subnet_id_source"] = "discovered"

    discovery.discover_instance_images(spec.get(state.INSTANCE_IMAGE_DISCOVERY_STAGE))
    discovery.discover_instance_flavor(spec.get(state.INSTANCE_FLAVOR_DISCOVERY_STAGE))
    discovery.discover_existing_subnets(spec.get(state.EXISTING_SUBNETS_STAGE))
    instance_runner.validate_instance_password_policy_stage(
        spec.get(state.INSTANCE_PASSWORD_POLICY_STAGE)
    )
    instance_runner.validate_instance_hostname_stage(
        spec.get(state.INSTANCE_HOSTNAME_VALIDATE_STAGE), suffix
    )
    instance_runner.validate_instance_stage(
        spec.get(state.INSTANCE_VALIDATE_STAGE), suffix, subnet_id, storage_policy
    )
    instance_runner.select_instance_round_stage(spec.get(state.INSTANCE_ROUND_SELECT_STAGE))
    instance_runner.validate_instance_network_stage(spec.get(state.INSTANCE_NETWORK_VALIDATE_STAGE))

    backup_regions = config.object_storage_regions()
    return stage_plan.build_terraform_checks(
        spec=spec,
        suffix=suffix,
        vpc_id=vpc_id,
        subnet_id=subnet_id,
        storage_policy=storage_policy,
        create_subnet_vars=create_subnet_vars,
        backup_regions=backup_regions,
    )


# ── Run / CLI ─────────────────────────────────────────────────────────────────
def run(stage_filter: str | None = None) -> None:
    if state.LOG_PATH.exists():
        state.LOG_PATH.unlink()
    if state.JSON_LOG_PATH.exists():
        state.JSON_LOG_PATH.unlink()
    if state.RUN_ROOT.exists():
        shutil.rmtree(state.RUN_ROOT)
    state.run_context.update(
        {
            "run_status": "running",
            "run_blocked": False,
            "user_action_required": False,
            "remaining_images_not_attempted": [],
        }
    )
    emit("run", "started", f"Health-check run {state.RUN_ID} started")
    emit(
        "environment",
        "done",
        (
            f"cwd={config.DOTENV_RESULT.get('cwd', str(Path.cwd()))}; "
            f"dotenv_path={config.DOTENV_RESULT.get('path', str(state.ROOT / '.env'))}; "
            f"dotenv_found={config.DOTENV_RESULT['found']}; "
            f"dotenv_loaded={bool(config.DOTENV_RESULT['loaded'])}; "
            f"dotenv_loaded_count={len(config.DOTENV_RESULT['loaded'])}; "
            f"dotenv_skipped_existing_count={len(config.DOTENV_RESULT['skipped_existing'])}; "
            f"{config.env_presence_report()}"
        ),
    )
    runtime_config = config.RUNTIME_CONFIG_RESULT
    emit(
        "runtime-config",
        "failed" if runtime_config.get("error") else "done",
        (
            f"path={runtime_config.get('path')}; found={runtime_config.get('found')}; "
            f"loaded={runtime_config.get('loaded')}; "
            f"error={runtime_config.get('error') or '<none>'}"
        ),
    )
    diagnostic_data = input_diagnostics()
    (state.RUN_ROOT / "input_diagnostics.json").write_text(
        json.dumps(diagnostic_data, indent=2),
        encoding="utf-8",
    )
    provider = diagnostic_data["provider"]
    context = diagnostic_data["provider_config"]
    network_vars = diagnostic_data["stage_inputs"]["network"]["vars"]
    config_eff = effective_config()
    discovery.update_vpc_context()
    emit(
        "diagnostics",
        "done",
        (
            f"Provider {provider.get('source')} {provider.get('version')} "
            f"from {provider.get('lock_file')}; "
            f"region={context.get('region')}; region_id={context.get('region_id') or '<unset>'}; "
            f"tenant={context.get('tenant_name')}; tenant_id={context.get('tenant_id') or '<unset>'}; "
            f"vpc_name={state.run_context.get('vpc_name') or '<unset>'}; "
            f"explicit_vpc_id={state.run_context.get('explicit_vpc_id') or '<unset>'}; "
            f"discovered_vpc_id={state.run_context.get('discovered_vpc_id') or '<unset>'}; "
            f"effective_vpc_id={state.run_context.get('effective_vpc_id') or '<unset>'}; "
            f"vpc_id_source={state.run_context.get('vpc_id_source') or 'unresolved'}; "
            f"subnet_name={network_vars.get('name')}; cidr={network_vars.get('cidr')}; "
            f"storage_policy_lookup_vpc={state.run_context.get('effective_vpc_id') or '<unresolved>'}; "
            f"subnet_id={config_eff.get('subnet_id') or '<unset>'}; "
            f"storage_policy_id={config_eff.get('storage_policy_id') or '<unset>'}"
        ),
    )
    completed_groups: set[str] = set()
    blocked = False
    for check in checks(stage_filter):
        if check.stop_group_on_success and check.stop_group_on_success in completed_groups:
            emit(
                check.name,
                "skipped",
                f"Skipped because {check.stop_group_on_success} already passed",
                [f"module.{check.module}"],
            )
            continue
        before = len(state.events)
        execute(check)
        if state.run_context.get("run_blocked"):
            blocked = True
            emit(
                "run",
                "blocked",
                (
                    f"Health-check run {state.RUN_ID} blocked; "
                    f"run_status=blocked_waiting_user_confirmation; "
                    f"quota_precheck=disabled; quota_assumption=assume_sufficient; "
                    f"quota_exceeded_action=stop_and_wait_for_user; "
                    f"stop_on_quota_exceeded=True; "
                    f"remaining_images_not_attempted={json.dumps(state.run_context.get('remaining_images_not_attempted') or [])}; "
                    f"user_action_required=True"
                ),
            )
            break
        if check.stop_group_on_success:
            new_events = state.events[before:]
            if any(
                event["stage"] == check.name and event["status"] == "passed" for event in new_events
            ):
                completed_groups.add(check.stop_group_on_success)
    if not blocked:
        instance_runner.wait_for_pending()
        emit("run", "done", f"Health-check run {state.RUN_ID} finished")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run spec-gated FPT Cloud health checks.")
    parser.add_argument("--stage", help="Run one stage ID from specs/health-check.json.")
    parser.add_argument(
        "--view", metavar="LOG_JSON", help="Path to a log.json file to render as a filtered table."
    )
    parser.add_argument(
        "--filter",
        dest="filter_mode",
        choices=FILTER_CHOICES,
        default="summary",
        help="Filter mode for --view (default: summary).",
    )
    args = parser.parse_args()

    if args.view:
        log_data = json.loads(Path(args.view).read_text(encoding="utf-8-sig"))
        print(render_table(log_data, args.filter_mode))
        return

    run(args.stage)
