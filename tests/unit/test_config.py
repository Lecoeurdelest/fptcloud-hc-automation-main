"""Unit tests for runtime configuration."""

from __future__ import annotations

import pytest

from hc.config.settings import CloudSettings

pytestmark = pytest.mark.unit


def test_cloud_settings_reads_multiple_vpc_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FPTCLOUD_API_URL", "https://console-api.fptcloud.com/api")
    monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
    monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "FCI-L1-ORG")
    monkeypatch.setenv("FPTCLOUD_TOKEN", "token")
    monkeypatch.setenv("VPC_IDS", "FCI-L1-HAN-VPC, FCI-L1-OPS-HAN")
    monkeypatch.delenv("VPC_ID", raising=False)

    settings = CloudSettings.from_env()

    assert settings.vpc_ids == ("FCI-L1-HAN-VPC", "FCI-L1-OPS-HAN")
    assert settings.vpc_id == "FCI-L1-HAN-VPC"
    assert settings.terraform_env() == {
        "FPTCLOUD_API_URL": "https://console-api.fptcloud.com/api",
        "FPTCLOUD_REGION": "VN/HAN",
        "FPTCLOUD_TENANT_NAME": "FCI-L1-ORG",
        "FPTCLOUD_TOKEN": "token",
    }
    assert settings.terraform_vars_by_vpc({"name": "hc-subnet"}) == (
        {"name": "hc-subnet", "vpc_id": "FCI-L1-HAN-VPC"},
        {"name": "hc-subnet", "vpc_id": "FCI-L1-OPS-HAN"},
    )


def test_cloud_settings_keeps_legacy_vpc_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VPC_IDS", raising=False)
    monkeypatch.delenv("FPTCLOUD_API_URL", raising=False)
    monkeypatch.delenv("FPTCLOUD_REGION", raising=False)
    monkeypatch.delenv("FPTCLOUD_TENANT_NAME", raising=False)
    monkeypatch.delenv("FPTCLOUD_TOKEN", raising=False)
    monkeypatch.setenv("VPC_ID", "FCI-L1-OPS-HAN")

    settings = CloudSettings.from_env()

    assert settings.vpc_ids == ("FCI-L1-OPS-HAN",)
    assert settings.vpc_id == "FCI-L1-OPS-HAN"
    assert settings.terraform_env() == {}


def test_cloud_settings_dedupes_vpc_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VPC_IDS", "FCI-L1-HAN-VPC,FCI-L1-OPS-HAN,FCI-L1-HAN-VPC")
    monkeypatch.setenv("VPC_ID", "FCI-L1-OPS-HAN")

    settings = CloudSettings.from_env()

    assert settings.vpc_ids == ("FCI-L1-HAN-VPC", "FCI-L1-OPS-HAN")
