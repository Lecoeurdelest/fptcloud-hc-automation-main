from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_health_checks.py"
SPEC = importlib.util.spec_from_file_location("run_health_checks", MODULE_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules["run_health_checks"] = runner
SPEC.loader.exec_module(runner)


pytestmark = pytest.mark.unit


def test_missing_vm_env_vars_are_preflight_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VPC_IDS", "vpc-a")
    check = runner.Check(
        name="vm",
        module="vm",
        vars={"storage_policy_id": "sp-a", "subnet_id": "subnet-a"},
        required_env=("VPC_IDS", "HC_IMAGE_NAME", "HC_FLAVOR_NAME", "HC_SSH_KEY"),
    )

    ok, reason = runner.preflight(check)

    assert not ok
    assert "HC_IMAGE_NAME" in reason
    assert "HC_FLAVOR_NAME" in reason
    assert "HC_SSH_KEY" in reason


def test_security_group_blocks_without_subnet() -> None:
    check = runner.Check(
        name="security-group",
        module="security_group",
        vars={"apply_to": []},
        required_vars=("apply_to",),
    )

    ok, reason = runner.preflight(check)

    assert not ok
    assert "apply_to" in reason


def test_security_group_reports_dependency_block() -> None:
    runner.stage_status.clear()
    check = runner.Check(
        name="security-group",
        module="security_group",
        vars={"apply_to": []},
        required_vars=("apply_to",),
        blocked_by=("network",),
    )

    ok, reason = runner.preflight(check)

    assert not ok
    assert "Blocked by incomplete dependency" in reason
    assert "network" in reason


def test_object_storage_without_enabled_region_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VPC_IDS", "vpc-a")
    monkeypatch.delenv("HC_ENABLED_OBJECT_REGIONS", raising=False)
    monkeypatch.delenv("HC_OBJECT_REGION", raising=False)

    check = runner.Check(
        name="backup",
        module="object_storage",
        vars={"region_name": "", "vpc_id": "vpc-a"},
        required_env=("VPC_IDS",),
        required_vars=("region_name",),
    )
    ok, reason = runner.preflight(check)

    assert not ok
    assert "No enabled object-storage region configured" in reason


def test_storage_policy_404_classification() -> None:
    message = "404 NOT FOUND Failed to retrieve storage policy"

    assert (
        runner.classify_error(message, "fptcloud_storage_policy")
        == "provider_endpoint_or_datasource_mismatch"
    )


def test_validate_subnet_inputs_rejects_gateway_outside_cidr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HC_VPC_CIDR", raising=False)

    valid, errors, warnings = runner.validate_subnet_inputs(
        {
            "name": "hc-net-test",
            "cidr": "172.26.222.0/24",
            "gateway_ip": "172.26.223.1",
            "type": "NAT_ROUTED",
            "vpc_id": "dc499fff-8a13-4d6a-b5f5-1d1e4ed04fce",
        }
    )

    assert not valid
    assert any("subnet_gateway must belong to subnet_cidr" in error for error in errors)
    assert any("Cannot validate overlap" in warning for warning in warnings)


def test_subnet_apply_failure_after_valid_inputs_gets_precise_classification() -> None:
    runner.stage_status.clear()
    runner.stage_status[runner.SUBNET_VALIDATION_STAGE] = "done"

    assert (
        runner.classify_error("Failed to create a new subnet: UnknownError", "module.subnet")
        == "provider_or_backend_system_error_after_valid_inputs"
    )


def test_provider_overlap_error_is_classified_as_subnet_cidr_overlap() -> None:
    message = (
        'HttpError: {"status":false,"error_code":"804007","data":null,'
        '"message":"The address 10.136.10.1/24 overlapped with address '
        '10.136.10.1/24 in Dungnt416Network subnet, FCI-L1-HAN-VPC vpc."}'
    )

    assert runner.classify_error(message, "module.subnet") == "subnet_cidr_overlap"


def test_overlap_report_includes_conflicting_subnet_name() -> None:
    message = (
        'Apply attempt 1 failed: HttpError: {"error_code":"804007",'
        '"message":"The address 10.136.10.1/24 overlapped with address '
        '10.136.10.1/24 in Dungnt416Network subnet, FCI-L1-HAN-VPC vpc."}'
    )
    context = runner.FailureContext(
        stage="network.additional-subnet",
        resource_type="module.subnet",
        address="module.this.fptcloud_subnet.this",
        module_path="modules/subnet",
        tenant="FCI-L1-ORG",
        region="VN/HAN",
        vpc_id="c9c1cfd6-7926-4a8a-96c6-cb4cd9d4aa19",
        reason=message,
        classification=runner.classify_error(message, "module.subnet"),
        attempted_cidr="10.136.10.0/24",
        attempted_gateway="10.136.10.1",
        conflicting_subnet=runner.conflicting_subnet_name(message),
    )

    report = runner.format_failure(context)

    assert "Classification: subnet_cidr_overlap" in report
    assert "Conflicting subnet: Dungnt416Network" in report
    assert "Attempted subnet CIDR: 10.136.10.0/24" in report
    assert "Attempted gateway: 10.136.10.1" in report
    assert "input/environment conflict" in report


def test_additional_subnet_spec_skips_when_cidr_or_gateway_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    runner.stage_status.clear()
    runner.stage_status[runner.VPC_DISCOVERY_STAGE] = "done"
    monkeypatch.delenv("HC_ADDITIONAL_SUBNET_CIDR", raising=False)
    monkeypatch.delenv("HC_ADDITIONAL_SUBNET_GATEWAY", raising=False)
    stage = runner.StageSpec(
        id="network.additional-subnet",
        manual_check_item="Create additional subnet from configured non-overlapping CIDR",
        automation_status="automated",
        required_inputs=("HC_ADDITIONAL_SUBNET_CIDR", "HC_ADDITIONAL_SUBNET_GATEWAY"),
        required_cloud_resources=("VPC",),
        expected_result="Temporary additional subnet is created.",
        validation_method="Terraform subnet module with explicit additional subnet inputs.",
        cleanup_behavior="Always run terraform destroy for the stage workspace.",
        dependency_stages=(runner.VPC_DISCOVERY_STAGE,),
        failure_classification="subnet_cidr_overlap",
        safe_for_daily_run=True,
    )

    check = runner.check_from_spec(
        stage,
        module="subnet",
        vars={"vpc_id": "c9c1cfd6-7926-4a8a-96c6-cb4cd9d4aa19", "cidr": "", "gateway_ip": ""},
        required_vars=("vpc_id", "cidr", "gateway_ip"),
    )

    assert check is None
    assert runner.events[-1]["stage"] == "network.additional-subnet"
    assert runner.events[-1]["status"] == "skipped"
    assert "HC_ADDITIONAL_SUBNET_CIDR" in runner.events[-1]["message"]
    assert "HC_ADDITIONAL_SUBNET_GATEWAY" in runner.events[-1]["message"]


def test_existing_subnet_inventory_selects_next_available_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    runner.existing_subnet_inventory.clear()
    for cidr in ("10.136.10.0/24", "10.136.20.0/24"):
        runner.existing_subnet_inventory.append(
            {
                "name": "operator-provided",
                "id": "",
                "cidr": cidr,
                "gateway": "",
                "vpc_id": "c9c1cfd6-7926-4a8a-96c6-cb4cd9d4aa19",
            }
        )
    monkeypatch.setenv("HC_ADDITIONAL_SUBNET_CIDR", "10.136.20.0/24")
    monkeypatch.setenv("HC_ADDITIONAL_SUBNET_GATEWAY", "10.136.20.1")

    selected, reason, _state = runner.select_additional_subnet_vars(
        {
            "cidr": "10.136.20.0/24",
            "gateway_ip": "10.136.20.1",
            "vpc_id": "c9c1cfd6-7926-4a8a-96c6-cb4cd9d4aa19",
        }
    )

    assert reason == ""
    assert selected["cidr"] == "10.136.30.0/24"
    assert selected["gateway_ip"] == "10.136.30.1"
    assert runner.events[-1]["stage"] == "network.select-additional-subnet-cidr"
    assert "total_attempts=2" in runner.events[-1]["message"]
    assert "rejected_cidrs=[10.136.20.0/24]" in runner.events[-1]["message"]


def test_candidate_selection_reports_exhaustion() -> None:
    selection = runner.select_additional_subnet_candidate(
        "10.136.10.0/24",
        "10.136.10.1",
        ["10.136.10.0/24", "10.136.20.0/24"],
        2,
    )

    assert selection.exhausted
    assert selection.candidate_attempt_count == 2
    assert selection.rejected_cidrs == ["10.136.10.0/24", "10.136.20.0/24"]


def test_provider_overlap_feedback_selects_next_candidate() -> None:
    state = runner.CandidateState(
        "10.136.20.0/24",
        "10.136.20.1",
        100,
        ("10.136.20.0/24",),
        ("preflight_inventory",),
        ("",),
    )
    state = runner.append_provider_overlap(state, "10.136.30.0/24", "subnet-testhc1-6206vn12")

    selected, reason, updated_state = runner.select_additional_subnet_vars(
        {
            "cidr": "10.136.30.0/24",
            "gateway_ip": "10.136.30.1",
            "vpc_id": "c9c1cfd6-7926-4a8a-96c6-cb4cd9d4aa19",
        },
        state,
    )

    assert reason == ""
    assert selected["cidr"] == "10.136.40.0/24"
    assert selected["gateway_ip"] == "10.136.40.1"
    assert updated_state.rejected_cidrs == ("10.136.20.0/24", "10.136.30.0/24")
    assert updated_state.conflict_sources == ("preflight_inventory", "provider_error")
    assert updated_state.conflicting_subnets[-1] == "subnet-testhc1-6206vn12"


def test_discover_existing_subnets_uses_configured_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    runner.stage_status.clear()
    runner.existing_subnet_inventory.clear()
    runner.stage_status[runner.VPC_DISCOVERY_STAGE] = "done"
    runner.run_context["effective_vpc_id"] = "c9c1cfd6-7926-4a8a-96c6-cb4cd9d4aa19"
    monkeypatch.setenv("HC_EXISTING_SUBNET_CIDRS", "10.136.10.0/24,10.136.20.0/24")
    stage = runner.StageSpec(
        id=runner.EXISTING_SUBNETS_STAGE,
        manual_check_item="Discover existing subnet inventory before additional subnet creation",
        automation_status="automated",
        required_inputs=(),
        required_cloud_resources=("Existing VPC",),
        expected_result="Existing subnet inventory is collected.",
        validation_method="Use provider/API listing or HC_EXISTING_SUBNET_CIDRS.",
        cleanup_behavior="No resources are created.",
        dependency_stages=(runner.VPC_DISCOVERY_STAGE,),
        failure_classification="subnet_cidr_overlap_preflight",
        safe_for_daily_run=True,
    )

    runner.discover_existing_subnets(stage)

    assert runner.stage_status[runner.EXISTING_SUBNETS_STAGE] == "done"
    assert [item["cidr"] for item in runner.existing_subnet_inventory] == [
        "10.136.10.0/24",
        "10.136.20.0/24",
    ]
    assert "HC_EXISTING_SUBNET_CIDRS" in runner.events[-1]["message"]
