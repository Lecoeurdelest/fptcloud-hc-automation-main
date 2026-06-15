"""Instance cleanup and reclamation — retain-by-default, fail-closed.

No automatic deletion unless explicitly enabled and the tag/name + workspace
safety gates pass. Quota-exceeded never triggers automatic deletion (spec 06).
"""

from __future__ import annotations

from pathlib import Path

from hc.inventory.fptcloud_inventory import list_vpc_instances, select_oldest_reclaimable
from healthcheck import config, state
from healthcheck import terraform_executor as tf
from healthcheck.classification import QUOTA_CLEANUP_CLASSIFICATIONS
from healthcheck.logging import emit, queue_error
from healthcheck.reporting import cleanup_policy_summary


def cleanup_safety_errors(
    workspace: Path,
    *,
    classification: str,
    instance_id: str,
    expected_instance_name: str,
    resource_name: str,
) -> list[str]:
    errors: list[str] = []
    values = tf.instance_state_values(workspace)
    state_name = str(values.get("name") or "")
    if classification not in QUOTA_CLEANUP_CLASSIFICATIONS:
        errors.append("delete reason is not quota cleanup")
    if not instance_id:
        errors.append("instance_id is missing")
    if not values:
        errors.append("instance is not managed by the current run workspace")
    if state_name != expected_instance_name:
        errors.append("instance state name does not match current run workspace inputs")
    if state.INSTANCE_RUN_SUFFIX not in resource_name:
        errors.append("instance resource_name does not match current run suffix")
    if state.RUN_ROOT not in workspace.resolve().parents and workspace.resolve() != state.RUN_ROOT:
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
    instance_id = tf.instance_id_from_state(workspace)
    resources = tf.state_resources(workspace)
    if not config.cleanup_on_quota_exceeded_enabled():
        skipped = "HC_CLEANUP_ON_QUOTA_EXCEEDED is false"
        emit(
            f"{name}:cleanup",
            "skipped",
            f"Classification: instance_retained_by_policy; {cleanup_policy_summary(classification=classification, retained_instance_ids=[instance_id] if instance_id else [], skipped_delete_reason=skipped)}",
            resources,
        )
        emit(
            state.INSTANCE_CLEANUP_STAGE,
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
            state.INSTANCE_CLEANUP_STAGE,
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
        state.INSTANCE_CLEANUP_STAGE,
        "started",
        f"Destroying only current-run instance for quota cleanup; {cleanup_policy_summary(classification=classification, delete_allowed=True, delete_reason='quota cleanup')}",
        resources,
    )
    result = tf.run_instance_terraform(
        ["terraform", "destroy", "-auto-approve", "-no-color", "-input=false"],
        workspace,
        stage=f"{name}-cleanup",
    )
    if result.returncode == 0:
        emit(
            f"{name}:cleanup",
            "destroyed",
            f"Instance quota cleanup completed; {cleanup_policy_summary(classification=classification, delete_allowed=True, delete_reason='quota cleanup', deleted_instance_ids=[instance_id])}",
            resources,
        )
        emit(
            state.INSTANCE_CLEANUP_STAGE,
            "destroyed",
            f"Instance quota cleanup completed; {cleanup_policy_summary(classification=classification, delete_allowed=True, delete_reason='quota cleanup', deleted_instance_ids=[instance_id])}",
            resources,
        )
        return True
    queue_error(
        f"{name}:cleanup",
        workspace,
        resources,
        f"Classification: instance_cleanup_failed; {cleanup_policy_summary(classification=classification, retained_instance_ids=[instance_id] if instance_id else [], skipped_delete_reason='terraform destroy failed')}; {(result.stderr or result.stdout)[-1200:]}",
    )
    queue_error(
        state.INSTANCE_CLEANUP_STAGE,
        workspace,
        resources,
        f"Classification: instance_cleanup_failed; {cleanup_policy_summary(classification=classification, retained_instance_ids=[instance_id] if instance_id else [], skipped_delete_reason='terraform destroy failed')}; {(result.stderr or result.stdout)[-1200:]}",
    )
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
    retained_classification = (
        "retained_failed_instance" if failed else "instance_retained_by_policy"
    )
    skipped_reason = (
        "HC_CLEANUP_ON_QUOTA_EXCEEDED is false"
        if classification in QUOTA_CLEANUP_CLASSIFICATIONS
        and not config.cleanup_on_quota_exceeded_enabled()
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
        state.INSTANCE_CLEANUP_STAGE,
        "skipped",
        (
            f"Classification: {retained_classification}; os_label={label}; "
            f"{cleanup_policy_summary(classification=classification, retained_instance_ids=retained, skipped_delete_reason=skipped_reason)}"
        ),
        resources,
    )


