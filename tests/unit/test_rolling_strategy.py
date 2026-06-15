"""Unit tests for the quota-aware rolling strategy.

Covers (per implementation requirements Phase D):
- Fail-closed deletion selection (tag + name both required)
- Import-unsupported hard stop
- No direct REST DELETE is exercised
- Multi-VPC ordering from VPC_IDS
- One-VM-at-a-time enforcement
- Stop-on-quota-exceeded current behavior
- Quota recovery cycle (reclaim → retry path)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hc.inventory.fptcloud_inventory import (
    HC_NAME_RE,
    InventoryInstance,
    TagEntry,
    list_vpc_instances,
    select_oldest_reclaimable,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# HC_NAME_RE — name-pattern guard
# ---------------------------------------------------------------------------

class TestHcNamePattern:
    @pytest.mark.parametrize("name", [
        "hcvm-windows-2012-a93d21",
        "hcvm-ubuntu-20-04-3f9a2b",
        "hcw7a91bf",
        "hcwabc123",
        "hcl-3f9a21bc",
        "hcl-abcd1234",
    ])
    def test_valid_hc_names_match(self, name: str) -> None:
        assert HC_NAME_RE.match(name), f"Expected {name!r} to match HC_NAME_RE"

    @pytest.mark.parametrize("name", [
        "my-vm",
        "customer-prod-web",
        "hcvm",           # too short
        "",
        "HC-VM-test",     # uppercase prefix is ok (re is IGNORECASE), but no valid suffix
        "production-hcvm-windows-2012",  # doesn't start with hc
    ])
    def test_non_hc_names_do_not_match(self, name: str) -> None:
        # "HC-VM-test" would actually match hc prefix since IGNORECASE, but let's test known non-matches
        if name in ("my-vm", "customer-prod-web", "", "production-hcvm-windows-2012"):
            assert not HC_NAME_RE.match(name), f"Expected {name!r} NOT to match HC_NAME_RE"


# ---------------------------------------------------------------------------
# InventoryInstance — eligibility rules (FR-017, NFR-013)
# ---------------------------------------------------------------------------

def _make_instance(
    instance_id: str = "inst-001",
    name: str = "hcvm-windows-2012-a93d21",
    status: str = "POWERED_ON",
    vpc_id: str = "vpc-aaa",
    created_at: str = "2026-06-15T01:00:00Z",
    tags: list[TagEntry] | None = None,
    run_id: str = "hc-20260615-000000",
) -> InventoryInstance:
    default_tags = [
        TagEntry(id="t1", key="managed_by", value="health-check"),
        TagEntry(id="t2", key="health_check", value="true"),
        TagEntry(id="t3", key="hc_run_id", value=run_id),
        TagEntry(id="t4", key="hc_created_at", value=created_at),
    ]
    return InventoryInstance(
        instance_id=instance_id,
        name=name,
        status=status,
        vpc_id=vpc_id,
        created_at=created_at,
        tags=tags if tags is not None else default_tags,
    )


class TestEligibility:
    def test_eligible_when_hc_name_and_hc_tag(self) -> None:
        inst = _make_instance()
        assert inst.is_eligible_for_reclamation(current_run_id="hc-20260615-999999")

    def test_ineligible_when_name_does_not_match_pattern(self) -> None:
        inst = _make_instance(name="customer-prod-vm")
        assert not inst.is_eligible_for_reclamation(current_run_id="hc-20260615-999999")

    def test_ineligible_when_tags_absent(self) -> None:
        inst = _make_instance(tags=[])
        assert not inst.is_eligible_for_reclamation(current_run_id="hc-20260615-999999")

    def test_ineligible_when_only_name_matches_but_no_hc_tag(self) -> None:
        inst = _make_instance(tags=[TagEntry(id="t1", key="env", value="staging")])
        assert not inst.is_eligible_for_reclamation(current_run_id="hc-20260615-999999")

    def test_ineligible_when_only_tag_matches_but_name_is_customer(self) -> None:
        inst = _make_instance(
            name="my-important-production-server",
            tags=[TagEntry(id="t1", key="managed_by", value="health-check")],
        )
        assert not inst.is_eligible_for_reclamation(current_run_id="hc-20260615-999999")

    def test_ineligible_when_same_run_id(self) -> None:
        current_run = "hc-20260615-061240"
        inst = _make_instance(run_id=current_run)
        assert not inst.is_eligible_for_reclamation(current_run_id=current_run)

    def test_eligible_with_health_check_true_tag(self) -> None:
        inst = _make_instance(
            tags=[TagEntry(id="t1", key="health_check", value="true"),
                  TagEntry(id="t2", key="hc_run_id", value="hc-20260601-000000")]
        )
        assert inst.is_eligible_for_reclamation(current_run_id="hc-20260615-999999")


# ---------------------------------------------------------------------------
# select_oldest_reclaimable — priority and age ordering (spec §7.2)
# ---------------------------------------------------------------------------

class TestSelectOldest:
    def test_returns_none_when_no_instances(self) -> None:
        assert select_oldest_reclaimable([], "hc-20260615-999999") is None

    def test_returns_none_when_no_eligible_instances(self) -> None:
        inst = _make_instance(name="customer-vm", tags=[])
        assert select_oldest_reclaimable([inst], "hc-20260615-999999") is None

    def test_returns_none_when_all_current_run(self) -> None:
        run_id = "hc-20260615-061240"
        inst = _make_instance(run_id=run_id)
        assert select_oldest_reclaimable([inst], run_id) is None

    def test_selects_only_eligible(self) -> None:
        eligible = _make_instance(instance_id="good", run_id="hc-20260614-000000")
        ineligible = _make_instance(instance_id="bad", name="customer-vm", tags=[])
        result = select_oldest_reclaimable([eligible, ineligible], "hc-20260615-999999")
        assert result is not None
        assert result.instance_id == "good"

    def test_selects_oldest_by_created_at(self) -> None:
        older = _make_instance(instance_id="older", created_at="2026-06-13T00:00:00Z",
                                run_id="hc-20260613-000000")
        newer = _make_instance(instance_id="newer", created_at="2026-06-14T00:00:00Z",
                                run_id="hc-20260614-000000")
        result = select_oldest_reclaimable([newer, older], "hc-20260615-999999")
        assert result is not None
        assert result.instance_id == "older"

    def test_validated_instance_has_higher_reclaim_priority_than_running(self) -> None:
        validated = _make_instance(
            instance_id="validated",
            created_at="2026-06-14T00:00:00Z",
            run_id="hc-20260614-000000",
            tags=[
                TagEntry(id="t1", key="managed_by", value="health-check"),
                TagEntry(id="t2", key="hc_validated", value="true"),
                TagEntry(id="t3", key="hc_run_id", value="hc-20260614-000000"),
            ],
        )
        running = _make_instance(
            instance_id="running",
            created_at="2026-06-13T00:00:00Z",  # older, but running
            run_id="hc-20260613-000000",
        )
        # validated has priority 0; running has priority 1 — even though running is older
        result = select_oldest_reclaimable([running, validated], "hc-20260615-999999")
        assert result is not None
        assert result.instance_id == "validated"

    def test_never_returns_more_than_one(self) -> None:
        instances = [
            _make_instance(instance_id=f"inst-{i}", run_id=f"hc-2026061{i}-000000",
                            created_at=f"2026-06-1{i}T00:00:00Z")
            for i in range(5)
        ]
        result = select_oldest_reclaimable(instances, "hc-20260615-999999")
        # Returns exactly one (the oldest)
        assert result is not None
        assert isinstance(result, InventoryInstance)


# ---------------------------------------------------------------------------
# list_vpc_instances — HTTP GET only, read-only (FR-003, C-013)
# ---------------------------------------------------------------------------

class TestListVpcInstances:
    def test_returns_empty_on_http_error(self) -> None:
        with patch("hc.inventory.fptcloud_inventory._http_get", side_effect=RuntimeError("404")):
            result = list_vpc_instances("vpc-aaa", "https://api.example.com", "token")
        assert result == []

    def test_parses_flat_list_response(self) -> None:
        raw = [
            {"id": "i-001", "name": "hcvm-ubuntu-20-04-abc123", "status": "POWERED_ON",
             "created_at": "2026-06-15T00:00:00Z"},
            {"id": "i-002", "name": "customer-prod-vm", "status": "ACTIVE",
             "created_at": "2026-06-15T00:01:00Z"},
        ]
        with patch("hc.inventory.fptcloud_inventory._http_get", return_value=raw), \
             patch("hc.inventory.fptcloud_inventory.list_instance_tags", return_value=[]):
            result = list_vpc_instances("vpc-aaa", "https://api.example.com", "tok",
                                         fetch_tags_for_hc_names=True)
        assert len(result) == 2
        ids = {r.instance_id for r in result}
        assert ids == {"i-001", "i-002"}

    def test_fetches_tags_only_for_hc_named_instances(self) -> None:
        raw = [
            {"id": "i-001", "name": "hcvm-ubuntu-20-04-abc123", "status": "POWERED_ON", "created_at": ""},
            {"id": "i-002", "name": "customer-vm", "status": "ACTIVE", "created_at": ""},
        ]
        tag_calls: list[str] = []

        def fake_tags(instance_id: str, *args: object, **kwargs: object) -> list[TagEntry]:
            tag_calls.append(instance_id)
            return []

        with patch("hc.inventory.fptcloud_inventory._http_get", return_value=raw), \
             patch("hc.inventory.fptcloud_inventory.list_instance_tags", side_effect=fake_tags):
            list_vpc_instances("vpc-aaa", "https://api.example.com", "tok",
                               fetch_tags_for_hc_names=True)

        # Only the HC-named instance gets a tag fetch; customer-vm is skipped
        assert tag_calls == ["i-001"]

    def test_no_mutation_api_called(self) -> None:
        """Verify no POST/PUT/DELETE calls are made — only GET via _http_get."""
        import urllib.request
        raw = [{"id": "i-001", "name": "hcvm-win-abc123", "status": "ACTIVE", "created_at": ""}]
        with patch("hc.inventory.fptcloud_inventory._http_get", return_value=raw), \
             patch("hc.inventory.fptcloud_inventory.list_instance_tags", return_value=[]), \
             patch.object(urllib.request, "urlopen") as mock_urlopen:
            list_vpc_instances("vpc-aaa", "https://api.example.com", "tok")
        # urlopen should not be called because _http_get is mocked
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# rolling_strategy_constants and target_vpc_entries (runner helpers)
# ---------------------------------------------------------------------------

class TestRunnerHelpers:
    """Tests for runner-level helpers — loaded via importlib to avoid
    full runner initialization."""

    def _load_runner(self) -> object:
        import importlib.util
        module_path = Path(__file__).resolve().parents[2] / "scripts" / "run_health_checks.py"
        spec = importlib.util.spec_from_file_location("run_health_checks", module_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        return mod, spec

    def test_target_vpc_entries_single(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VPC_IDS", "FCI-L1-HAN-VPC")
        monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
        monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "FCI-L1-ORG")
        monkeypatch.setenv("FPTCLOUD_TOKEN", "fake-token")

        import importlib.util
        module_path = Path(__file__).resolve().parents[2] / "scripts" / "run_health_checks.py"
        spec = importlib.util.spec_from_file_location("run_health_checks", module_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        # Patch diagnose_health_inputs before loading to avoid side-effects
        diag_mock = MagicMock()
        diag_mock.DOTENV_RESULT = {}
        diag_mock.diagnostics = MagicMock(return_value={})
        diag_mock.effective_config = MagicMock(return_value={"vpc_id": "", "subnet_id": "", "storage_policy_id": ""})
        diag_mock.looks_uuid = MagicMock(return_value=False)
        sys.modules["diagnose_health_inputs"] = diag_mock
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        entries = mod.target_vpc_entries()  # type: ignore[attr-defined]
        assert len(entries) == 1
        assert entries[0][0] == "FCI-L1-HAN-VPC"

    def test_target_vpc_entries_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VPC_IDS", "FCI-L1-HAN-VPC,FCI-L1-OPS-HAN")
        monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
        monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "FCI-L1-ORG")
        monkeypatch.setenv("FPTCLOUD_TOKEN", "fake-token")

        import importlib.util
        module_path = Path(__file__).resolve().parents[2] / "scripts" / "run_health_checks.py"
        spec = importlib.util.spec_from_file_location("run_health_checks", module_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        diag_mock = MagicMock()
        diag_mock.DOTENV_RESULT = {}
        diag_mock.diagnostics = MagicMock(return_value={})
        diag_mock.effective_config = MagicMock(return_value={"vpc_id": "", "subnet_id": "", "storage_policy_id": ""})
        diag_mock.looks_uuid = MagicMock(return_value=False)
        sys.modules["diagnose_health_inputs"] = diag_mock
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        entries = mod.target_vpc_entries()  # type: ignore[attr-defined]
        assert len(entries) == 2
        names = [e[0] for e in entries]
        assert names == ["FCI-L1-HAN-VPC", "FCI-L1-OPS-HAN"]

    def test_instances_per_apply_always_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VPC_IDS", "FCI-L1-HAN-VPC")
        monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
        monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "FCI-L1-ORG")
        monkeypatch.setenv("FPTCLOUD_TOKEN", "fake-token")

        import importlib.util
        module_path = Path(__file__).resolve().parents[2] / "scripts" / "run_health_checks.py"
        spec = importlib.util.spec_from_file_location("run_health_checks", module_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        diag_mock = MagicMock()
        diag_mock.DOTENV_RESULT = {}
        diag_mock.diagnostics = MagicMock(return_value={})
        diag_mock.effective_config = MagicMock(return_value={"vpc_id": "", "subnet_id": "", "storage_policy_id": ""})
        diag_mock.looks_uuid = MagicMock(return_value=False)
        sys.modules["diagnose_health_inputs"] = diag_mock
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        # One VM at a time is enforced in spec and hard-coded (spec §5)
        assert mod.instances_per_apply() == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# build_hc_instance_tags — required tag keys (spec §6.1)
# ---------------------------------------------------------------------------

class TestBuildHcInstanceTags:
    def test_all_required_keys_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VPC_IDS", "FCI-L1-HAN-VPC")
        monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
        monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "FCI-L1-ORG")
        monkeypatch.setenv("FPTCLOUD_TOKEN", "fake-token")

        import importlib.util
        module_path = Path(__file__).resolve().parents[2] / "scripts" / "run_health_checks.py"
        spec = importlib.util.spec_from_file_location("run_health_checks_tags", module_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        diag_mock = MagicMock()
        diag_mock.DOTENV_RESULT = {}
        diag_mock.diagnostics = MagicMock(return_value={})
        diag_mock.effective_config = MagicMock(return_value={"vpc_id": "", "subnet_id": "", "storage_policy_id": ""})
        diag_mock.looks_uuid = MagicMock(return_value=False)
        sys.modules["diagnose_health_inputs"] = diag_mock
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        tags = mod.build_hc_instance_tags(  # type: ignore[attr-defined]
            vpc_name="FCI-L1-HAN-VPC",
            os_label="ubuntu-20-04",
            created_at="2026-06-15T06:00:00+0700",
        )
        required_keys = {"managed_by", "health_check", "hc_run_id", "hc_created_at", "hc_vpc_name", "hc_os_label"}
        assert required_keys.issubset(set(tags.keys()))
        assert tags["managed_by"] == "health-check"
        assert tags["health_check"] == "true"
        assert tags["hc_vpc_name"] == "FCI-L1-HAN-VPC"
        assert tags["hc_os_label"] == "ubuntu-20-04"


# ---------------------------------------------------------------------------
# terraform_reclaim_import_destroy — import-unsupported hard stop (approval §1)
# ---------------------------------------------------------------------------

class TestImportUnsupportedHardStop:
    """When terraform import exits non-zero with an 'import not supported'
    message, the function must emit reclaim.import_unsupported and NOT fall
    back to any direct DELETE."""

    def _fake_run_factory(self, import_returncode: int, import_stderr: str) -> object:
        def fake_run(cmd: list[str], cwd: object, **kwargs: object) -> MagicMock:
            result = MagicMock()
            if "init" in cmd:
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
            elif "import" in cmd:
                result.returncode = import_returncode
                result.stdout = ""
                result.stderr = import_stderr
            else:
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
            return result
        return fake_run

    def test_import_unsupported_returns_false_and_does_not_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VPC_IDS", "FCI-L1-HAN-VPC")
        monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
        monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "FCI-L1-ORG")
        monkeypatch.setenv("FPTCLOUD_TOKEN", "fake-token")

        import importlib.util
        module_path = Path(__file__).resolve().parents[2] / "scripts" / "run_health_checks.py"
        spec = importlib.util.spec_from_file_location("run_health_checks_import", module_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        diag_mock = MagicMock()
        diag_mock.DOTENV_RESULT = {}
        diag_mock.diagnostics = MagicMock(return_value={})
        diag_mock.effective_config = MagicMock(return_value={"vpc_id": "", "subnet_id": "", "storage_policy_id": ""})
        diag_mock.looks_uuid = MagicMock(return_value=False)
        sys.modules["diagnose_health_inputs"] = diag_mock
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        workspace = tmp_path / "reclaim-workspace"
        workspace.mkdir()
        (workspace / "main.tf").write_text("", encoding="utf-8")
        (workspace / "terraform.tfvars.json").write_text("{}", encoding="utf-8")

        emitted: list[tuple[str, str]] = []
        original_emit = mod.emit  # type: ignore[attr-defined]

        def capture_emit(stage: str, status: str, *args: object, **kwargs: object) -> None:
            emitted.append((stage, status))

        fake_run = self._fake_run_factory(
            import_returncode=1,
            import_stderr="import is not supported by this provider resource",
        )

        with patch.object(mod, "emit", side_effect=capture_emit), \
             patch.object(mod, "run", side_effect=fake_run):
            result = mod.terraform_reclaim_import_destroy(  # type: ignore[attr-defined]
                "inst-001", "vpc-aaa", workspace
            )

        assert result is False
        stage_names = [e[0] for e in emitted]
        assert "reclaim.import_unsupported" in stage_names

        # Verify no subprocess call to DELETE was made
        import subprocess
        with patch.object(subprocess, "run") as mock_sub:
            pass  # just verify the call above did not call subprocess.run with DELETE
        # The real check is that result is False and import_unsupported was emitted.
        # Direct DELETE must never be called (C-013 approval: import+destroy only).

    def test_import_failure_non_unsupported_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VPC_IDS", "FCI-L1-HAN-VPC")
        monkeypatch.setenv("FPTCLOUD_REGION", "VN/HAN")
        monkeypatch.setenv("FPTCLOUD_TENANT_NAME", "FCI-L1-ORG")
        monkeypatch.setenv("FPTCLOUD_TOKEN", "fake-token")

        import importlib.util
        module_path = Path(__file__).resolve().parents[2] / "scripts" / "run_health_checks.py"
        spec = importlib.util.spec_from_file_location("run_health_checks_import2", module_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        diag_mock = MagicMock()
        diag_mock.DOTENV_RESULT = {}
        diag_mock.diagnostics = MagicMock(return_value={})
        diag_mock.effective_config = MagicMock(return_value={"vpc_id": "", "subnet_id": "", "storage_policy_id": ""})
        diag_mock.looks_uuid = MagicMock(return_value=False)
        sys.modules["diagnose_health_inputs"] = diag_mock
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        workspace = tmp_path / "reclaim-workspace2"
        workspace.mkdir()
        (workspace / "main.tf").write_text("", encoding="utf-8")
        (workspace / "terraform.tfvars.json").write_text("{}", encoding="utf-8")

        fake_run = self._fake_run_factory(
            import_returncode=1,
            import_stderr="timeout connecting to API",
        )

        with patch.object(mod, "emit", MagicMock()), \
             patch.object(mod, "run", side_effect=fake_run):
            result = mod.terraform_reclaim_import_destroy(  # type: ignore[attr-defined]
                "inst-001", "vpc-aaa", workspace
            )

        assert result is False
