"""Terraform init/plan/apply/show wrapper, workspace rendering, and TF state I/O.

No stage-decision logic: callers decide what to do with results. The heavily
patched primitives (``run``, ``planned_resources``, ``state_resources``,
``readiness``, ``instance_id_from_state``, ``instance_state_values``,
``run_instance_terraform``, ``destroy``) are module globals so a single
``monkeypatch.setattr(healthcheck.terraform_executor, ...)`` affects every caller
(both inside this module and in peers that call them as ``tf.<name>``).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from healthcheck import state
from healthcheck.config import env
from healthcheck.logging import emit, now, queue_error, safe_name
from healthcheck.models import Check
from healthcheck.reporting import redact_text
from healthcheck.state import PENDING_STATUSES, READY_STATUSES


def run(
    cmd: list[str],
    cwd: Path,
    timeout: int = 900,
    stage: str = "terraform",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ)
    if extra_env:
        run_env.update(extra_env)
    if env("HC_TF_LOG"):
        run_env["TF_LOG"] = env("HC_TF_LOG")
    if env("HC_TF_LOG_PATH"):
        run_env["TF_LOG_PATH"] = env("HC_TF_LOG_PATH")
    elif env("HC_TF_LOG"):
        run_env["TF_LOG_PATH"] = str(cwd / f"{safe_name(stage)}.tf.log")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    log_dir = cwd / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(stage)
    (log_dir / f"{base}.stdout.log").write_text(redact_text(result.stdout or ""), encoding="utf-8")
    (log_dir / f"{base}.stderr.log").write_text(redact_text(result.stderr or ""), encoding="utf-8")
    return result


def run_instance_terraform(
    cmd: list[str], cwd: Path, *, timeout: int = 900, stage: str = "terraform"
) -> subprocess.CompletedProcess[str]:
    terraform_env = (
        {"TF_VAR_password": state.GENERATED_INSTANCE_PASSWORD}
        if state.GENERATED_INSTANCE_PASSWORD
        else None
    )
    return run(cmd, cwd, timeout=timeout, stage=stage, extra_env=terraform_env)


def write_workspace(check: Check) -> Path:
    workspace = state.RUN_ROOT / check.name
    workspace.mkdir(parents=True, exist_ok=True)
    module_source = (state.MODULES / check.module).resolve().as_posix()
    var_blocks = "\n".join(f'variable "{key}" {{}}' for key in sorted(check.vars))
    args = "\n".join(f"  {key} = var.{key}" for key in sorted(check.vars))
    (workspace / "main.tf").write_text(
        f"""
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    fptcloud = {{
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }}
  }}
}}

provider "fptcloud" {{}}

{var_blocks}

module "this" {{
  source = "{module_source}"
{args}
}}
""".lstrip(),
        encoding="utf-8",
    )
    (workspace / "terraform.tfvars.json").write_text(
        json.dumps(check.vars, indent=2),
        encoding="utf-8",
    )
    return workspace


def write_workspace_at(check: Check, workspace: Path) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    module_source = (state.MODULES / check.module).resolve().as_posix()
    var_blocks = "\n".join(
        'variable "password" {\n  default = null\n  sensitive = true\n}'
        if key == "password"
        else 'variable "tags" {\n  type    = map(string)\n  default = {}\n}'
        if key == "tags"
        else f'variable "{key}" {{}}'
        for key in sorted(check.vars)
    )
    args = "\n".join(f"  {key} = var.{key}" for key in sorted(check.vars))
    (workspace / "main.tf").write_text(
        f"""
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    fptcloud = {{
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }}
  }}
}}

provider "fptcloud" {{}}

{var_blocks}

module "this" {{
  source = "{module_source}"
{args}
}}
""".lstrip(),
        encoding="utf-8",
    )
    (workspace / "terraform.tfvars.json").write_text(
        json.dumps(
            {key: value for key, value in check.vars.items() if key != "password"}, indent=2
        ),
        encoding="utf-8",
    )
    return workspace


def write_import_workspace(instance_id: str, vpc_id: str, workspace: Path) -> Path:
    """Write a minimal Terraform workspace for import + targeted destroy.

    The workspace contains only fptcloud_instance.target (no module, no tags).
    Only fptcloud_instance is imported — tags are left as orphans (spec §7.3).
    """
    workspace.mkdir(parents=True, exist_ok=True)
    module_source = (state.MODULES / "vm").resolve().as_posix()
    (workspace / "main.tf").write_text(
        f"""
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    fptcloud = {{
      source  = "fpt-corp/fptcloud"
      version = "~> 0.3"
    }}
  }}
}}

provider "fptcloud" {{}}

