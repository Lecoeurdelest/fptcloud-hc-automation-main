"""Environment, dotenv, spec constants, and normalized runtime settings.

No Terraform or cloud-mutation logic. Reads the spec catalog's ``constants``
block and exposes typed accessors used across the package.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - project runtime is Python 3.11.
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

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


def runtime_config_path() -> Path:
    configured = os.environ.get("HC_CONFIG_TOML", "").strip()
    return Path(configured) if configured else state.ROOT / "healthcheck.toml"


def load_runtime_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or runtime_config_path()
    result: dict[str, Any] = {
        "path": str(config_path),
        "found": config_path.exists(),
        "loaded": False,
        "error": "",
        "data": {},
    }
    if not config_path.exists():
        return result
    if tomllib is None:
        result["error"] = "Python 3.11 tomllib or tomli is required to read healthcheck.toml"
        return result
    try:
        result["data"] = tomllib.loads(config_path.read_text(encoding="utf-8"))
        result["loaded"] = True
    except (OSError, tomllib.TOMLDecodeError) as exc:
        result["error"] = str(exc)
    return result


RUNTIME_CONFIG_RESULT = load_runtime_config()


def runtime_config() -> dict[str, Any]:
    data = RUNTIME_CONFIG_RESULT.get("data", {})
    return data if isinstance(data, dict) else {}


def phase_config(stage_id: str) -> dict[str, Any]:
    phases = runtime_config().get("phases", {})
    if not isinstance(phases, dict):
        return {}
    raw = phases.get(stage_id, {})
    return raw if isinstance(raw, dict) else {}


def phase_value(stage_id: str, key: str, default: Any = None) -> Any:
    return phase_config(stage_id).get(key, default)


def phase_bool(stage_id: str, key: str, default: bool) -> bool:
    raw = phase_value(stage_id, key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def phase_int(stage_id: str, key: str, default: int, *, minimum: int = 0) -> int:
    raw = phase_value(stage_id, key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def phase_string_list(stage_id: str, key: str) -> list[str]:
    raw = phase_value(stage_id, key, [])
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def runtime_string_list(section: str, key: str) -> list[str]:
    raw_section = runtime_config().get(section, {})
    if not isinstance(raw_section, dict):
        return []
    raw = raw_section.get(key, [])
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def target_vpcs() -> list[str]:
    raw = env("VPC_IDS") or env("VPC_ID")
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return runtime_string_list("targets", "vpcs")


def _constraint_ok(actual: Any, op: str, expected: Any) -> bool:
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op in {"<", "<=", ">", ">="}:
        try:
            left = float(actual)
            right = float(expected)
        except (TypeError, ValueError):
            return False
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        return left >= right
    if op == "in":
        return isinstance(expected, list) and actual in expected
    if op == "not_in":
        return isinstance(expected, list) and actual not in expected
    return False


def validate_phase_constraints(stage_id: str, values: dict[str, Any] | None = None) -> list[str]:
    phase = phase_config(stage_id)
    constraints = phase.get("constraints", [])
    if not isinstance(constraints, list):
        return [f"{stage_id}: constraints must be a list"]
    merged = dict(phase)
    if values:
        merged.update(values)
    errors: list[str] = []
    for index, constraint in enumerate(constraints, start=1):
        if not isinstance(constraint, dict):
            errors.append(f"{stage_id}: constraint #{index} must be an object")
            continue
        key = str(constraint.get("key", "")).strip()
        op = str(constraint.get("op", "")).strip()
        expected = constraint.get("value")
        message = str(constraint.get("message") or "").strip()
        if not key or not op:
            errors.append(f"{stage_id}: constraint #{index} requires key and op")
            continue
        actual = merged.get(key)
        if not _constraint_ok(actual, op, expected):
            detail = message or f"{key} {op} {expected!r} failed (actual={actual!r})"
            errors.append(detail)
    return errors


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
    if env("HC_KEEP_INSTANCE"):
        return env_bool_default("HC_KEEP_INSTANCE", True)
    delete_after_create = phase_bool(state.INSTANCE_CREATE_STAGE, "delete_after_create", False)
    if delete_after_create:
        return False
    # specs/health-check.json INSTANCE_CLEANUP_POLICY: HC_KEEP_INSTANCE defaults to true.
    return env_bool_default("HC_KEEP_INSTANCE", True)


def cleanup_on_quota_exceeded_enabled() -> bool:
    if not env("HC_CLEANUP_ON_QUOTA_EXCEEDED"):
        return phase_bool(
            state.INSTANCE_CREATE_STAGE,
            "cleanup_on_quota_exceeded",
            False,
        )
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
    default = batching_int("HC_INSTANCES_PER_APPLY", 1)
    if env("HC_INSTANCES_PER_APPLY"):
        return max(1, default)
    return phase_int(state.INSTANCE_CREATE_STAGE, "instances_per_apply", default, minimum=1)


def stop_on_quota_exceeded_enabled() -> bool:
    if "stop_on_quota_exceeded" in phase_config(state.INSTANCE_CREATE_STAGE):
        return phase_bool(state.INSTANCE_CREATE_STAGE, "stop_on_quota_exceeded", True)
    # Quota handling is optimistic-apply-only: any provider-side quota rejection
    # stops the run and waits for explicit user confirmation.
    return True


def instance_selection_order() -> list[str]:
    phase_order = phase_string_list(state.INSTANCE_CREATE_STAGE, "selection_order")
    if phase_order:
        return phase_order
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
    if phase_value("network.additional-subnet", "max_candidate_attempts", None) is not None:
        return phase_int("network.additional-subnet", "max_candidate_attempts", 1, minimum=1)
    value = spec_constants().get("MAX_SUBNET_CANDIDATE_ATTEMPTS")
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, attempts)


def additional_subnet_cidr() -> str:
    return env("HC_ADDITIONAL_SUBNET_CIDR") or str(
        phase_value("network.additional-subnet", "cidr", "")
    ).strip()


def additional_subnet_gateway() -> str:
    return env("HC_ADDITIONAL_SUBNET_GATEWAY") or str(
        phase_value("network.additional-subnet", "gateway_ip", "")
    ).strip()


def existing_subnet_cidrs() -> list[str]:
    raw = env("HC_EXISTING_SUBNET_CIDRS")
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return phase_string_list("network.additional-subnet", "existing_subnet_cidrs")


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
    if "disk_gb" in phase_config(state.INSTANCE_CREATE_STAGE):
        return "healthcheck.toml:phases.compute.create-instance.disk_gb"
    return "default"


def root_disk_size() -> tuple[int | None, str]:
    # Governed by specs/health-check.json INSTANCE_BATCHING_POLICY and INSTANCE_QUOTA_INSPECTION_POLICY.
    policy_default = instance_batching_policy().get(
        "HC_INSTANCE_DISK_SIZE_GB",
        spec_constants()
        .get("INSTANCE_QUOTA_INSPECTION_POLICY", {})
        .get("HC_INSTANCE_DISK_SIZE_GB_default", 40),
    )
    toml_disk_size = phase_value(state.INSTANCE_CREATE_STAGE, "disk_gb", None)
    raw = env("HC_INSTANCE_DISK_SIZE_GB") or env("HC_ROOT_DISK_SIZE") or toml_disk_size or str(policy_default)
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


def instance_delete_after_create_enabled() -> bool:
    return phase_bool(state.INSTANCE_CREATE_STAGE, "delete_after_create", False)


def instance_attach_subnet_enabled() -> bool:
    return phase_bool(state.INSTANCE_CREATE_STAGE, "attach_subnet", True)


def instance_assign_floating_ip_enabled() -> bool:
    return phase_bool(state.INSTANCE_CREATE_STAGE, "assign_floating_ip", False)


def instance_attach_security_group_enabled() -> bool:
    return phase_bool(state.INSTANCE_CREATE_STAGE, "attach_security_group", bool(env("HC_SECURITY_GROUP_ID")))


def instance_security_group_ids() -> list[str]:
    if env("HC_SECURITY_GROUP_ID"):
        return [env("HC_SECURITY_GROUP_ID")]
    if not instance_attach_security_group_enabled():
        return []
    return phase_string_list(state.INSTANCE_CREATE_STAGE, "security_group_ids")


def instance_resize_after_create_enabled() -> bool:
    return phase_bool(state.INSTANCE_CREATE_STAGE, "resize_after_create", False)


def instance_create_snapshot_enabled() -> bool:
    return phase_bool(state.INSTANCE_CREATE_STAGE, "create_snapshot", False)


def instance_add_nic_enabled() -> bool:
    return phase_bool(state.INSTANCE_CREATE_STAGE, "add_nic", False)


def instance_phase_runtime_values() -> dict[str, Any]:
    return {
        "delete_after_create": instance_delete_after_create_enabled(),
        "keep_instance": keep_instance_enabled(),
        "cleanup_on_quota_exceeded": cleanup_on_quota_exceeded_enabled(),
        "instances_per_apply": instances_per_apply(),
        "stop_on_quota_exceeded": stop_on_quota_exceeded_enabled(),
        "attach_subnet": instance_attach_subnet_enabled(),
        "assign_floating_ip": instance_assign_floating_ip_enabled(),
        "attach_security_group": instance_attach_security_group_enabled(),
        "security_group_ids": instance_security_group_ids(),
        "resize_after_create": instance_resize_after_create_enabled(),
        "create_snapshot": instance_create_snapshot_enabled(),
        "add_nic": instance_add_nic_enabled(),
        "disk_gb": root_disk_size()[0],
    }


def object_storage_regions() -> list[str]:
    raw = env("HC_ENABLED_OBJECT_REGIONS") or env("HC_OBJECT_REGION")
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    regions = phase_string_list("object-storage.bucket", "enabled_regions")
    if regions:
        return regions
    region = phase_value("object-storage.bucket", "region", "")
    return [str(region).strip()] if str(region).strip() else []


def object_storage_bucket_prefix() -> str:
    return env(
        "HC_OBJECT_BUCKET_PREFIX",
        str(phase_value("object-storage.bucket", "bucket_prefix", "hc-object")),
    )


def object_storage_test_key() -> str:
    return env(
        "HC_OBJECT_TEST_KEY",
        str(phase_value("object-storage.bucket", "test_key", "testfile.txt")),
    )


def object_storage_test_body() -> bytes:
    return env(
        "HC_OBJECT_TEST_BODY",
        str(
            phase_value(
                "object-storage.bucket",
                "test_body",
                "fptcloud health-check object storage probe",
            )
        ),
    ).encode(
        "utf-8"
    )


def s3_config() -> dict[str, str]:
    return {
        "endpoint": env("S3_ENDPOINT"),
        "region": env("S3_REGION"),
        "access_key": env("S3_ACCESS_KEY"),
        "secret_key": env("S3_SECRET_KEY"),
    }


def missing_s3_config() -> list[str]:
    values = s3_config()
    return [name.upper() for name, value in values.items() if not value]
