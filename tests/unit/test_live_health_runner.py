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


def test_instance_storage_quota_classification() -> None:
    message = "Instance storage exceeds VPC quota. Please check again!"

    assert runner.classify_error(message, "module.vm") == "instance_storage_quota_exceeded"


def test_instance_storage_policy_name_selects_collected_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HC_INSTANCE_STORAGE_POLICY_NAME", "Premium-SSD")
    monkeypatch.delenv("HC_INSTANCE_STORAGE_POLICY_ID", raising=False)
    monkeypatch.delenv("HC_INSTANCE_STORAGE_POLICY_DB_ID", raising=False)
    monkeypatch.delenv("HC_INSTANCE_STORAGE_POLICY_PROVIDER_FIELD", raising=False)
    monkeypatch.setitem(runner.run_context, "discovered_storage_policies", [])

    selected = runner.select_instance_storage_policy("")

    assert selected["requested_name"] == "Premium-SSD"
    assert selected["name"] == "Premium-SSD"
    assert selected["id"] == "3f359ae7-b64c-4491-84df-7ab3899400a5"
    assert selected["id_db"] == "0334c678-d427-4654-beab-39067a145aca"
    assert selected["provider_value"] == "3f359ae7-b64c-4491-84df-7ab3899400a5"
    assert selected["provider_field_used"] == "storage_policy_id"
    assert selected["source"] == "preferred_exact_name"


def test_instance_storage_policy_db_id_is_debug_only_for_vm_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HC_INSTANCE_STORAGE_POLICY_ID", raising=False)
    monkeypatch.delenv("HC_INSTANCE_STORAGE_POLICY_NAME", raising=False)
    monkeypatch.setenv("HC_INSTANCE_STORAGE_POLICY_DB_ID", "0334c678-d427-4654-beab-39067a145aca")
    monkeypatch.setenv("HC_INSTANCE_STORAGE_POLICY_PROVIDER_FIELD", "db_id")
    monkeypatch.setitem(runner.run_context, "discovered_storage_policies", [])

    selected = runner.select_instance_storage_policy("")

    assert selected["name"] == "Premium-SSD"
    assert selected["id_db"] == "0334c678-d427-4654-beab-39067a145aca"
    assert selected["provider_value"] == "3f359ae7-b64c-4491-84df-7ab3899400a5"
    assert selected["provider_field_used"] == "storage_policy_id"


