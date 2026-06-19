"""Object storage live-run workflow: Terraform bucket + S3 probe object."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from healthcheck import classification, config, state
from healthcheck import terraform_executor as tf
from healthcheck.logging import emit, queue_error
from healthcheck.models import Check
from healthcheck.reporting import format_failure
from healthcheck.s3_client import S3Client, S3Error
from healthcheck.spec_loader import preflight


def execute_object_storage(check: Check) -> None:
    resources = ["module.this.fptcloud_object_storage_bucket.this"]
    ok, reason = preflight(check)
    if not ok:
        emit(check.name, "skipped", reason, resources)
        return

    missing_s3 = config.missing_s3_config()
    if missing_s3:
        emit(
            check.name,
            "skipped",
            (
                "Classification: s3_config_missing; "
                f"missing={','.join(missing_s3)}; Terraform apply not called"
            ),
            resources,
        )
        return

    workspace = tf.write_workspace(check)
    module_path = state.MODULES / check.module
    bucket = str(check.vars.get("bucket_name") or "")
    region = str(check.vars.get("region_name") or "")
    key = config.object_storage_test_key()
    try:
        with tf.ResourceLock(check.name):
            emit(
                f"{check.name}:inputs",
                "done",
                (
                    f"bucket_name={bucket}; region_name={region}; "
                    f"s3_endpoint_configured={bool(config.env('S3_ENDPOINT'))}; "
                    f"s3_region={config.env('S3_REGION') or '<unset>'}; "
                    f"s3_access_key_configured={bool(config.env('S3_ACCESS_KEY'))}; "
                    "s3_secret_key_configured=True; s3_secret_key_redacted=True; "
                    f"object_key={key}; terraform_vars={json.dumps(check.vars, sort_keys=True)}"
                ),
                resources,
            )
            emit(check.name, "started", "Initializing Terraform object storage workspace", resources)
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
                ["terraform", "plan", "-out=tfplan", "-detailed-exitcode", "-no-color", "-input=false"],
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
                return
            emit(check.name, "pending", "Plan completed; bucket will be created/updated", planned or resources)

            apply = tf.run(
                ["terraform", "apply", "-auto-approve", "-no-color", "-input=false", "tfplan"],
                workspace,
                stage=f"{check.name}-apply",
            )
            current = tf.state_resources(workspace) or planned or resources
            if apply.returncode != 0:
                context = classification.classify_context(
                    stage=check.name,
                    resource_type=f"module.{check.module}",
                    address=", ".join(current) or "terraform.apply",
                    module_path=module_path,
                    reason=(apply.stderr or apply.stdout)[-1200:],
                    vars=check.vars,
                )
                queue_error(check.name, workspace, current, format_failure(context))
                return
            emit(
                check.name,
                "passed",
                f"bucket.created; bucket_name={bucket}; region_name={region}; settling briefly",
                current,
            )
            time.sleep(state.SETTLE_SECONDS)

            s3 = S3Client(**config.s3_config())
            if not _run_s3_step(
                "object-storage.connect-s3",
                "s3_endpoint_unreachable",
                workspace,
                current,
                lambda: _head_bucket(s3, bucket, current),
            ):
                _destroy_bucket(workspace, check.name, current)
                return
            if not _run_s3_step(
                "object-storage.upload-file",
                "s3_object_upload_failed",
                workspace,
                current,
                lambda: _put_and_validate_object(s3, bucket, key, current),
            ):
                _best_effort_delete_object(s3, bucket, key, current)
                _destroy_bucket(workspace, check.name, current)
                return
            if not _run_s3_step(
                "object-storage.delete-file",
                "s3_object_delete_failed",
                workspace,
                current,
                lambda: _delete_and_validate_object(s3, bucket, key, current),
            ):
                _best_effort_delete_object(s3, bucket, key, current)
                _destroy_bucket(workspace, check.name, current)
                return
            _destroy_bucket(workspace, check.name, current)
    except RuntimeError as exc:
        queue_error(check.name, workspace, resources, str(exc))


def _run_s3_step(
    stage: str,
    error_class: str,
    workspace: Path,
    resources: list[str],
    callback: Callable[[], None],
) -> bool:
    try:
        callback()
        return True
    except S3Error as exc:
        queue_error(
            stage,
            workspace,
            resources,
            f"Classification: {error_class}; {exc}; status={exc.status}",
        )
        return False


def _head_bucket(s3: S3Client, bucket: str, resources: list[str]) -> None:
    emit(
        "object-storage.connect-s3",
        "started",
        f"bucket_name={bucket}; checking S3 endpoint reachability",
        resources,
    )
    response = s3.head_bucket(bucket)
    emit(
        "object-storage.connect-s3",
        "passed",
        f"bucket_name={bucket}; s3_endpoint_reachable=True; status={response.status}",
        resources,
    )


def _put_and_validate_object(s3: S3Client, bucket: str, key: str, resources: list[str]) -> None:
    body = config.object_storage_test_body()
    emit(
        "object-storage.upload-file",
        "started",
        f"bucket_name={bucket}; object_key={key}; bytes={len(body)}",
        resources,
    )
    put = s3.put_object(bucket, key, body)
    head = s3.head_object(bucket, key)
    emit(
        "object-storage.upload-file",
        "passed",
        (
            f"bucket_name={bucket}; object_key={key}; put_status={put.status}; "
            f"head_status={head.status}; object_exists=True"
        ),
        resources,
    )


def _delete_and_validate_object(s3: S3Client, bucket: str, key: str, resources: list[str]) -> None:
    emit(
        "object-storage.delete-file",
        "started",
        f"bucket_name={bucket}; object_key={key}",
        resources,
    )
    delete = s3.delete_object(bucket, key)
    absent = False
    status = 0
    try:
        s3.head_object(bucket, key)
    except S3Error as exc:
        status = exc.status
        absent = exc.status in {404, 405}
        if not absent:
            raise
    if not absent:
        raise S3Error(
            f"S3 HEAD /{bucket}/{key} still succeeded after delete",
            status=status,
        )
    emit(
        "object-storage.delete-file",
        "passed",
        (
            f"bucket_name={bucket}; object_key={key}; delete_status={delete.status}; "
            f"post_delete_head_status={status}; object_absent=True"
        ),
        resources,
    )


def _best_effort_delete_object(s3: S3Client, bucket: str, key: str, resources: list[str]) -> None:
    try:
        result = s3.delete_object(bucket, key)
        emit(
            "object-storage.delete-file",
            "cleanup",
            f"best_effort_delete_object=True; bucket_name={bucket}; object_key={key}; status={result.status}",
            resources,
        )
    except S3Error as exc:
        emit(
            "object-storage.delete-file",
            "cleanup_failed",
            f"best_effort_delete_object=False; bucket_name={bucket}; object_key={key}; status={exc.status}",
            resources,
        )


def _destroy_bucket(workspace: Path, stage: str, resources: list[str]) -> None:
    emit(
        "object-storage.delete-bucket",
        "started",
        "Destroying temporary object storage bucket with Terraform",
        resources,
    )
    result = tf.run(
        ["terraform", "destroy", "-auto-approve", "-no-color", "-input=false"],
        workspace,
        stage=f"{stage}-destroy",
    )
    if result.returncode != 0:
        queue_error(
            "object-storage.delete-bucket",
            workspace,
            resources,
            f"Classification: s3_object_validation_failed; terraform destroy failed; {(result.stderr or result.stdout)[-1200:]}",
        )
        return
    emit(
        "object-storage.delete-bucket",
        "destroyed",
        "Temporary object storage bucket destroyed",
        resources,
    )
