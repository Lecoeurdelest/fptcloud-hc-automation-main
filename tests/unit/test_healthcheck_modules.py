"""Per-module unit tests for the refactored ``healthcheck`` package.

These import each module directly (not through the ``run_health_checks`` facade)
to prove the split modules are independently testable, and to cover the
behaviours called out in the refactor request: spec validation/gating,
Premium-SSD exact-match storage-policy selection, optimistic-quota behaviour,
failure classification, JSON event logging, report filtering, and
retain-by-default/fail-closed cleanup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src/ and scripts/ are importable (mirrors healthcheck/__init__).
_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from healthcheck import (  # noqa: E402
    classification,
    cleanup,
    config,
    discovery,
    instance_runner,
    reporting,
    spec_loader,
    state,
)
from healthcheck import logging as hclog  # noqa: E402
from healthcheck import terraform_executor as tf  # noqa: E402
from healthcheck.models import StageSpec  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_run_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect log/queue writes to a tmp dir and reset shared mutable state."""
    monkeypatch.setattr(state, "RUN_ROOT", tmp_path / "run")
    monkeypatch.setattr(state, "JSON_LOG_PATH", tmp_path / "log.json")
    monkeypatch.setattr(state, "LOG_PATH", tmp_path / "log.html")
    state.events.clear()
    state.stage_status.clear()
    state.pending_queue.clear()
    state.error_queue.clear()
    state.existing_subnet_inventory.clear()


def _stage(stage_id: str, **overrides: object) -> StageSpec:
    base = dict(
        id=stage_id,
        manual_check_item="item",
        automation_status="automated",
        required_inputs=(),
        required_cloud_resources=(),
        expected_result="ok",
        validation_method="m",
        cleanup_behavior="No resources are created.",
        dependency_stages=(),
        failure_classification="unknown",
        safe_for_daily_run=True,
    )
    base.update(overrides)
    return StageSpec(**base)  # type: ignore[arg-type]


# ── classification ────────────────────────────────────────────────────────────
class TestClassification:
    def test_storage_policy_404(self) -> None:
        assert (
            classification.classify_error("404 NOT FOUND", "fptcloud_storage_policy")
            == "provider_endpoint_or_datasource_mismatch"
        )

    def test_instance_storage_quota(self) -> None:
        assert (
            classification.classify_error("Instance storage exceeds VPC quota.", "module.vm")
            == "instance_storage_quota_exceeded"
        )

    def test_subnet_overlap_804007(self) -> None:
        msg = '{"error_code":"804007","message":"overlapped"}'
        assert classification.classify_error(msg, "module.subnet") == "subnet_cidr_overlap"

    def test_is_quota_error(self) -> None:
        assert classification.is_quota_error("instance_storage_quota_exceeded")
        assert classification.is_quota_error("instance_quota_exceeded")
        assert not classification.is_quota_error("instance_provider_error")

    def test_conflicting_subnet_name(self) -> None:
        msg = "overlapped with address 10.0.0.1/24 in Dungnt416Network subnet, X vpc."
        assert classification.conflicting_subnet_name(msg) == "Dungnt416Network"


# ── config ──────────────────────────────────────────────────────────────────
class TestConfig:
    def test_env_bool_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HC_KEEP_INSTANCE", raising=False)
        assert config.env_bool_default("HC_KEEP_INSTANCE", True) is True
        monkeypatch.setenv("HC_KEEP_INSTANCE", "false")
        assert config.env_bool_default("HC_KEEP_INSTANCE", True) is False

    def test_disk_size_default_and_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HC_INSTANCE_DISK_SIZE_GB", raising=False)
        monkeypatch.delenv("HC_ROOT_DISK_SIZE", raising=False)
        size, error = config.root_disk_size()
        assert error == "" and size == 40
        monkeypatch.setenv("HC_INSTANCE_DISK_SIZE_GB", "20")
        size, error = config.root_disk_size()
        assert size == 20 and config.reduced_disk_test(size) is True

    def test_one_vm_per_apply_and_stop_on_quota(self) -> None:
        assert config.instances_per_apply() == 1
        assert config.stop_on_quota_exceeded_enabled() is True