def test_instance_storage_policy_exact_name_does_not_select_premium_4000(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HC_INSTANCE_STORAGE_POLICY_NAME", "Premium-SSD")
    monkeypatch.setattr(
        runner,
        "collected_storage_policies",
        lambda: [
            {
                "name": "Premium-SSD-4000",
                "id": "56f82d58-dd3e-4d48-9606-d6bd52bdf96f",
                "id_db": "00305cf3-b33a-4172-8256-8f7380d67c3f",
            }
        ],
    )
    monkeypatch.setitem(runner.run_context, "discovered_storage_policies", [
        {
            "name": "Premium-SSD-4000",
            "id": "56f82d58-dd3e-4d48-9606-d6bd52bdf96f",
            "id_db": "00305cf3-b33a-4172-8256-8f7380d67c3f",
        }
    ])

    selected = runner.select_instance_storage_policy("")

    assert selected["provider_value"] == ""
    assert selected["name"] == ""
    assert selected["classification"] == "storage_policy_preferred_not_found"


def test_instance_storage_policy_prefers_required_collected_id_over_provider_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HC_INSTANCE_STORAGE_POLICY_NAME", "Premium-SSD")
    monkeypatch.setitem(runner.run_context, "discovered_storage_policies", [
        {
            "name": "Premium-SSD",
            "id": "9fe95650-d902-4613-ad5a-88c94c71d725",
            "id_db": "",
        }
    ])

    selected = runner.select_instance_storage_policy("")

    assert selected["name"] == "Premium-SSD"
    assert selected["id"] == "3f359ae7-b64c-4491-84df-7ab3899400a5"
    assert selected["id_db"] == "0334c678-d427-4654-beab-39067a145aca"
    assert selected["provider_value"] == "3f359ae7-b64c-4491-84df-7ab3899400a5"
    assert selected["source"] == "preferred_exact_name"


def test_validate_instance_storage_policy_reports_preferred_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    runner.stage_status.clear()
    monkeypatch.setenv("HC_INSTANCE_STORAGE_POLICY_NAME", "Premium-SSD")
    monkeypatch.setattr(
        runner,
        "collected_storage_policies",
        lambda: [
            {
                "name": "Premium-SSD-4000",
                "id": "56f82d58-dd3e-4d48-9606-d6bd52bdf96f",
                "id_db": "00305cf3-b33a-4172-8256-8f7380d67c3f",
            }
        ],
    )
    monkeypatch.setitem(runner.run_context, "discovered_storage_policies", [
        {
            "name": "Premium-SSD-4000",
            "id": "56f82d58-dd3e-4d48-9606-d6bd52bdf96f",
            "id_db": "00305cf3-b33a-4172-8256-8f7380d67c3f",
        }
    ])
    stage = runner.StageSpec(
        id=runner.INSTANCE_STORAGE_POLICY_VALIDATE_STAGE,
        manual_check_item="storage",
        automation_status="automated",
        required_inputs=("effective_storage_policy_id",),
        required_cloud_resources=("Existing storage policy",),
        expected_result="storage",
        validation_method="storage",
        cleanup_behavior="No resources are created.",
        dependency_stages=(),
        failure_classification="storage_policy_preferred_not_found",
        safe_for_daily_run=True,
    )

    selected = runner.validate_instance_storage_policy_stage(stage, "")

    assert selected == ""
    assert runner.events[-1]["status"] == "skipped"
    assert "Classification: storage_policy_preferred_not_found" in runner.events[-1]["message"]
    assert "storage_policy_requested=Premium-SSD" in runner.events[-1]["message"]
    assert "selected_storage_policy_name=<unresolved>" in runner.events[-1]["message"]


def test_validate_instance_inputs_use_premium_provider_id(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    set_matrix_env(monkeypatch)
    monkeypatch.setenv("HC_INSTANCE_STORAGE_POLICY_NAME", "Premium-SSD")
    monkeypatch.setitem(runner.run_context, "discovered_storage_policies", [])
    storage_stage = runner.StageSpec(
        id=runner.INSTANCE_STORAGE_POLICY_VALIDATE_STAGE,
        manual_check_item="storage",
        automation_status="automated",
        required_inputs=("effective_storage_policy_id",),
        required_cloud_resources=("Existing storage policy",),
        expected_result="storage",
        validation_method="storage",
        cleanup_behavior="No resources are created.",
        dependency_stages=(),
        failure_classification="storage_policy_preferred_not_found",
        safe_for_daily_run=True,
    )

    storage_policy_id = runner.validate_instance_storage_policy_stage(storage_stage, "")
    runner.validate_instance_stage(instance_stage(), "hc-test", "subnet-a", storage_policy_id)

    assert runner.instance_validation["valid"] is True
    first_instance = runner.instance_validation["vars"]["instances"][0]
    assert first_instance["vars"]["storage_policy_id"] == "3f359ae7-b64c-4491-84df-7ab3899400a5"
    assert runner.instance_validation["diagnostics"]["selected_storage_policy_id"] == "3f359ae7-b64c-4491-84df-7ab3899400a5"
    assert runner.instance_validation["diagnostics"]["selected_storage_policy_db_id"] == "0334c678-d427-4654-beab-39067a145aca"
    message = runner.events[-1]["message"]
    assert "storage_policy_requested=Premium-SSD" in message
    assert "storage_policy_source=preferred_exact_name" in message
    assert "provider_field_used=storage_policy_id" in message
    assert "quota_precheck=disabled" in message


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


def instance_stage(stage_id: str = runner.INSTANCE_VALIDATE_STAGE) -> runner.StageSpec:
    return runner.StageSpec(
        id=stage_id,
        manual_check_item="instance",
        automation_status="automated",
        required_inputs=("generated_instance_password", "HC_FLAVOR_NAME or HC_FLAVOR_ID", *[var_name for _label, var_name in runner.INSTANCE_IMAGE_MATRIX], "effective_vpc_id", "effective_subnet_id", "effective_storage_policy_id"),
        required_cloud_resources=("Existing VPC", "Existing subnet", "Existing storage policy"),
        expected_result="instance",
        validation_method="instance",
        cleanup_behavior="No resources are created." if stage_id == runner.INSTANCE_VALIDATE_STAGE else "Destroy the created instance.",
        dependency_stages=(runner.VPC_DISCOVERY_STAGE, "compute.discover-subnet", "compute.discover-storage-policy"),
        failure_classification="instance_missing_required_inputs",
        safe_for_daily_run=True,
    )


def prepare_instance_dependencies() -> None:
    runner.stage_status.clear()
    for stage in (runner.VPC_DISCOVERY_STAGE, "compute.discover-subnet", "compute.discover-storage-policy"):
        runner.stage_status[stage] = "done"
    runner.run_context["effective_vpc_id"] = "vpc-a"
    runner.run_context["effective_subnet_id"] = "subnet-a"
    runner.run_context["subnet_id_source"] = "discovered"
    runner.run_context["effective_storage_policy_id"] = "sp-a"
    runner.run_context["storage_policy_id_source"] = "discovered"
    runner.run_context["storage_policy_requested"] = ""
    runner.run_context["discovered_storage_policies"] = []
    runner.run_context["selected_storage_policy_name"] = ""
    runner.run_context["selected_storage_policy_id"] = ""
    runner.run_context["selected_storage_policy_db_id"] = ""
    runner.run_context["selected_storage_policy_provider_field_used"] = "storage_policy_id"
    runner.run_context["validated_instance_hostnames"] = {
        label: {
            "os_label": label,
            "resource_name": f"hc-vm-{label}-abc123def456",
            "guest_hostname": f"hcw{label[-4:]}abc123" if label.startswith("windows-") else f"hcl-{label}-abc123",
            "selected_hostname": f"hcw{label[-4:]}abc123" if label.startswith("windows-") else f"hcl-{label}-abc123",
            "hostname_length": len(f"hcw{label[-4:]}abc123" if label.startswith("windows-") else f"hcl-{label}-abc123"),
            "hostname_valid": True,
            "hostname_validation_status": "passed",
            "validation_errors": [],
        }
        for label, _var_name in runner.INSTANCE_IMAGE_MATRIX
    }
    runner.run_context["discovered_instance_images"] = {}
    runner.run_context["instance_image_sources"] = {}
    runner.run_context["discovered_instance_flavor_name"] = ""
    runner.run_context["discovered_instance_flavor"] = ""
    runner.run_context["instance_flavor_source"] = "unresolved"
    runner.run_context["instance_quota"] = {}
    runner.run_context["selected_instance_round"] = {}
    runner.run_context["run_status"] = "running"
    runner.run_context["run_blocked"] = False
    runner.run_context["user_action_required"] = False
    runner.run_context["remaining_images_not_attempted"] = []


def set_matrix_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for label, var_name in runner.INSTANCE_IMAGE_MATRIX:
        monkeypatch.setenv(var_name, f"image-{label}")
    monkeypatch.setenv("HC_FLAVOR_NAME", "2C2G")


def discovery_stage(stage_id: str) -> runner.StageSpec:
    return runner.StageSpec(
        id=stage_id,
        manual_check_item="discovery",
        automation_status="automated",
        required_inputs=("FPTCLOUD_TOKEN", "FPTCLOUD_REGION", "FPTCLOUD_TENANT_NAME", "effective_vpc_id"),
        required_cloud_resources=("Existing VPC",),
        expected_result="discovered",
        validation_method="data source only",
        cleanup_behavior="No resources are created.",
        dependency_stages=(runner.VPC_DISCOVERY_STAGE,),
        failure_classification="instance_image_unresolved" if stage_id == runner.INSTANCE_IMAGE_DISCOVERY_STAGE else "instance_flavor_unresolved",
        safe_for_daily_run=True,
    )


def test_discover_instance_images_resolves_matrix_from_provider_datasource(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    monkeypatch.setenv("FPTCLOUD_TOKEN", "token")
    monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
    monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "tenant")
    images = [
        {"name": "WINDOWS-SERVER-2012", "catalog": "Windows", "is_gpu": False},
        {"name": "WINDOWS-SERVER-2016", "catalog": "Windows", "is_gpu": False},
        {"name": "WINDOWS-SERVER-2019", "catalog": "Windows", "is_gpu": False},
        {"name": "WINDOWS-SERVER-2022", "catalog": "Windows", "is_gpu": False},
        {"name": "UBUNTU-16.04", "catalog": "Ubuntu", "is_gpu": False},
        {"name": "UBUNTU-18.04", "catalog": "Ubuntu", "is_gpu": False},
        {"name": "UBUNTU-20.04", "catalog": "Ubuntu", "is_gpu": False},
        {"name": "UBUNTU-22.04", "catalog": "Ubuntu", "is_gpu": False},
    ]
    monkeypatch.setattr(runner, "discover_data_collection", lambda *_args, **_kwargs: (images, ""))

    runner.discover_instance_images(discovery_stage(runner.INSTANCE_IMAGE_DISCOVERY_STAGE))

    assert runner.stage_status[runner.INSTANCE_IMAGE_DISCOVERY_STAGE] == "done"
    assert runner.run_context["discovered_instance_images"]["ubuntu-22-04"] == "UBUNTU-22.04"
    assert "provider_capability=supported" in runner.events[-1]["message"]
    assert "source=provider_datasource" in runner.events[-1]["message"]
    assert "resolved_count=8" in runner.events[-1]["message"]


def test_discover_instance_flavor_resolves_2c2g_from_provider_datasource(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    monkeypatch.setenv("FPTCLOUD_TOKEN", "token")
    monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
    monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "tenant")
    monkeypatch.delenv("HC_FLAVOR_NAME", raising=False)
    monkeypatch.delenv("HC_FLAVOR_ID", raising=False)
    flavors = [
        {"name": "1C1G", "id": "small", "cpu": 1, "memory_mb": 1024, "type": "VM_SIZE", "gpu_memory_gb": 0},
        {"name": "2C2G", "id": "target", "cpu": 2, "memory_mb": 2048, "type": "VM_SIZE", "gpu_memory_gb": 0},
    ]
    monkeypatch.setattr(runner, "discover_data_collection", lambda *_args, **_kwargs: (flavors, ""))

    runner.discover_instance_flavor(discovery_stage(runner.INSTANCE_FLAVOR_DISCOVERY_STAGE))

    assert runner.stage_status[runner.INSTANCE_FLAVOR_DISCOVERY_STAGE] == "done"
    assert runner.run_context["discovered_instance_flavor_name"] == "2C2G"
    assert "flavor_status=resolved" in runner.events[-1]["message"]
    assert "source=provider_datasource" in runner.events[-1]["message"]


def test_discover_instance_images_reports_partial_with_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    monkeypatch.setenv("FPTCLOUD_TOKEN", "token")
    monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
    monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "tenant")
    monkeypatch.setenv("HC_REQUIRE_ALL_INSTANCE_IMAGES", "true")
    images = [
        {"name": "Windows Server 2012 Standard", "catalog": "Windows", "is_gpu": False},
        {"name": "Windows Server 2016 Standard", "catalog": "Windows", "is_gpu": False},
        {"name": "Windows Server 2019 Standard", "catalog": "Windows", "is_gpu": False},
        {"name": "Windows Server 2022 Standard", "catalog": "Windows", "is_gpu": False},
        {"name": "Ubuntu-20-04", "catalog": "Ubuntu", "is_gpu": False},
        {"name": "ubuntu-22.04", "catalog": "Ubuntu", "is_gpu": False},
    ]
    monkeypatch.setattr(runner, "discover_data_collection", lambda *_args, **_kwargs: (images, ""))

    runner.discover_instance_images(discovery_stage(runner.INSTANCE_IMAGE_DISCOVERY_STAGE))

    assert runner.events[-1]["status"] == "skipped"
    assert "resolution_status=partial" in runner.events[-1]["message"]
    assert "resolved_count=6" in runner.events[-1]["message"]
    assert "unresolved_count=2" in runner.events[-1]["message"]
    assert "image_unavailable_in_region" in runner.events[-1]["message"]
    assert "Ubuntu-20-04" in runner.events[-1]["message"]


def test_partial_images_can_validate_when_require_all_false(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    monkeypatch.setenv("HC_REQUIRE_ALL_INSTANCE_IMAGES", "false")
    monkeypatch.setenv("HC_INSTANCE_PASSWORD", "super-secret")
    runner.run_context["discovered_instance_images"] = {"ubuntu-20-04": "Ubuntu-20-04"}
    runner.run_context["instance_image_sources"] = {"ubuntu-20-04": "provider_datasource"}
    runner.run_context["discovered_instance_flavor_name"] = "Medium-2"
    runner.run_context["instance_flavor_source"] = "provider_datasource"

    vars, _diagnostics, errors = runner.instance_base_inputs("hc-test", "subnet-a", "sp-a")

    assert errors == []
    assert len(vars["instances"]) == 1


def test_matrix_instance_name_uses_12_char_lowercase_suffix() -> None:
    name = runner.matrix_instance_name("windows-2022", "ignored")
    suffix = name.rsplit("-", 1)[-1]

    assert len(suffix) == 12
    assert suffix.isalnum()
    assert suffix == suffix.lower()


def test_hostname_validation_generates_windows_safe_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    runner.run_context["discovered_instance_images"] = {"windows-2012": "Windows Server 2012 Standard"}
    monkeypatch.setenv("HC_REQUIRE_ALL_INSTANCE_IMAGES", "false")
    stage = runner.StageSpec(
        id=runner.INSTANCE_HOSTNAME_VALIDATE_STAGE,
        manual_check_item="hostname",
        automation_status="automated",
        required_inputs=("discovered_instance_images",),
        required_cloud_resources=(),
        expected_result="hostname",
        validation_method="hostname",
        cleanup_behavior="No resources are created.",
        dependency_stages=(runner.INSTANCE_IMAGE_DISCOVERY_STAGE,),
        failure_classification="instance_hostname_invalid",
        safe_for_daily_run=True,
    )
    runner.stage_status[runner.INSTANCE_IMAGE_DISCOVERY_STAGE] = "done"

    runner.validate_instance_hostname_stage(stage, "hc-test")

    entry = runner.run_context["validated_instance_hostnames"]["windows-2012"]
    assert entry["resource_name"].startswith("hc-vm-windows-2012-")
    assert entry["guest_hostname"].startswith("hcw2012")
    assert entry["hostname_length"] <= 15
    assert entry["hostname_valid"] is True
    assert "resource_name=" in runner.events[-1]["message"]
    assert "guest_hostname=" in runner.events[-1]["message"]


def test_instance_inputs_use_guest_hostname_as_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare_instance_dependencies()
    monkeypatch.setenv("HC_REQUIRE_ALL_INSTANCE_IMAGES", "false")
    runner.run_context["discovered_instance_images"] = {"windows-2012": "Windows Server 2012 Standard"}
    runner.run_context["discovered_instance_flavor_name"] = "Medium-2"
    runner.run_context["instance_flavor_source"] = "provider_datasource"
    runner.run_context["validated_instance_hostnames"] = {
        "windows-2012": {
            "os_label": "windows-2012",
            "resource_name": "hc-vm-windows-2012-abc123def456",
            "guest_hostname": "hcw2012a1b2c3",
            "selected_hostname": "hcw2012a1b2c3",
            "hostname_length": 13,
            "hostname_valid": True,
            "hostname_validation_status": "passed",
            "validation_errors": [],
        }
    }

    vars, diagnostics, errors = runner.instance_base_inputs("hc-test", "subnet-a", "sp-a")

    assert errors == []
    assert vars["instances"][0]["resource_name"] == "hc-vm-windows-2012-abc123def456"
    assert vars["instances"][0]["vars"]["name"] == "hcw2012a1b2c3"
    assert diagnostics["guest_hostname"] == "hcw2012a1b2c3"


def test_network_validation_blocks_bare_instance() -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    runner.instance_validation.update(
        {
            "valid": True,
            "diagnostics": {},
            "vars": {
                "instances": [
                    {
                        "label": "ubuntu-22-04",
                        "vars": {
                            "name": "hc-vm-ubuntu-22-04-abc123def456",
                            "vpc_id": "vpc-a",
                            "subnet_id": "",
                            "storage_policy_id": "sp-a",
                        },
                    }
                ]
            },
            "errors": [],
        }
    )
    stage = runner.StageSpec(
        id=runner.INSTANCE_NETWORK_VALIDATE_STAGE,
        manual_check_item="network",
        automation_status="automated",
        required_inputs=("effective_vpc_id", "effective_subnet_id", "effective_storage_policy_id"),
        required_cloud_resources=("Existing VPC",),
        expected_result="network",
        validation_method="network",
        cleanup_behavior="No resources are created.",
        dependency_stages=(runner.VPC_DISCOVERY_STAGE, "compute.discover-subnet", "compute.discover-storage-policy", runner.INSTANCE_VALIDATE_STAGE),
        failure_classification="instance_network_attachment_missing",
        safe_for_daily_run=True,
    )
    runner.stage_status[runner.INSTANCE_VALIDATE_STAGE] = "done"

    runner.validate_instance_network_stage(stage)

    assert runner.events[-1]["stage"] == runner.INSTANCE_NETWORK_VALIDATE_STAGE
    assert "instance_network_attachment_missing" in runner.events[-1]["message"]
    assert "subnet_id network attachment is required" in runner.events[-1]["message"]


def test_network_validation_passes_with_vpc_and_subnet_attachment() -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    runner.instance_validation.update(
        {
            "valid": True,
            "diagnostics": {},
            "vars": {
                "instances": [
                    {
                        "label": "ubuntu-22-04",
                        "vars": {
                            "name": "hc-vm-ubuntu-22-04-abc123def456",
                            "vpc_id": "vpc-a",
                            "subnet_id": "subnet-a",
                            "storage_policy_id": "sp-a",
                            "security_group_ids": [],
                        },
                    }
                ]
            },
            "errors": [],
        }
    )
    stage = runner.StageSpec(
        id=runner.INSTANCE_NETWORK_VALIDATE_STAGE,
        manual_check_item="network",
        automation_status="automated",
        required_inputs=("effective_vpc_id", "effective_subnet_id", "effective_storage_policy_id"),
        required_cloud_resources=("Existing VPC",),
        expected_result="network",
        validation_method="network",
        cleanup_behavior="No resources are created.",
        dependency_stages=(runner.VPC_DISCOVERY_STAGE, "compute.discover-subnet", "compute.discover-storage-policy", runner.INSTANCE_VALIDATE_STAGE),
        failure_classification="instance_network_attachment_missing",
        safe_for_daily_run=True,
    )
    runner.stage_status[runner.INSTANCE_VALIDATE_STAGE] = "done"

    runner.validate_instance_network_stage(stage)

    assert runner.stage_status[runner.INSTANCE_NETWORK_VALIDATE_STAGE] == "done"
    assert "network_attachment_fields=vpc_id,subnet_id" in runner.events[-1]["message"]
    assert "connection_test_policy=manual_verification_required" in runner.events[-1]["message"]


def test_missing_image_skips_instance_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    set_matrix_env(monkeypatch)
    monkeypatch.setenv("HC_REQUIRE_ALL_INSTANCE_IMAGES", "true")
    monkeypatch.delenv("HC_IMAGE_WINDOWS_2012", raising=False)

    runner.validate_instance_stage(instance_stage(), "hc-test", "subnet-a", "sp-a")

    assert runner.instance_validation["valid"] is False
    assert runner.events[-1]["stage"] == runner.INSTANCE_VALIDATE_STAGE
    assert "instance_image_unresolved" in runner.events[-1]["message"]


def test_missing_flavor_skips_instance_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    set_matrix_env(monkeypatch)
    monkeypatch.delenv("HC_FLAVOR_NAME", raising=False)
    monkeypatch.delenv("HC_FLAVOR_ID", raising=False)

    runner.validate_instance_stage(instance_stage(), "hc-test", "subnet-a", "sp-a")

    assert runner.instance_validation["valid"] is False
    assert "instance_flavor_unresolved" in runner.events[-1]["message"]


def test_generated_password_is_reported_without_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    set_matrix_env(monkeypatch)

    runner.validate_instance_stage(instance_stage(), "hc-test", "subnet-a", "sp-a")

    assert runner.instance_validation["valid"] is True
    assert "password_generated=True" in runner.events[-1]["message"]
    assert runner.GENERATED_INSTANCE_PASSWORD not in runner.events[-1]["message"]


def test_generated_password_satisfies_provider_policy() -> None:
    password = runner.GENERATED_INSTANCE_PASSWORD
    result = runner.password_policy_result(password)

    assert len(password) >= 12
    assert " " not in password
    assert any(char.isupper() for char in password)
    assert any(char.islower() for char in password)
    assert any(char.isdigit() for char in password)
    assert any(char in runner.PASSWORD_SPECIALS for char in password)
    assert result["no_disallowed_specials"] is True
    assert runner.password_policy_valid(password) is True


def test_password_policy_rejects_spaces_and_disallowed_specials() -> None:
    result = runner.password_policy_result("Aa1?bad_password ")

    assert result["no_spaces"] is False
    assert result["no_disallowed_specials"] is False
    assert runner.password_policy_valid("Aa1?bad_password ") is False


def test_password_policy_stage_reports_only_booleans(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    monkeypatch.setattr(runner, "GENERATED_INSTANCE_PASSWORD", "Aa1?Bb2@Cc3#")
    stage = runner.StageSpec(
        id=runner.INSTANCE_PASSWORD_POLICY_STAGE,
        manual_check_item="password",
        automation_status="automated",
        required_inputs=("generated_instance_password",),
        required_cloud_resources=(),
        expected_result="password",
        validation_method="password",
        cleanup_behavior="No resources are created.",
        dependency_stages=(),
        failure_classification="instance_password_policy_invalid",
        safe_for_daily_run=True,
    )

    runner.validate_instance_password_policy_stage(stage)

    message = runner.events[-1]["message"]
    assert runner.events[-1]["status"] == "done"
    assert "password_generated=True" in message
    assert "password_redacted=True" in message
    assert "length_ok=True" in message
    assert "no_spaces=True" in message
    assert "uppercase_present=True" in message
    assert "lowercase_present=True" in message
    assert "number_present=True" in message
    assert "allowed_special_present=True" in message
    assert "no_disallowed_specials=True" in message
    assert "Aa1?Bb2@Cc3#" not in message


def test_missing_subnet_or_storage_policy_skips_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    set_matrix_env(monkeypatch)

    runner.validate_instance_stage(instance_stage(), "hc-test", "", "")

    assert runner.instance_validation["valid"] is False
    assert "effective_subnet_id is required" in runner.events[-1]["message"]
    assert "effective_storage_policy_id is required" in runner.events[-1]["message"]


def quota_stage(stage_id: str) -> runner.StageSpec:
    return runner.StageSpec(
        id=stage_id,
        manual_check_item="quota",
        automation_status="automated",
        required_inputs=("effective_vpc_id", "effective_storage_policy_id"),
        required_cloud_resources=("Existing VPC", "Existing storage policy"),
        expected_result="quota",
        validation_method="quota",
        cleanup_behavior="No resources are created.",
        dependency_stages=(),
        failure_classification="instance_storage_quota_exceeded_preflight",
        safe_for_daily_run=True,
    )


def test_quota_inspection_reports_assumption_without_supported_source(monkeypatch: pytest.MonkeyPatch) -> None:
    runner.events.clear()
    runner.run_context["effective_vpc_id"] = "vpc-a"
    runner.run_context["effective_storage_policy_id"] = "sp-a"
    monkeypatch.delenv("HC_QUOTA_EXPORT_JSON", raising=False)
    monkeypatch.setattr(runner, "inspect_provider_quota_capabilities", lambda: pytest.fail("quota precheck must not inspect provider schema"))

    runner.inspect_instance_quota_stage(quota_stage(runner.INSTANCE_QUOTA_INSPECT_STAGE))

    assert runner.stage_status[runner.INSTANCE_QUOTA_INSPECT_STAGE] == "done"
    assert runner.run_context["instance_quota"]["quota_status"] == "assumed_sufficient"
    assert "quota_precheck=disabled" in runner.events[-1]["message"]
    assert "quota_assumption=assume_sufficient" in runner.events[-1]["message"]
    assert "target_requested_disk_size_gb=40" in runner.events[-1]["message"]


def test_quota_validation_does_not_skip_apply_when_remaining_storage_is_insufficient() -> None:
    runner.events.clear()
    runner.stage_status.clear()
    runner.stage_status[runner.INSTANCE_QUOTA_INSPECT_STAGE] = "done"
    runner.stage_status[runner.INSTANCE_VALIDATE_STAGE] = "done"
    runner.run_context["effective_vpc_id"] = "vpc-a"
    runner.run_context["effective_storage_policy_id"] = "sp-a"
    runner.run_context["instance_quota"] = {
        **runner.not_available_quota_report(40),
        "quota_source": "HC_QUOTA_EXPORT_JSON",
        "quota_status": "available",
        "remaining_storage_gb": 20,
        "target_requested_disk_size_gb": 40,
    }
    runner.instance_validation.update(
        {
            "valid": True,
            "vars": {"instances": [{"label": "windows-2012", "vars": {"disk_gb": 40}}]},
            "diagnostics": {},
            "errors": [],
        }
    )
    stage = quota_stage(runner.INSTANCE_QUOTA_VALIDATE_STAGE)
    stage = runner.StageSpec(
        **{
            **stage.__dict__,
            "dependency_stages": (runner.INSTANCE_QUOTA_INSPECT_STAGE, runner.INSTANCE_VALIDATE_STAGE),
        }
    )

    runner.validate_instance_quota_stage(stage)

    assert runner.events[-1]["stage"] == runner.INSTANCE_QUOTA_VALIDATE_STAGE
    assert runner.events[-1]["status"] == "done"
    assert "preflight_decision=disabled_allow_apply" in runner.events[-1]["message"]
    assert "quota_precheck=disabled" in runner.events[-1]["message"]
    assert "quota_assumption=assume_sufficient" in runner.events[-1]["message"]


def test_create_instance_spec_does_not_depend_on_quota_precheck_stages() -> None:
    spec = runner.load_spec()
    create_stage = spec[runner.INSTANCE_CREATE_STAGE]

    assert runner.INSTANCE_QUOTA_INSPECT_STAGE not in create_stage.dependency_stages
    assert runner.INSTANCE_QUOTA_VALIDATE_STAGE not in create_stage.dependency_stages


def test_instance_disk_size_override_marks_reduced_disk_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HC_INSTANCE_DISK_SIZE_GB", "20")
    monkeypatch.setenv("HC_ROOT_DISK_SIZE", "40")

    disk_size, error = runner.root_disk_size()

    assert error == ""
    assert disk_size == 20
    assert runner.root_disk_size_source() == "HC_INSTANCE_DISK_SIZE_GB"
    assert runner.reduced_disk_test(disk_size)


def test_instance_diagnostics_redact_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare_instance_dependencies()
    set_matrix_env(monkeypatch)
    monkeypatch.setenv("HC_SSH_KEY", "secret-key")

    vars, _diagnostics, errors = runner.instance_base_inputs("hc-test", "subnet-a", "sp-a")
    redacted = runner.redacted_vars(vars["instances"][0]["vars"])
    nested_redacted = runner.redacted_vars(vars)

    assert errors == []
    assert redacted["ssh_key"] == "<redacted>"
    assert redacted["password"] == "<redacted>"
    assert "secret-key" not in str(redacted)
    assert runner.GENERATED_INSTANCE_PASSWORD not in str(redacted)
    assert "secret-key" not in str(nested_redacted)
    assert runner.GENERATED_INSTANCE_PASSWORD not in str(nested_redacted)


def test_instance_workspace_does_not_persist_plaintext_password(tmp_path: Path) -> None:
    check = runner.Check(
        name=runner.INSTANCE_CREATE_STAGE,
        module="vm",
        vars={
            "name": "hc-vm",
            "vpc_id": "vpc-a",
            "image_name": "img",
            "flavor_name": "Medium-2",
            "storage_policy_id": "sp-a",
            "disk_gb": 40,
            "subnet_id": "subnet-a",
            "password": runner.GENERATED_INSTANCE_PASSWORD,
        },
    )

    runner.write_workspace_at(check, tmp_path)

    tfvars = (tmp_path / "terraform.tfvars.json").read_text(encoding="utf-8")
    main_tf = (tmp_path / "main.tf").read_text(encoding="utf-8")
    assert "password" not in tfvars
    assert runner.GENERATED_INSTANCE_PASSWORD not in tfvars
    assert 'variable "password"' in main_tf
    assert "sensitive = true" in main_tf
    assert "default = null" in main_tf


def test_instance_terraform_receives_password_only_through_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], cwd: Path, *, timeout: int = 900, stage: str = "terraform", extra_env: dict[str, str] | None = None):
        captured["cmd"] = cmd
        captured["extra_env"] = extra_env
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(runner, "run", fake_run)

    runner.run_instance_terraform(["terraform", "plan", "-input=false"], tmp_path, stage="compute.create-instance-test")

    assert captured["extra_env"] == {"TF_VAR_password": runner.GENERATED_INSTANCE_PASSWORD}
    assert runner.GENERATED_INSTANCE_PASSWORD not in " ".join(captured["cmd"])


def test_command_logs_redact_generated_password(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_subprocess_run(*_args, **_kwargs):
        return type("Result", (), {"returncode": 0, "stdout": f"secret={runner.GENERATED_INSTANCE_PASSWORD}", "stderr": ""})()

    monkeypatch.setattr(runner.subprocess, "run", fake_subprocess_run)

    runner.run(["terraform", "show"], tmp_path, stage="show-plan")

    stdout_log = (tmp_path / "logs" / "show-plan.stdout.log").read_text(encoding="utf-8")
    assert runner.GENERATED_INSTANCE_PASSWORD not in stdout_log
    assert "<redacted>" in stdout_log


def test_instance_create_retains_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner.events.clear()
    runner.instance_validation.update({"valid": True, "diagnostics": {}, "vars": {"instances": [{"label": "ubuntu-22-04", "vars": {"name": "hc-vm", "vpc_id": "vpc-a", "image_name": "img", "flavor_name": "2C2G", "storage_policy_id": "sp-a", "disk_gb": 40, "subnet_id": "subnet-a", "password": "secret", "ssh_key": None, "security_group_ids": []}}]}, "errors": []})
    monkeypatch.delenv("HC_KEEP_INSTANCE", raising=False)
    monkeypatch.delenv("HC_CLEANUP_ON_QUOTA_EXCEEDED", raising=False)
    monkeypatch.setattr(runner, "RUN_ROOT", tmp_path)
    monkeypatch.setattr(runner, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(runner, "resolve_instance_image_flavor", lambda vars, diagnostics: (vars, diagnostics, ""))
    monkeypatch.setattr(runner, "planned_resources", lambda workspace: ["module.this.fptcloud_instance.this"])
    monkeypatch.setattr(runner, "state_resources", lambda workspace: ["module.this.fptcloud_instance.this"])
    monkeypatch.setattr(runner, "readiness", lambda workspace: (True, "resources are ready", ["module.this.fptcloud_instance.this"]))
    monkeypatch.setattr(runner, "instance_id_from_state", lambda workspace: "instance-a")
    monkeypatch.setattr(runner, "input_diagnostics", lambda: {"provider": {"source": "fpt-corp/fptcloud", "version": "0.3.50"}})
    monkeypatch.setattr(runner, "run", lambda *_args, **_kwargs: type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})())

    check = runner.Check(
        name=runner.INSTANCE_CREATE_STAGE,
        module="vm",
        vars={"instances": [{"label": "ubuntu-22-04"}]},
        required_vars=("instances",),
    )

    runner.execute_instance_create(check)

    assert any(event["stage"] == runner.INSTANCE_CREATE_STAGE and "instance_id=instance-a" in event["message"] for event in runner.events)
    assert any(event["stage"] == f"{runner.INSTANCE_CREATE_STAGE}:cleanup" and "instance_retained_by_policy" in event["message"] for event in runner.events)
    assert any("cleanup_policy=retain_by_default" in event["message"] for event in runner.events)
    assert any("cleanup_on_quota_exceeded=False" in event["message"] for event in runner.events)


def test_quota_apply_failure_blocks_without_cleanup_recovery_or_next_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner.events.clear()
    prepare_instance_dependencies()
    runner.instance_validation.update(
        {
            "valid": True,
            "diagnostics": {},
            "vars": {
                "instances": [
                    {
                        "label": "windows-2012",
                        "hostname": {"resource_name": "hc-vm-windows-2012-test", "guest_hostname": "hcw2012test01", "selected_hostname": "hcw2012test01", "hostname_valid": True, "hostname_validation_status": "passed", "validation_errors": []},
                        "vars": {"name": "hcw2012test01", "vpc_id": "vpc-a", "image_name": "win2012", "flavor_name": "2C2G", "storage_policy_id": "sp-a", "disk_gb": 40, "subnet_id": "subnet-a", "password": "secret", "ssh_key": None, "security_group_ids": []},
                    },
                    {
                        "label": "windows-2016",
                        "hostname": {"resource_name": "hc-vm-windows-2016-test", "guest_hostname": "hcw2016test01", "selected_hostname": "hcw2016test01", "hostname_valid": True, "hostname_validation_status": "passed", "validation_errors": []},
                        "vars": {"name": "hcw2016test01", "vpc_id": "vpc-a", "image_name": "win2016", "flavor_name": "2C2G", "storage_policy_id": "sp-a", "disk_gb": 40, "subnet_id": "subnet-a", "password": "secret", "ssh_key": None, "security_group_ids": []},
                    },
                ]
            },
            "errors": [],
        }
    )
    runner.run_context["selected_instance_round"] = {
        "instances_per_apply": 1,
        "stop_on_quota_exceeded": True,
        "selected_images": ["windows-2012", "windows-2016"],
        "successful_images": [],
        "failed_image": "",
        "failure_reason": "",
        "remaining_images_not_attempted": [],
        "unavailable_images": [],
        "apply_requested_instance_count": 1,
        "apply_requested_storage_gb": 40,
        "apply_requested_cpu": 2,
        "apply_requested_ram_mb": 2048,
    }
    monkeypatch.setenv("HC_CLEANUP_ON_QUOTA_EXCEEDED", "true")
    monkeypatch.setattr(runner, "RUN_ROOT", tmp_path)
    monkeypatch.setattr(runner, "resolve_instance_image_flavor", lambda vars, diagnostics: (vars, diagnostics, ""))
    monkeypatch.setattr(runner, "planned_resources", lambda workspace: ["module.this.fptcloud_instance.this"])
    monkeypatch.setattr(runner, "state_resources", lambda workspace: ["module.this.fptcloud_instance.this"])
    monkeypatch.setattr(runner, "instance_id_from_state", lambda workspace: "instance-failed")
    monkeypatch.setattr(runner, "input_diagnostics", lambda: {"provider": {"source": "fpt-corp/fptcloud", "version": "0.3.50"}})

    forbidden_calls: list[str] = []
    monkeypatch.setattr(runner, "cleanup_instance", lambda *args, **kwargs: forbidden_calls.append("cleanup"))
    monkeypatch.setattr(runner, "reclaim_health_check_instance", lambda *args, **kwargs: forbidden_calls.append("reclaim"))
    monkeypatch.setattr(runner, "fallback_storage_policy_allowed", lambda *args, **kwargs: forbidden_calls.append("fallback") or False)

    terraform_stages: list[str] = []

    def fake_run_instance_terraform(_cmd: list[str], _cwd: Path, *, timeout: int = 900, stage: str = "terraform"):
        terraform_stages.append(stage)
        if stage.endswith("-apply-attempt-1"):
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "Instance storage exceeds VPC quota. Please check again!"})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(runner, "run_instance_terraform", fake_run_instance_terraform)

    check = runner.Check(
        name=runner.INSTANCE_CREATE_STAGE,
        module="vm",
        vars=dict(runner.instance_validation["vars"]),
        required_vars=("instances",),
    )

    runner.execute_instance_create(check)

    input_events = [event for event in runner.events if event["stage"] == f"{runner.INSTANCE_CREATE_STAGE}:inputs"]
    assert len(input_events) == 1
    assert "os_label=windows-2012" in input_events[0]["message"]
    assert "windows-2016" not in [stage for stage in terraform_stages if "apply" in stage]
    assert forbidden_calls == []
    assert runner.run_context["run_status"] == "blocked_waiting_user_confirmation"
    assert runner.run_context["user_action_required"] is True
    assert runner.run_context["remaining_images_not_attempted"] == ["windows-2016"]
    assert not any(event["stage"] == "compute.recover-instance-quota" for event in runner.events)
    assert not any(event["stage"] == "compute.reclaim-health-check-instance" for event in runner.events)
    assert not any(event["stage"] == "compute.recreate-instance" for event in runner.events)
    quota_messages = "\n".join(event["message"] for event in runner.events)
    assert "failure_reason=instance_storage_quota_exceeded" in quota_messages
    assert "quota_precheck=disabled" in quota_messages
    assert "quota_assumption=assume_sufficient" in quota_messages
    assert "quota_exceeded_action=stop_and_wait_for_user" in quota_messages
    assert "user_action_required=True" in quota_messages