def reclaim_health_check_instance(
    vpc_id: str,
    vpc_name: str,
    current_run_id: str,
) -> tuple[bool, str, str]:
    """Find and delete the oldest reclaimable HC instance in a VPC.

    Returns (success, instance_id, reason).
    Fail-closed: any ambiguity -> (False, "", reason).
    Governed by specs 7.2-7.3, FR-016, FR-017, NFR-013.
    """
    api_url = config.env("FPTCLOUD_API_URL") or ""
    token = config.env("FPTCLOUD_TOKEN") or ""
    stage = "compute.reclaim-health-check-instance"
    resources = [f"vpc:{vpc_id}"]

    if not api_url or not token:
        emit(
            stage,
            "failed",
            f"Classification: health_check_instance_not_found; "
            f"reason=missing_api_url_or_token; vpc_id={vpc_id}; vpc_name={vpc_name}; "
            f"no_deletion_performed=true",
            resources,
        )
        return False, "", "missing_api_url_or_token"

    emit(
        stage,
        "started",
        f"Listing HC instances in vpc_id={vpc_id}; vpc_name={vpc_name}; "
        f"current_run_id={current_run_id}; read_only_inventory_call=true",
        resources,
    )

    instances = list_vpc_instances(vpc_id, api_url, token)
    candidate = select_oldest_reclaimable(instances, current_run_id)

    if candidate is None:
        total = len(instances)
        hc_named = sum(1 for i in instances if i.is_hc_name())
        emit(
            stage,
            "failed",
            f"Classification: no_reclaimable_health_check_instance; "
            f"vpc_id={vpc_id}; vpc_name={vpc_name}; "
            f"total_instances={total}; hc_named_instances={hc_named}; "
            f"eligible_reclaimable=0; no_deletion_performed=true",
            resources,
        )
        return False, "", "no_reclaimable_health_check_instance"

    emit(
        stage,
        "started",
        f"Selected candidate for reclamation: instance_id={candidate.instance_id}; "
        f"name={candidate.name}; status={candidate.status}; "
        f"created_at={candidate.created_at}; os_label={candidate.os_label}; "
        f"vpc_id={vpc_id}; vpc_name={vpc_name}; "
        f"deletion_mechanism=terraform_import_destroy",
        resources,
    )

    reclaim_workspace = state.RUN_ROOT / "reclaim" / candidate.instance_id
    tf.write_import_workspace(candidate.instance_id, vpc_id, reclaim_workspace)

    success = tf.terraform_reclaim_import_destroy(
        candidate.instance_id, vpc_id, reclaim_workspace, stage_prefix=stage
    )
    if not success:
        return False, candidate.instance_id, "terraform_import_destroy_failed"

    emit(
        stage,
        "done",
        f"instance.deleted; instance_id={candidate.instance_id}; "
        f"name={candidate.name}; vpc_id={vpc_id}; vpc_name={vpc_name}; "
        f"deletion_mechanism=terraform_import_destroy; "
        f"tags_orphaned=true",
        resources,
    )
    return True, candidate.instance_id, ""
