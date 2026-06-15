from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "diagnose_health_inputs.py"
SPEC = importlib.util.spec_from_file_location("diagnose_health_inputs", MODULE_PATH)
assert SPEC and SPEC.loader
diag = importlib.util.module_from_spec(SPEC)
sys.modules["diagnose_health_inputs"] = diag
SPEC.loader.exec_module(diag)


pytestmark = pytest.mark.unit


def test_diagnostics_redacts_instance_secrets_and_warns_on_vpc_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
    monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "tenant-a")
    monkeypatch.setenv("VPC_IDS", "name-like-vpc")
    monkeypatch.setenv("HC_SSH_KEY", "secret-key-name")

    data = diag.diagnostics()

    assert data["stage_inputs"]["vm"]["configured"]["ssh_key"] == "<redacted>"
    assert data["stage_inputs"]["vm"]["configured"]["password_policy"] == "generated_by_runner"
    assert data["stage_inputs"]["vm"]["configured"]["keep_instance"] == "false"
    assert "cwd" in data["environment"]
    assert data["environment"]["dotenv_path"].endswith(".env")
    assert data["environment"]["dotenv_found"] in {True, False}
    assert data["environment"]["required_presence"]["FPTCLOUD_TOKEN"] in {"present", "missing"}
    assert any("compute.discover-vpc" in warning for warning in data["warnings"])


def test_explicit_ids_skip_discovery_and_prefer_vpc_id(monkeypatch: pytest.MonkeyPatch) -> None:
    vpc_id = "c9c1cfd6-7926-4a8a-96c6-cb4cd9d4aa19"
    monkeypatch.setenv("VPC_IDS", "display-vpc-name")
    monkeypatch.setenv("HC_VPC_ID", vpc_id)
    monkeypatch.setenv("HC_SUBNET_ID", "subnet-123")
    monkeypatch.setenv("HC_STORAGE_POLICY_ID", "policy-123")

    data = diag.diagnostics()
    config = diag.effective_config()

    assert config["vpc_id"] == vpc_id
    assert config["vpc_id_source"] == "explicit"
    assert data["stage_inputs"]["network"]["vars"]["vpc_id"] == vpc_id
    assert data["stage_inputs"]["discover_subnet"]["will_run"] is False
    assert data["stage_inputs"]["discover_storage_policy"]["will_run"] is False