def test_keep_instance_true_prevents_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner.events.clear()
    runner.run_context["selected_instance_round"] = {}
    runner.run_context["run_status"] = "running"
    runner.run_context["run_blocked"] = False
    runner.run_context["user_action_required"] = False
    runner.run_context["remaining_images_not_attempted"] = []
    runner.instance_validation.update({"valid": True, "diagnostics": {}, "vars": {"instances": [{"label": "ubuntu-22-04", "vars": {"name": "hc-vm", "vpc_id": "vpc-a", "image_name": "img", "flavor_name": "2C2G", "storage_policy_id": "sp-a", "disk_gb": 40, "subnet_id": "subnet-a", "password": "secret", "ssh_key": None, "security_group_ids": []}}]}, "errors": []})
    cleaned: list[str] = []
    monkeypatch.setenv("HC_KEEP_INSTANCE", "true")
    monkeypatch.setattr(runner, "RUN_ROOT", tmp_path)
    monkeypatch.setattr(runner, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(runner, "resolve_instance_image_flavor", lambda vars, diagnostics: (vars, diagnostics, ""))
    monkeypatch.setattr(runner, "planned_resources", lambda workspace: ["module.this.fptcloud_instance.this"])
    monkeypatch.setattr(runner, "state_resources", lambda workspace: ["module.this.fptcloud_instance.this"])
    monkeypatch.setattr(runner, "readiness", lambda workspace: (True, "resources are ready", ["module.this.fptcloud_instance.this"]))
    monkeypatch.setattr(runner, "instance_id_from_state", lambda workspace: "instance-a")
    monkeypatch.setattr(runner, "input_diagnostics", lambda: {"provider": {"source": "fpt-corp/fptcloud", "version": "0.3.50"}})
    monkeypatch.setattr(runner, "cleanup_instance", lambda workspace, name: cleaned.append(name))
    monkeypatch.setattr(runner, "run", lambda *_args, **_kwargs: type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})())

    check = runner.Check(
        name=runner.INSTANCE_CREATE_STAGE,
        module="vm",
        vars={"instances": [{"label": "ubuntu-22-04"}]},
        required_vars=("instances",),
    )

    runner.execute_instance_create(check)

    assert cleaned == []
    assert any(event["stage"] == f"{runner.INSTANCE_CREATE_STAGE}:cleanup" and "instance_retained_by_policy" in event["message"] for event in runner.events)


