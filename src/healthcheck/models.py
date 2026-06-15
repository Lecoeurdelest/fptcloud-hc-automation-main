"""Dataclasses shared across the health-check package.

Moved verbatim from scripts/run_health_checks.py to keep field names, defaults,
and frozen/mutable semantics identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
