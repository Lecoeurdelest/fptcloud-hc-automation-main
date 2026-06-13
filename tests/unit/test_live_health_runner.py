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