def test_quota_cleanup_requires_explicit_flag_and_safety(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner.events.clear()
    workspace = tmp_path / runner.INSTANCE_CREATE_STAGE / "ubuntu-22-04"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(runner, "RUN_ROOT", tmp_path)
    monkeypatch.setenv("HC_CLEANUP_ON_QUOTA_EXCEEDED", "true")
    monkeypatch.setattr(runner, "state_resources", lambda workspace: ["module.this.fptcloud_instance.this"])
    monkeypatch.setattr(runner, "instance_id_from_state", lambda workspace: "instance-a")
    monkeypatch.setattr(
        runner,
        "instance_state_values",
        lambda workspace: {"id": "instance-a", "name": "hcl-ubuntu2204-test01"},
    )
    destroyed: list[str] = []

    def fake_run_instance_terraform(cmd: list[str], cwd: Path, *, timeout: int = 900, stage: str = "terraform"):
        destroyed.append(stage)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(runner, "run_instance_terraform", fake_run_instance_terraform)

    deleted = runner.cleanup_instance(
        workspace,
        runner.INSTANCE_CREATE_STAGE,
        classification="instance_storage_quota_exceeded",
        expected_instance_name="hcl-ubuntu2204-test01",
        resource_name=f"hc-vm-ubuntu-22-04-{runner.INSTANCE_RUN_SUFFIX}",
    )

    assert deleted is True
    assert destroyed == [f"{runner.INSTANCE_CREATE_STAGE}-cleanup"]
    assert any("delete_allowed=True" in event["message"] for event in runner.events)
    assert any("deleted_instance_ids=instance-a" in event["message"] for event in runner.events)


def test_quota_cleanup_skips_when_safety_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner.events.clear()
    workspace = tmp_path / runner.INSTANCE_CREATE_STAGE / "ubuntu-22-04"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(runner, "RUN_ROOT", tmp_path)
    monkeypatch.setenv("HC_CLEANUP_ON_QUOTA_EXCEEDED", "true")
    monkeypatch.setattr(runner, "state_resources", lambda workspace: ["module.this.fptcloud_instance.this"])
    monkeypatch.setattr(runner, "instance_id_from_state", lambda workspace: "instance-a")
    monkeypatch.setattr(runner, "instance_state_values", lambda workspace: {"id": "instance-a", "name": "unexpected"})

    deleted = runner.cleanup_instance(
        workspace,
        runner.INSTANCE_CREATE_STAGE,
        classification="instance_storage_quota_exceeded",
        expected_instance_name="hcl-ubuntu2204-test01",
        resource_name="hc-vm-ubuntu-22-04-other-run",
    )

    assert deleted is False
    assert any("instance_cleanup_not_allowed" in event["message"] for event in runner.events)
    assert any("skipped_delete_reason=" in event["message"] for event in runner.events)


def test_create_instance_stage_cannot_run_if_not_defined_in_specs() -> None:
    runner.events.clear()

    selected = runner.select_stages({}, runner.INSTANCE_CREATE_STAGE)

    assert selected == {}
    assert runner.events[-1]["stage"] == "spec"
    assert runner.events[-1]["status"] == "failed"
