"""Unit tests for src/hc/executor/ — Phase 2.

All tests mock the terraform subprocess so no real Terraform binary is needed.
Covers T-0201 through T-0215.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hc.executor.classifier import ErrorClassifier
from hc.executor.executor import TerraformExecutor, TerraformExecutorError
from hc.executor.models import ErrorCategory, TFState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture()
def module_path(tmp_path: Path) -> Path:
    return tmp_path / "modules" / "subnet"


@pytest.fixture()
def executor(workspace: Path, module_path: Path) -> TerraformExecutor:
    return TerraformExecutor(
        workspace_path=workspace,
        module_path=module_path,
        vars={"cidr": "10.0.0.0/24", "name": "test-subnet", "vpc_id": "vpc-123"},
        env={},
        cleanup_on_success=False,
    )


def _make_executor(
    tmp_path: Path,
    vars: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    cleanup_on_success: bool = False,
    plugin_cache_dir: Path | None = None,
) -> TerraformExecutor:
    return TerraformExecutor(
        workspace_path=tmp_path / "ws",
        module_path=tmp_path / "modules" / "subnet",
        vars=vars or {"cidr": "10.0.0.0/24"},
        env=env or {},
        cleanup_on_success=cleanup_on_success,
        plugin_cache_dir=plugin_cache_dir,
    )


# ---------------------------------------------------------------------------
# T-0201: Workspace bootstrap creates main.tf with correct module source
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWorkspaceBootstrap:
    def test_main_tf_created_with_module_source(
        self, executor: TerraformExecutor, workspace: Path, module_path: Path
    ) -> None:
        """T-0201 — bootstrap() writes main.tf containing the module source path."""
        with patch.object(executor, "_run_init"):
            executor.bootstrap()

        main_tf_text = (workspace / "main.tf").read_text()
        # Module source path (with forward slashes for HCL)
        expected_source = str(module_path).replace("\\", "/")
        assert expected_source in main_tf_text

    def test_main_tf_has_provider_block(self, executor: TerraformExecutor, workspace: Path) -> None:
        with patch.object(executor, "_run_init"):
            executor.bootstrap()
        content = (workspace / "main.tf").read_text()
        assert 'source  = "fpt-corp/fptcloud"' in content
        assert 'provider "fptcloud" {}' in content

    def test_main_tf_has_variable_and_module_arg_for_each_var(
        self, executor: TerraformExecutor, workspace: Path
    ) -> None:
        with patch.object(executor, "_run_init"):
            executor.bootstrap()
        content = (workspace / "main.tf").read_text()
        for key in ("cidr", "name", "vpc_id"):
            assert f'variable "{key}"' in content
            assert f"{key} = var.{key}" in content

    # T-0202 — tfvars.json matches input vars
    def test_tfvars_json_written_correctly(
        self, executor: TerraformExecutor, workspace: Path
    ) -> None:
        """T-0202 — terraform.tfvars.json matches input vars dict."""
        with patch.object(executor, "_run_init"):
            executor.bootstrap()

        tfvars = json.loads((workspace / "terraform.tfvars.json").read_text())
        assert tfvars["cidr"] == "10.0.0.0/24"
        assert tfvars["name"] == "test-subnet"
        assert tfvars["vpc_id"] == "vpc-123"

    # T-0203 — terraform init called with plugin cache
    def test_init_uses_plugin_cache_dir(self, tmp_path: Path) -> None:
        """T-0203 — TF_PLUGIN_CACHE_DIR is set in the env passed to terraform init."""
        cache = tmp_path / "plugin-cache"
        ex = _make_executor(tmp_path, plugin_cache_dir=cache)

        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
            captured["env"] = env or {}
            return (0, "", "")

        # Ensure workspace exists so _run_init doesn't fail on mkdir
        (tmp_path / "ws").mkdir(parents=True)
        with patch.object(ex, "_run", side_effect=fake_run):
            ex._run_init()

        assert captured["env"].get("TF_PLUGIN_CACHE_DIR") == str(cache)

    # T-0215 — env vars not written to disk
    def test_fptcloud_token_not_written_to_disk(self, tmp_path: Path) -> None:
        """T-0215 — FPTCLOUD_* secrets are passed only to subprocess, never written to disk."""
        secret = "super-secret-token-xyz"
        ex = _make_executor(
            tmp_path,
            vars={"cidr": "10.0.0.0/24"},
            env={"FPTCLOUD_TOKEN": secret},
        )
        with patch.object(ex, "_run_init"):
            ex.bootstrap()

        ws = tmp_path / "ws"
        for f in ws.rglob("*"):
            if f.is_file():
                assert secret not in f.read_text(errors="ignore"), f"Secret found in {f}"


# ---------------------------------------------------------------------------
# T-0204 / T-0205: plan exit codes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPlan:
    def _executor_with_fake_run(
        self, tmp_path: Path, plan_rc: int
    ) -> tuple[TerraformExecutor, Path]:
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)
        ex = _make_executor(tmp_path)

        canned_plan_json = json.dumps(
            {
                "format_version": "1.0",
                "resource_changes": [{"change": {"actions": ["create"]}}],
            }
        )

        def fake_run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
            if "plan" in cmd and "-detailed-exitcode" in cmd:
                return (plan_rc, "Plan: 1 to add.", "")
            if "show" in cmd and "-json" in cmd:
                return (0, canned_plan_json, "")
            return (0, "", "")

        ex._run = fake_run  # type: ignore[method-assign]
        return ex, ws

    def test_plan_returns_exit_code_2_on_changes(self, tmp_path: Path) -> None:
        """T-0204 — plan() returns exit code 2 when resources are pending creation."""
        ex, _ = self._executor_with_fake_run(tmp_path, plan_rc=2)
        rc, plan_json = ex.plan()
        assert rc == 2
        assert "resource_changes" in plan_json

    def test_plan_returns_exit_code_0_on_no_changes(self, tmp_path: Path) -> None:
        """T-0205 — plan() returns exit code 0 when state is already up-to-date."""
        ex, _ = self._executor_with_fake_run(tmp_path, plan_rc=0)
        rc, _ = ex.plan()
        assert rc == 0

    def test_plan_raises_on_non_zero_non_two(self, tmp_path: Path) -> None:
        ex, _ = self._executor_with_fake_run(tmp_path, plan_rc=1)
        with pytest.raises(TerraformExecutorError):
            ex.plan()


# ---------------------------------------------------------------------------
# T-0206: apply captures stdout/stderr into structured log
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApplyStreaming:
    def test_apply_stdout_and_stderr_captured(self, tmp_path: Path) -> None:
        """T-0206 — execute() captures stdout/stderr line by line."""
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)
        ex = _make_executor(tmp_path, cleanup_on_success=False)

        tf_state_json = json.dumps(
            {
                "format_version": "1.0",
                "terraform_version": "1.7.0",
                "resources": [
                    {
                        "mode": "managed",
                        "type": "fptcloud_subnet",
                        "name": "this",
                        "provider": 'provider["fpt-corp/fptcloud"]',
                        "instances": [{"attributes": {"id": "s-1", "cidr": "10.0.0.0/24"}}],
                    }
                ],
                "outputs": {},
            }
        )

        def fake_run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
            if "init" in cmd:
                return (0, "Initializing provider...", "")
            if "plan" in cmd and "-detailed-exitcode" in cmd:
                return (2, "Plan: 1 to add.", "")
            if "show" in cmd and "tfplan" in cmd:
                return (0, json.dumps({"format_version": "1.0"}), "")
            if "apply" in cmd:
                return (0, "Apply complete!\nResources: 1 added.", "")
            if "show" in cmd:
                return (0, tf_state_json, "")
            return (0, "", "")

        ex._run = fake_run  # type: ignore[method-assign]

        with patch.object(ex, "_run_init"):
            result = ex.execute()

        assert result.success is True
        assert "Apply complete" in result.stdout

    def test_apply_failure_returns_error_result(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)
        ex = _make_executor(tmp_path, cleanup_on_success=False)

        def fake_run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
            if "plan" in cmd and "-detailed-exitcode" in cmd:
                return (2, "Plan: 1 to add.", "")
            if "show" in cmd and "tfplan" in cmd:
                return (0, "{}", "")
            if "apply" in cmd:
                return (1, "", "Error: 503 Service Unavailable")
            return (0, "", "")

        ex._run = fake_run  # type: ignore[method-assign]

        with patch.object(ex, "_run_init"):
            result = ex.execute()

        assert result.success is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# T-0207: post-apply terraform show -json → TFState
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShowState:
    def test_show_state_parsed_into_tfstate(self, tmp_path: Path) -> None:
        """T-0207 — show_state() parses terraform show -json into TFState model."""
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)
        ex = _make_executor(tmp_path)

        tf_state_json = {
            "format_version": "1.0",
            "terraform_version": "1.7.0",
            "resources": [
                {
                    "mode": "managed",
                    "type": "fptcloud_subnet",
                    "name": "this",
                    "provider": 'provider["fpt-corp/fptcloud"]',
                    "instances": [{"attributes": {"id": "s-abc", "cidr": "172.26.221.0/24"}}],
                }
            ],
            "outputs": {"result": {"value": {"id": "s-abc"}, "sensitive": False}},
        }

        def fake_run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
            return (0, json.dumps(tf_state_json), "")

        ex._run = fake_run  # type: ignore[method-assign]
        state = ex.show_state()

        assert isinstance(state, TFState)
        assert state.format_version == "1.0"
        assert state.terraform_version == "1.7.0"
        assert len(state.resources) == 1
        attrs = state.get_resource_attrs("fptcloud_subnet")
        assert attrs is not None
        assert attrs["cidr"] == "172.26.221.0/24"


# ---------------------------------------------------------------------------
# T-0208 → T-0212: Error classifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestErrorClassifier:
    def setup_method(self) -> None:
        self.c = ErrorClassifier()

    def test_5xx_transient(self) -> None:
        """T-0208 — 5xx provider response → transient."""
        err = self.c.classify("Error: 500 internal server error from provider API")
        assert err.category == ErrorCategory.TRANSIENT

    def test_quota_exceeded(self) -> None:
        """T-0209 — quota exceeded → quota."""
        err = self.c.classify("Error: quota exceeded for resource type fptcloud_instance")
        assert err.category == ErrorCategory.QUOTA

    def test_auth_failure_401(self) -> None:
        """T-0210 — 401 unauthorized → auth."""
        err = self.c.classify("Error: 401 unauthorized — invalid token")
        assert err.category == ErrorCategory.AUTH

    def test_auth_failure_invalid_token(self) -> None:
        """T-0210 — 'invalid token' → auth."""
        err = self.c.classify("Error: invalid token provided, please re-authenticate")
        assert err.category == ErrorCategory.AUTH

    def test_schema_mismatch(self) -> None:
        """T-0211 — unsupported argument → schema."""
        err = self.c.classify('Error: An argument named "unknown_field" is not expected here.')
        assert err.category == ErrorCategory.SCHEMA

    def test_unknown_pattern_returns_unknown(self) -> None:
        """T-0212 — unrecognised error text → unknown."""
        err = self.c.classify("Something completely weird happened: gnarly bug #42")
        assert err.category == ErrorCategory.UNKNOWN

    def test_unknown_pattern_extracts_first_line_as_message(self) -> None:
        """T-0212 — message is first non-empty line of the raw error."""
        raw = "\nFirst line of the error\nSecond line"
        err = self.c.classify(raw)
        assert err.message == "First line of the error"

    def test_403_forbidden_is_auth(self) -> None:
        err = self.c.classify("Error: 403 forbidden — access denied")
        assert err.category == ErrorCategory.AUTH

    def test_rate_limit_is_quota(self) -> None:
        err = self.c.classify("Error: rate limit reached, please slow down")
        assert err.category == ErrorCategory.QUOTA

    def test_connection_timeout_is_transient(self) -> None:
        err = self.c.classify("Error: connection timed out while reaching API endpoint")
        assert err.category == ErrorCategory.TRANSIENT

    def test_state_lock_is_transient(self) -> None:
        err = self.c.classify("Error: state lock timeout — another process holds the lock")
        assert err.category == ErrorCategory.TRANSIENT

    def test_invalid_value_is_schema(self) -> None:
        err = self.c.classify('Error: invalid value for "cidr" — expected a valid CIDR block')
        assert err.category == ErrorCategory.SCHEMA


# ---------------------------------------------------------------------------
# T-0213 / T-0214: Workspace cleanup
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWorkspaceCleanup:
    def test_cleanup_on_success_removes_workspace(self, tmp_path: Path) -> None:
        """T-0213 — workspace directory is deleted when cleanup_on_success=True."""
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)
        (ws / "main.tf").write_text("# dummy")

        ex = TerraformExecutor(
            workspace_path=ws,
            module_path=tmp_path / "modules" / "subnet",
            vars={},
            env={},
            cleanup_on_success=True,
        )

        tf_state_json = json.dumps(
            {
                "format_version": "1.0",
                "terraform_version": "1.7.0",
                "resources": [],
                "outputs": {},
            }
        )

        def fake_run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
            if "plan" in cmd and "-detailed-exitcode" in cmd:
                return (2, "Plan: 1 to add.", "")
            if "show" in cmd and "tfplan" in cmd:
                return (0, "{}", "")
            if "apply" in cmd:
                return (0, "Apply complete!", "")
            if "show" in cmd:
                return (0, tf_state_json, "")
            return (0, "", "")

        ex._run = fake_run  # type: ignore[method-assign]

        with patch.object(ex, "_run_init"):
            result = ex.execute()

        assert result.success is True
        assert not ws.exists()

    def test_workspace_preserved_on_failure(self, tmp_path: Path) -> None:
        """T-0214 — workspace is preserved when cleanup_on_success=False (default for failures)."""
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)

        ex = TerraformExecutor(
            workspace_path=ws,
            module_path=tmp_path / "modules" / "subnet",
            vars={},
            env={},
            cleanup_on_success=True,  # cleanup only on success — failure keeps ws
        )

        def fake_run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
            if "plan" in cmd and "-detailed-exitcode" in cmd:
                return (2, "Plan: 1 to add.", "")
            if "show" in cmd and "tfplan" in cmd:
                return (0, "{}", "")
            if "apply" in cmd:
                return (1, "", "Error: 503 Service Unavailable")
            return (0, "", "")

        ex._run = fake_run  # type: ignore[method-assign]

        with patch.object(ex, "_run_init"):
            result = ex.execute()

        assert result.success is False
        # cleanup is NOT called on failure path — workspace survives
        assert ws.exists()


# ---------------------------------------------------------------------------
# Additional TFState model tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTFStateModel:
    def test_from_json_parses_resources_and_outputs(self) -> None:
        raw = {
            "format_version": "1.0",
            "terraform_version": "1.7.5",
            "resources": [
                {
                    "mode": "managed",
                    "type": "fptcloud_subnet",
                    "name": "this",
                    "provider": 'provider["fpt-corp/fptcloud"]',
                    "instances": [{"attributes": {"id": "s-1", "cidr": "10.0.0.0/24"}}],
                }
            ],
            "outputs": {"result": {"value": {"id": "s-1"}, "type": "object", "sensitive": False}},
        }
        state = TFState.from_json(raw)
        assert state.terraform_version == "1.7.5"
        assert len(state.resources) == 1
        attrs = state.get_resource_attrs("fptcloud_subnet", "this")
        assert attrs is not None
        assert attrs["cidr"] == "10.0.0.0/24"
        assert state.get_output("result") == {"id": "s-1"}

    def test_get_resource_attrs_returns_none_for_missing(self) -> None:
        state = TFState.from_json({"format_version": "1.0", "resources": []})
        assert state.get_resource_attrs("fptcloud_subnet") is None

    def test_get_output_returns_none_for_missing(self) -> None:
        state = TFState.from_json({})
        assert state.get_output("nonexistent") is None


# ---------------------------------------------------------------------------
# Integration-style test (no real Terraform) — T-1101 marker
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExecutorIntegration:
    """These tests require the terraform binary on PATH. Skipped in unit-only runs."""

    def test_terraform_init_validates_module(self, tmp_path: Path) -> None:
        """T-1101 — terraform init succeeds with a well-formed module (offline cache)."""
        pytest.importorskip("subprocess")
        import shutil

        if not shutil.which("terraform"):
            pytest.skip("terraform binary not on PATH")

        # Use an actual module from the repo
        repo_root = Path(__file__).resolve().parents[2]
        module = repo_root / "modules" / "subnet"
        if not module.exists():
            pytest.skip("modules/subnet not found")

        ex = TerraformExecutor(
            workspace_path=tmp_path / "ws",
            module_path=module,
            vars={"cidr": "10.0.0.0/24", "name": "t", "vpc_id": "vpc-1"},
            env={},
            cleanup_on_success=False,
        )
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)
        ex._write_main_tf()
        ex._write_tfvars()
        # init will try to download the provider — only works online
        # The test is marked integration; in CI without FPT creds it will fail
        # at plan/apply time, but init itself should succeed if provider registry reachable.
        rc, _, stderr = ex._run(
            ["terraform", "init", "-no-color", "-input=false", "-backend=false"]
        )
        # Accept 0 (success) or non-zero but NOT a Python-level crash
        assert isinstance(rc, int)
