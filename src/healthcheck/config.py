"""Environment, dotenv, spec constants, and normalized runtime settings.

No Terraform or cloud-mutation logic. Reads the spec catalog's ``constants``
block and exposes typed accessors used across the package.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from diagnose_health_inputs import DOTENV_RESULT as INPUT_DOTENV_RESULT
from diagnose_health_inputs import effective_config

from healthcheck import state

# Password generation primitives are surfaced through config for the spec module
# map; they physically live in the leaf ``state`` module.
from healthcheck.state import (  # noqa: F401  (re-exported for the facade / spec map)
    GENERATED_INSTANCE_PASSWORD,
    PASSWORD_MIN_LENGTH,
    PASSWORD_SPECIALS,
    generate_instance_password,
)


def parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return (key, value) if key else None


def load_dotenv(path: Path = state.ROOT / ".env", *, override: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "cwd": str(Path.cwd()),
        "path": str(path),
        "found": path.exists(),
        "loaded": [],
        "skipped_existing": [],
    }
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        parsed = parse_dotenv_line(raw)
        if not parsed:
            continue
        key, value = parsed
        if key in os.environ and not override:
            result["skipped_existing"].append(key)
            continue
        os.environ[key] = value
        result["loaded"].append(key)
    return result


def env_present(requirement: str) -> bool:
    if " or " in requirement:
        return any(env_present(part.strip()) for part in requirement.split(" or "))
    return bool(os.environ.get(requirement, "").strip())


def env_presence_report() -> str:
    parts = []
    for requirement in state.REQUIRED_ENV_PRESENCE:
        parts.append(f"{requirement}={'present' if env_present(requirement) else 'missing'}")
    parts.append(
        f"generated_instance_password={'present' if state.GENERATED_INSTANCE_PASSWORD else 'missing'}"
    )
    parts.append(
        "password_generated=True"
        if state.GENERATED_INSTANCE_PASSWORD
        else "password_generated=False"
    )
    parts.append("password_redacted=True")
    return "; ".join(parts)


DOTENV_RESULT = INPUT_DOTENV_RESULT if INPUT_DOTENV_RESULT.get("found") else load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str) -> bool:
    return env(name).lower() == "true"


def env_bool_default(name: str, default: bool) -> bool:
    raw = env(name)
    if not raw:
        return default
    return raw.lower() == "true"


def keep_instance_enabled() -> bool:
    # specs/health-check.json INSTANCE_CLEANUP_POLICY: HC_KEEP_INSTANCE defaults to true.
    return env_bool_default("HC_KEEP_INSTANCE", True)


def cleanup_on_quota_exceeded_enabled() -> bool:
    # specs/health-check.json INSTANCE_CLEANUP_POLICY: HC_CLEANUP_ON_QUOTA_EXCEEDED defaults to false.
    return env_bool_default("HC_CLEANUP_ON_QUOTA_EXCEEDED", False)


def spec_constants(path: Path = state.SPEC_PATH) -> dict[str, Any]:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("constants", {})


def instance_batching_policy() -> dict[str, Any]:
    # Governed by specs/health-check.json INSTANCE_BATCHING_POLICY.
    policy = spec_constants().get("INSTANCE_BATCHING_POLICY", {})
    return policy if isinstance(policy, dict) else {}


def env_int_default(name: str, default: int) -> int:
    raw = env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def batching_int(name: str, fallback: int) -> int:
    value = instance_batching_policy().get(name, fallback)
    try:
        default = int(value)
    except (TypeError, ValueError):
        default = fallback
    return max(0, env_int_default(name, default))


def instances_per_apply() -> int:
    # specs/health-check.json INSTANCE_BATCHING_POLICY: health checks create exactly one VM per apply.
    return 1


def stop_on_quota_exceeded_enabled() -> bool:
    # Quota handling is optimistic-apply-only: any provider-side quota rejection
    # stops the run and waits for explicit user confirmation.
    return True


def instance_selection_order() -> list[str]:
    order = instance_batching_policy().get("selection_order", [])
    if isinstance(order, list) and order:
        return [str(item) for item in order]
    return [
        "windows-2012",
        "windows-2016",
        "windows-2019",
        "windows-2022",
        "ubuntu-20-04",
        "ubuntu-22-04",
    ]


def max_subnet_candidate_attempts() -> int:
    value = spec_constants().get("MAX_SUBNET_CANDIDATE_ATTEMPTS")
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, attempts)


def cloud_context() -> dict[str, str]:
    return {
        "tenant": env("FPTCLOUD_TENANT_NAME"),
        "region": env("FPTCLOUD_REGION"),
        "vpc_id": state.run_context.get("effective_vpc_id") or effective_config()["vpc_id"],
    }


def rolling_strategy_constants() -> dict[str, Any]:
    """Read ROLLING_INSTANCE_STRATEGY from health-check.json constants (C-014)."""
    return dict(spec_constants().get("ROLLING_INSTANCE_STRATEGY") or {})


def root_disk_size_source() -> str:
    if env("HC_INSTANCE_DISK_SIZE_GB"):
        return "HC_INSTANCE_DISK_SIZE_GB"
    if env("HC_ROOT_DISK_SIZE"):
        return "HC_ROOT_DISK_SIZE"
    return "default"


def root_disk_size() -> tuple[int | None, str]:
    # Governed by specs/health-check.json INSTANCE_BATCHING_POLICY and INSTANCE_QUOTA_INSPECTION_POLICY.
    policy_default = instance_batching_policy().get(
        "HC_INSTANCE_DISK_SIZE_GB",
        spec_constants()
        .get("INSTANCE_QUOTA_INSPECTION_POLICY", {})
        .get("HC_INSTANCE_DISK_SIZE_GB_default", 40),
    )
    raw = env("HC_INSTANCE_DISK_SIZE_GB") or env("HC_ROOT_DISK_SIZE") or str(policy_default)
    try:
        value = int(raw)
    except ValueError:
        return None, f"{root_disk_size_source()} must be an integer"
    if value < 10:
        return None, f"{root_disk_size_source()} must be at least 10"
    return value, ""


def official_instance_disk_size_gb() -> int:
    # Governed by specs/health-check.json INSTANCE_BATCHING_POLICY.
    try:
        return int(instance_batching_policy().get("HC_INSTANCE_DISK_SIZE_GB", 40) or 40)
    except (TypeError, ValueError):
        return 40


def reduced_disk_test(disk_size: int | None = None) -> bool:
    value = disk_size if disk_size is not None else root_disk_size()[0]
    official = official_instance_disk_size_gb()
    return bool(value and value != official)