# ── logging ───────────────────────────────────────────────────────────────────
class TestLogging:
    def test_emit_event_schema_and_json_is_source_of_truth(self) -> None:
        hclog.emit("compute.discover-vpc", "done", "os_label=ubuntu-22-04; attempt=2; detail")
        event = state.events[-1]
        assert set(event) == {
            "timestamp",
            "run_id",
            "stage",
            "status",
            "message",
            "details",
            "classification",
            "resource",
            "os_label",
            "attempt",
        }
        assert event["os_label"] == "ubuntu-22-04"
        assert event["attempt"] == 2
        # HTML/JSON are derived from the same events list.
        import json

        written = json.loads(state.JSON_LOG_PATH.read_text(encoding="utf-8"))
        assert written[-1]["stage"] == "compute.discover-vpc"
        assert state.LOG_PATH.exists()

    def test_status_class(self) -> None:
        assert hclog.status_class("passed") == "ok"
        assert hclog.status_class("blocked") == "warn"
        assert hclog.status_class("failed") == "error"
        assert hclog.status_class("weird") == "info"


# ── reporting ─────────────────────────────────────────────────────────────────
class TestReporting:
    EVENTS = [
        {"stage": "run", "status": "started", "details": "", "run_id": "r"},
        {
            "stage": "compute.create-instance",
            "status": "passed",
            "details": "instance_id=i-1",
            "run_id": "r",
        },
        {
            "stage": "compute.x:cleanup",
            "status": "skipped",
            "details": "retained_instance_ids=i-1",
            "run_id": "r",
        },
        {"stage": "compute.y", "status": "blocked", "details": "", "run_id": "r"},
        {"stage": "compute.z", "status": "queued", "details": "", "run_id": "r"},
    ]

    def test_filter_failed_includes_blocked(self) -> None:
        out = reporting.filter_events(self.EVENTS, "failed")
        assert {e["status"] for e in out} == {"blocked"}

    def test_filter_created_and_retained(self) -> None:
        assert (
            reporting.filter_events(self.EVENTS, "created_resources")[0]["stage"]
            == "compute.create-instance"
        )
        assert (
            reporting.filter_events(self.EVENTS, "retained_resources")[0]["stage"]
            == "compute.x:cleanup"
        )

    def test_filter_summary_drops_subevents(self) -> None:
        stages = {e["stage"] for e in reporting.filter_events(self.EVENTS, "summary")}
        assert "compute.x:cleanup" not in stages

    def test_redaction(self) -> None:
        red = reporting.redacted_vars({"password": "secret", "ssh_key": "k", "name": "vm"})
        assert (
            red["password"] == "<redacted>"
            and red["ssh_key"] == "<redacted>"
            and red["name"] == "vm"
        )


# ── spec_loader ───────────────────────────────────────────────────────────────
class TestSpecLoader:
    def test_runnable_rejects_non_automated(self) -> None:
        ok, reason = spec_loader.runnable_spec(_stage("x", automation_status="manual_only"))
        assert not ok and "not automated" in reason

    def test_runnable_rejects_unsafe(self) -> None:
        ok, reason = spec_loader.runnable_spec(_stage("x", safe_for_daily_run=False))
        assert not ok and "unsafe" in reason

    def test_runnable_rejects_cloud_resource_without_cleanup(self) -> None:
        ok, reason = spec_loader.runnable_spec(
            _stage("x", required_cloud_resources=("VPC",), cleanup_behavior="creates things")
        )
        assert not ok and "safe cleanup" in reason

    def test_spec_preflight_missing_inputs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HC_THING", raising=False)
        ok, reason = spec_loader.spec_preflight(_stage("x", required_inputs=("HC_THING",)))
        assert not ok and "HC_THING" in reason

    def test_select_stages_unknown_is_fatal(self) -> None:
        assert spec_loader.select_stages({}, "compute.create-instance") == {}
        assert state.events[-1]["stage"] == "spec" and state.events[-1]["status"] == "failed"