variable "vpc_id" {{}}
variable "name" {{}}
variable "image_name" {{ default = "placeholder" }}
variable "flavor_name" {{ default = "placeholder" }}
variable "storage_policy_id" {{ default = "placeholder" }}
variable "disk_gb" {{ default = 40 }}
variable "subnet_id" {{ default = "placeholder" }}
variable "status" {{ default = "POWERED_ON" }}
variable "password" {{ default = null; sensitive = true }}
variable "ssh_key" {{ default = null }}
variable "security_group_ids" {{ default = [] }}
variable "tags" {{ type = map(string); default = {{}} }}

module "this" {{
  source             = "{module_source}"
  vpc_id             = var.vpc_id
  name               = var.name
  image_name         = var.image_name
  flavor_name        = var.flavor_name
  storage_policy_id  = var.storage_policy_id
  disk_gb            = var.disk_gb
  subnet_id          = var.subnet_id
  status             = var.status
  password           = var.password
  ssh_key            = var.ssh_key
  security_group_ids = var.security_group_ids
  tags               = var.tags
}}
""".lstrip(),
        encoding="utf-8",
    )
    (workspace / "terraform.tfvars.json").write_text(
        json.dumps({"vpc_id": vpc_id, "name": "reclaim-placeholder"}, indent=2),
        encoding="utf-8",
    )
    return workspace


def terraform_reclaim_import_destroy(
    instance_id: str,
    vpc_id: str,
    workspace: Path,
    *,
    stage_prefix: str = "compute.reclaim-health-check-instance",
) -> bool:
    """Import an existing HC instance into an ephemeral workspace, then destroy it.

    Returns True on success, False (fail-closed) on any error.
    Governed by C-013 / FR-003: deletion via Terraform import+destroy only.
    No direct REST DELETE is used.
    """
    resources = [f"fptcloud_instance:{instance_id}"]

    init = run(
        ["terraform", "init", "-input=false", "-no-color"], workspace, stage=f"{stage_prefix}-init"
    )
    if init.returncode != 0:
        emit(
            stage_prefix,
            "failed",
            f"Classification: health_check_instance_delete_failed; "
            f"reason=terraform_init_failed; instance_id={instance_id}; "
            f"{(init.stderr or init.stdout)[-800:]}",
            resources,
        )
        return False

    # Try terraform import.  If import itself is not supported, hard-stop (C-013/approval).
    import_result = run(
        [
            "terraform",
            "import",
            "-no-color",
            "-input=false",
            "module.this.fptcloud_instance.this",
            instance_id,
        ],
        workspace,
        stage=f"{stage_prefix}-import",
    )
    if import_result.returncode != 0:
        stderr = import_result.stderr or import_result.stdout
        unsupported = any(
            kw in stderr.lower()
            for kw in (
                "import is not supported",
                "does not support import",
                "not importable",
                "cannot import",
            )
        )
        if unsupported:
            emit(
                stage_prefix,
                "failed",
                f"Classification: instance_reclaim_import_unsupported; "
                f"reason=provider_does_not_support_fptcloud_instance_import; "
                f"instance_id={instance_id}; hard_stop=true; "
                f"{stderr[-800:]}",
                resources,
            )
            emit(
                "reclaim.import_unsupported",
                "failed",
                f"instance_id={instance_id}; vpc_id={vpc_id}; no_deletion_performed=true",
                resources,
            )
            return False
        emit(
            stage_prefix,
            "failed",
            f"Classification: health_check_instance_delete_failed; "
            f"reason=terraform_import_failed; instance_id={instance_id}; "
            f"{stderr[-800:]}",
            resources,
        )
        return False

    # Import succeeded — now destroy only the imported instance resource.
    destroy_result = run(
        [
            "terraform",
            "destroy",
            "-auto-approve",
            "-no-color",
            "-input=false",
            "-target=module.this.fptcloud_instance.this",
        ],
        workspace,
        stage=f"{stage_prefix}-destroy",
    )
    if destroy_result.returncode != 0:
        emit(
            stage_prefix,
            "failed",
            f"Classification: health_check_instance_delete_failed; "
            f"reason=terraform_destroy_failed; instance_id={instance_id}; "
            f"{(destroy_result.stderr or destroy_result.stdout)[-800:]}",
            resources,
        )
        return False

    return True


def planned_resources(workspace: Path) -> list[str]:
    result = run(
        ["terraform", "show", "-json", "-no-color", "tfplan"], workspace, stage="show-plan"
    )
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout or "{}")
    resources = []
    for change in data.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if "create" in actions or "update" in actions:
            resources.append(change.get("address", "unknown"))
    return resources


def state_resources(workspace: Path) -> list[str]:
    result = run(["terraform", "state", "list"], workspace, timeout=120, stage="state-list")
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def state_json(workspace: Path) -> dict[str, Any]:
    result = run(
        ["terraform", "show", "-json", "-no-color"], workspace, timeout=120, stage="show-state"
    )
    if result.returncode != 0:
        return {}
    return json.loads(result.stdout or "{}")


def module_resources(data: dict[str, Any]) -> list[dict[str, Any]]:
    root = data.get("values", {}).get("root_module", {})
    found: list[dict[str, Any]] = []

    def walk(module: dict[str, Any]) -> None:
        found.extend(module.get("resources", []))
        for child in module.get("child_modules", []):
            walk(child)

    walk(root)
    return found


def resource_statuses(workspace: Path) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for resource in module_resources(state_json(workspace)):
        address = str(resource.get("address", "unknown"))
        values = resource.get("values", {})
        raw = values.get("status")
        if raw is None:
            raw = values.get("state")
        if raw is None:
            statuses[address] = "NO_STATUS"
        elif isinstance(raw, bool):
            statuses[address] = "READY" if raw else "PENDING"
        else:
            statuses[address] = str(raw).upper()
    return statuses


def readiness(workspace: Path) -> tuple[bool, str, list[str]]:
    resources = state_resources(workspace)
    if not resources:
        return True, "no managed resources in state", []
    statuses = resource_statuses(workspace)
    unknown_or_pending = [
        f"{resource}={status}"
        for resource, status in statuses.items()
        if status in PENDING_STATUSES or status not in READY_STATUSES | {"NO_STATUS"}
    ]
    if unknown_or_pending:
        return False, "; ".join(unknown_or_pending), resources
    return True, "resources are ready", resources


def destroy(workspace: Path, name: str) -> None:
    resources = state_resources(workspace)
    emit(f"{name}:destroy", "started", "Destroying created resources", resources)
    result = run(
        ["terraform", "destroy", "-auto-approve", "-no-color", "-input=false"],
        workspace,
        stage=f"{name}-destroy",
    )
    if result.returncode == 0:
        emit(f"{name}:destroy", "destroyed", "Destroy completed", resources)
    else:
        queue_error(
            f"{name}:destroy", workspace, resources, (result.stderr or result.stdout)[-1200:]
        )


def instance_id_from_state(workspace: Path) -> str:
    for resource in module_resources(state_json(workspace)):
        if resource.get("type") == "fptcloud_instance":
            return str(resource.get("values", {}).get("id") or "")
    return ""


def instance_state_values(workspace: Path) -> dict[str, Any]:
    for resource in module_resources(state_json(workspace)):
        if resource.get("type") == "fptcloud_instance":
            values = resource.get("values", {})
            return dict(values) if isinstance(values, dict) else {}
    return {}


def inspect_provider_quota_capabilities() -> dict[str, Any]:
    # Spec-approved local schema inspection; no cloud/API call is made.
    capabilities = {
        "provider_schema_quota_fields": [],
        "provider_instance_inventory": "not_checked",
        "provider_storage_inventory": "not_checked",
    }
    try:
        raw = subprocess.check_output(
            ["terraform", "providers", "schema", "-json"],
            cwd=state.MODULES / "vm",
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        schema = json.loads(raw)["provider_schemas"]["registry.terraform.io/fpt-corp/fptcloud"]
    except (KeyError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return capabilities
    quota_fields: list[str] = []
    for schema_group in ("resource_schemas", "data_source_schemas"):
        for name, value in schema.get(schema_group, {}).items():
            attrs = (value.get("block") or {}).get("attributes", {})
            for attr in attrs:
                if "quota" in attr.lower():
                    quota_fields.append(f"{schema_group}.{name}.{attr}")
    data_sources = schema.get("data_source_schemas", {})
    instance_attrs = (data_sources.get("fptcloud_instance", {}).get("block") or {}).get(
        "attributes", {}
    )
    storage_attrs = (data_sources.get("fptcloud_storage", {}).get("block") or {}).get(
        "attributes", {}
    )
    capabilities["provider_schema_quota_fields"] = quota_fields
    capabilities["provider_instance_inventory"] = (
        "single_lookup_only" if instance_attrs else "not_available"
    )
    capabilities["provider_storage_inventory"] = (
        "single_lookup_only" if storage_attrs else "not_available"
    )
    return capabilities


class ResourceLock:
    def __init__(self, name: str) -> None:
        self.name = name
        self.path = state.LOCK_ROOT / f"{safe_name(name)}.lock"
        self.acquired = False

    def __enter__(self) -> ResourceLock:
        state.LOCK_ROOT.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(f"resource group is already locked: {self.name}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{state.RUN_ID}\n{now()}\n")
        self.acquired = True
        emit(f"{self.name}:lock", "locked", "Resource-group lock acquired", [str(self.path)])
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            emit(f"{self.name}:lock", "unlocked", "Resource-group lock released", [str(self.path)])