# ── discovery: Premium-SSD exact match ────────────────────────────────────────
class TestStoragePolicySelection:
    def test_exact_name_selects_premium_ssd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HC_INSTANCE_STORAGE_POLICY_NAME", "Premium-SSD")
        monkeypatch.setattr(discovery, "collected_storage_policies", list)
        state.run_context["discovered_storage_policies"] = [
            {"name": "Premium-SSD", "id": "sp-prem", "id_db": "db-prem"},
            {"name": "Premium-SSD-4000", "id": "sp-4000", "id_db": "db-4000"},
        ]
        selected = discovery.select_instance_storage_policy("")
        assert selected["name"] == "Premium-SSD"
        assert selected["id"] == "sp-prem"
        assert selected["provider_value"] == "sp-prem"
        assert selected["source"] == "preferred_exact_name"

    def test_exact_name_does_not_fall_back_to_premium_4000(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HC_INSTANCE_STORAGE_POLICY_NAME", "Premium-SSD")
        monkeypatch.setattr(discovery, "collected_storage_policies", list)
        state.run_context["discovered_storage_policies"] = [
            {"name": "Premium-SSD-4000", "id": "sp-4000", "id_db": "db-4000"},
        ]
        selected = discovery.select_instance_storage_policy("")
        assert selected["provider_value"] == ""
        assert selected["classification"] == "storage_policy_preferred_not_found"


# ── discovery: image / flavor / subnet math ───────────────────────────────────
class TestDiscoveryHelpers:
    def test_image_matches(self) -> None:
        assert discovery.image_matches(
            "windows-2012", {"name": "Windows Server 2012", "catalog": "Windows"}
        )
        assert not discovery.image_matches(
            "windows-2012", {"name": "Windows Server 2016", "catalog": "Windows"}
        )
        assert discovery.image_matches(
            "ubuntu-22-04", {"name": "Ubuntu 22.04", "catalog": "Ubuntu"}
        )

    def test_select_image_candidate_picks_last_sorted(self) -> None:
        images = [{"name": "Ubuntu 22.04 A"}, {"name": "Ubuntu 22.04 B"}]
        selected, names = discovery.select_image_candidate("ubuntu-22-04", images)
        assert selected == "Ubuntu 22.04 B" and len(names) == 2

    def test_flavor_matches_2c2g(self) -> None:
        assert discovery.flavor_matches(
            {"cpu": 2, "memory_mb": 2048, "type": "VM_SIZE", "gpu_memory_gb": 0}
        )
        assert not discovery.flavor_matches({"cpu": 1, "memory_mb": 1024})

    def test_subnet_candidate_exhaustion(self) -> None:
        selection = discovery.select_additional_subnet_candidate(
            "10.136.10.0/24", "10.136.10.1", ["10.136.10.0/24", "10.136.20.0/24"], 2
        )
        assert selection.exhausted
        assert selection.rejected_cidrs == ["10.136.10.0/24", "10.136.20.0/24"]

    def test_next_subnet_candidate_steps(self) -> None:
        assert discovery.next_subnet_candidate("10.136.10.0/24") == "10.136.20.0/24"


# ── instance_runner: optimistic quota + password policy ───────────────────────
class TestOptimisticQuota:
    def test_optimistic_quota_report_fields(self) -> None:
        report = instance_runner.optimistic_quota_report(40)
        assert report["quota_precheck"] == "disabled"
        assert report["quota_assumption"] == "assume_sufficient"
        assert report["quota_exceeded_action"] == "stop_and_wait_for_user"
        assert report["stop_on_quota_exceeded"] is True
        assert report["quota_status"] == "assumed_sufficient"

    def test_not_available_quota_report(self) -> None:
        report = instance_runner.not_available_quota_report(40)
        assert report["quota_status"] == "not_available"
        assert report["target_requested_disk_size_gb"] == 40

    def test_generated_password_satisfies_policy(self) -> None:
        result = instance_runner.password_policy_result(state.GENERATED_INSTANCE_PASSWORD)
        assert all(result.values())
        assert instance_runner.password_policy_valid(state.GENERATED_INSTANCE_PASSWORD)

    def test_password_policy_rejects_spaces(self) -> None:
        assert instance_runner.password_policy_valid("Aa1?bad pass") is False


# ── cleanup: retain-by-default, fail-closed ───────────────────────────────────
class TestCleanup:
    def test_cleanup_instance_retains_when_flag_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("HC_CLEANUP_ON_QUOTA_EXCEEDED", raising=False)
        monkeypatch.setattr(tf, "instance_id_from_state", lambda ws: "i-1")
        monkeypatch.setattr(
            tf, "state_resources", lambda ws: ["module.this.fptcloud_instance.this"]
        )
        deleted = cleanup.cleanup_instance(
            tmp_path,
            "compute.create-instance",
            classification="instance_storage_quota_exceeded",
            expected_instance_name="hcl-x",
            resource_name="hc-vm-x",
        )
        assert deleted is False
        assert any("instance_retained_by_policy" in e["message"] for e in state.events)

    def test_retain_instance_emits_retain(self) -> None:
        cleanup.retain_instance(
            "compute.create-instance",
            label="ubuntu-22-04",
            instance_id="i-1",
            classification="",
            failed=False,
            resources=["module.this.fptcloud_instance.this"],
        )
        assert any("cleanup_policy=retain_by_default" in e["details"] for e in state.events)
        assert any("instance_retained_by_policy" in e["message"] for e in state.events)
