from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - local Python 3.9 fallback.
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "runs" / "diagnostics"
SPEC_PATH = ROOT / "specs" / "health-check.json"
LOCK_CANDIDATES = [
    ROOT / "modules" / "subnet" / ".terraform.lock.hcl",
    ROOT / "runs" / "fptcloud-connect-check" / ".terraform.lock.hcl",
]

SECRET_KEYS = ("TOKEN", "PASSWORD", "SECRET", "KEY_MATERIAL", "PRIVATE")
REDACT_EXACT = {"HC_SSH_KEY"}
REQUIRED_ENV_PRESENCE = (
    "FPTCLOUD_TOKEN",
    "FPTCLOUD_REGION",
    "FPTCLOUD_TENANT_NAME",
)

INSTANCE_IMAGE_MATRIX = (
    ("windows-2012", "HC_IMAGE_WINDOWS_2012"),
    ("windows-2016", "HC_IMAGE_WINDOWS_2016"),
    ("windows-2019", "HC_IMAGE_WINDOWS_2019"),
    ("windows-2022", "HC_IMAGE_WINDOWS_2022"),
    ("ubuntu-16-04", "HC_IMAGE_UBUNTU_16_04"),
    ("ubuntu-18-04", "HC_IMAGE_UBUNTU_18_04"),
    ("ubuntu-20-04", "HC_IMAGE_UBUNTU_20_04"),
    ("ubuntu-22-04", "HC_IMAGE_UBUNTU_22_04"),
)


def runtime_config() -> dict[str, Any]:
    path = Path(os.environ.get("HC_CONFIG_TOML") or ROOT / "healthcheck.toml")
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def phase_value(stage_id: str, key: str, default: Any = None) -> Any:
    phases = runtime_config().get("phases", {})
    raw = phases.get(stage_id, {}) if isinstance(phases, dict) else {}
    return raw.get(key, default) if isinstance(raw, dict) else default


def object_storage_regions() -> list[str]:
    raw = env("HC_ENABLED_OBJECT_REGIONS") or env("HC_OBJECT_REGION")
    if raw:
        return [value.strip() for value in raw.split(",") if value.strip()]
    raw_regions = phase_value("object-storage.bucket", "enabled_regions", [])
    if isinstance(raw_regions, list):
        regions = [str(value).strip() for value in raw_regions if str(value).strip()]
        if regions:
            return regions
    if isinstance(raw_regions, str) and raw_regions.strip():
        return [value.strip() for value in raw_regions.split(",") if value.strip()]
    raw_region = str(phase_value("object-storage.bucket", "region", "")).strip()
    return [raw_region] if raw_region else []


def phase_string_list(stage_id: str, key: str) -> list[str]:
    raw = phase_value(stage_id, key, [])
    if isinstance(raw, list):
        return [str(value).strip() for value in raw if str(value).strip()]
    if isinstance(raw, str) and raw.strip():
        return [value.strip() for value in raw.split(",") if value.strip()]
    return []


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
        return [value.strip() for value in raw.split(",") if value.strip()]
    return phase_string_list("network.additional-subnet", "existing_subnet_cidrs")


def runtime_string_list(section: str, key: str) -> list[str]:
    raw_section = runtime_config().get(section, {})
    if not isinstance(raw_section, dict):
        return []
    raw = raw_section.get(key, [])
    if isinstance(raw, list):
        return [str(value).strip() for value in raw if str(value).strip()]
    if isinstance(raw, str) and raw.strip():
        return [value.strip() for value in raw.split(",") if value.strip()]
    return []


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


def load_dotenv(path: Path = ROOT / ".env", *, override: bool = False) -> dict[str, Any]:
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


DOTENV_RESULT = load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_present(requirement: str) -> bool:
    if " or " in requirement:
        return any(env_present(part.strip()) for part in requirement.split(" or "))
    return bool(env(requirement))


def environment_diagnostics() -> dict[str, Any]:
    return {
        "cwd": DOTENV_RESULT.get("cwd", str(Path.cwd())),
        "dotenv_path": DOTENV_RESULT.get("path", str(ROOT / ".env")),
        "dotenv_found": bool(DOTENV_RESULT["found"]),
        "dotenv_loaded": bool(DOTENV_RESULT["loaded"]),
        "dotenv_loaded_count": len(DOTENV_RESULT["loaded"]),
        "dotenv_skipped_existing_count": len(DOTENV_RESULT["skipped_existing"]),
        "required_presence": {
            requirement: "present" if env_present(requirement) else "missing"
            for requirement in REQUIRED_ENV_PRESENCE
        },
    }


def redact(name: str, value: Any) -> Any:
    if name.upper() in REDACT_EXACT or any(part in name.upper() for part in SECRET_KEYS):
        return "<redacted>" if value else ""
    return value


def provider_lock() -> dict[str, str]:
    for path in LOCK_CANDIDATES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        source = re.search(r'provider "([^"]+)"', text)
        version = re.search(r'version\s+=\s+"([^"]+)"', text)
        constraints = re.search(r'constraints\s+=\s+"([^"]+)"', text)
        return {
            "lock_file": str(path),
            "source": source.group(1) if source else "",
            "version": version.group(1) if version else "",
            "constraints": constraints.group(1) if constraints else "",
        }
    return {"lock_file": "", "source": "", "version": "", "constraints": ""}


def spec_constants() -> dict[str, Any]:
    try:
        return json.loads(SPEC_PATH.read_text(encoding="utf-8")).get("constants", {})
    except (OSError, json.JSONDecodeError):
        return {}


def vpc_values() -> list[str]:
    raw = env("VPC_IDS") or env("VPC_ID")
    if raw:
        return [value.strip() for value in raw.split(",") if value.strip()]
    return runtime_string_list("targets", "vpcs")


def configured_vpc_lookup_key() -> str:
    values = vpc_values()
    return env("HC_VPC_NAME") or env("VPC_NAME") or env("VPC_ID") or (values[0] if values else "")


def explicit_vpc_id() -> str:
    return env("HC_VPC_ID")


def looks_uuid(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            value,
        )
    )


def base_context() -> dict[str, Any]:
    values = vpc_values()
    vpc_name = configured_vpc_lookup_key()
    selected_vpc = explicit_vpc_id()
    return {
        "api_endpoint": redact("FPTCLOUD_API_URL", env("FPTCLOUD_API_URL")),
        "region": env("FPTCLOUD_REGION"),
        "region_id": env("HC_REGION_ID"),
        "tenant_name": env("FPTCLOUD_TENANT_NAME"),
        "tenant_id": env("HC_TENANT_ID"),
        "vpc_name": vpc_name,
        "explicit_vpc_id": explicit_vpc_id(),
        "discovered_vpc_id": "",
        "effective_vpc_id": selected_vpc,
        "vpc_id": explicit_vpc_id(),
        "vpc_id_source": "explicit" if explicit_vpc_id() else "unresolved",
        "selected_vpc": selected_vpc,
        "selected_vpc_looks_uuid": looks_uuid(selected_vpc),
        "vpc_lookup_key": vpc_name,
        "all_vpc_values": values,
        "redis_url": env("REDIS_URL"),
    }


def stage_inputs() -> dict[str, Any]:
    context = base_context()
    selected_vpc = context["selected_vpc"]
    subnet_id = env("HC_SUBNET_ID")
    storage_policy_id = env("HC_STORAGE_POLICY_ID")
    instance_storage_policy_id = env("HC_INSTANCE_STORAGE_POLICY_ID")
    instance_storage_policy_name = env("HC_INSTANCE_STORAGE_POLICY_NAME")
    instance_storage_policy_db_id = env("HC_INSTANCE_STORAGE_POLICY_DB_ID")
    return {
        "discover_storage_policy": {
            "terraform_data_source": "data.fptcloud_storage_policy.this",
            "lookup_input": {"vpc_id": selected_vpc},
            "configured_storage_policy_id": storage_policy_id,
            "will_run": not bool(storage_policy_id),
            "hypothesis_if_404": "provider endpoint or data-source mismatch, or vpc_id is not in provider-expected format",
        },
        "discover_subnet": {
            "terraform_data_source": "data.fptcloud_subnet.this",
            "lookup_input": {"vpc_id": selected_vpc},
            "configured_subnet_id": subnet_id,
            "will_run": not bool(subnet_id),
            "hypothesis_if_system_error": "backend/provider mapping error, wrong region, or VPC identifier format mismatch",
        },
        "network": {
            "module_path": str(ROOT / "modules" / "subnet"),
            "terraform_resource": "module.this.fptcloud_subnet.this",
            "vars": {
                "name": env("HC_SUBNET_NAME", f"hc-net-diagnostic-{time.strftime('%Y%m%d%H%M%S')}"),
                "cidr": env("HC_SUBNET_CIDR", "172.26.222.0/24"),
                "gateway_ip": env("HC_SUBNET_GATEWAY", "172.26.222.1"),
                "type": env("HC_SUBNET_TYPE", "NAT_ROUTED"),
                "vpc_id": selected_vpc,
            },
        },
        "additional_subnet": {
            "module_path": str(ROOT / "modules" / "subnet"),
            "terraform_resource": "module.this.fptcloud_subnet.this",
            "vars": {
                "cidr": additional_subnet_cidr(),
                "gateway_ip": additional_subnet_gateway(),
                "vpc_id": selected_vpc,
            },
            "existing_subnet_cidrs": existing_subnet_cidrs(),
            "max_subnet_candidate_attempts": spec_constants().get("MAX_SUBNET_CANDIDATE_ATTEMPTS"),
            "candidate_strategy": "deterministic increment by ten same-size network blocks, preserving gateway host offset",
            "will_run": bool(additional_subnet_cidr() and additional_subnet_gateway()),
        },
        "vm": {
            "required_env": [
                "generated_instance_password",
                "discovered_instance_flavor",
                "discovered_instance_images",
            ],
            "configured": {
                "images": {
                    label: {"env_var": var_name, "value": env(var_name)}
                    for label, var_name in INSTANCE_IMAGE_MATRIX
                },
                "flavor_id": env("HC_FLAVOR_ID"),
                "flavor_name": env("HC_FLAVOR_NAME"),
                "upsize_flavor_name": env("HC_UPSIZE_FLAVOR_NAME"),
                "ssh_key": redact("HC_SSH_KEY", env("HC_SSH_KEY")),
                "password_policy": "generated_by_runner",
                "subnet_id": subnet_id,
                "storage_policy_id": storage_policy_id,
                "instance_storage_policy_id": instance_storage_policy_id,
                "instance_storage_policy_name": instance_storage_policy_name,
                "instance_storage_policy_db_id": instance_storage_policy_db_id,
                "instance_storage_policy_provider_field": env("HC_INSTANCE_STORAGE_POLICY_PROVIDER_FIELD", "id"),
                "root_disk_size": env("HC_ROOT_DISK_SIZE", "40"),
                "keep_instance": env("HC_KEEP_INSTANCE", "false"),
            },
        },
        "object_storage": {
            "enabled_regions": object_storage_regions(),
        },
    }


def diagnostics() -> dict[str, Any]:
    context = base_context()
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": environment_diagnostics(),
        "provider": provider_lock(),
        "provider_config": context,
        "stage_inputs": stage_inputs(),
        "warnings": warnings(context),
    }


def warnings(context: dict[str, Any]) -> list[str]:
    result: list[str] = []
    if not context["explicit_vpc_id"] and not context["vpc_lookup_key"]:
        result.append("No HC_VPC_ID, HC_VPC_NAME, VPC_NAME, VPC_ID, VPC_IDS, or healthcheck.toml [targets].vpcs lookup key configured.")
    elif not context["explicit_vpc_id"]:
        result.append("HC_VPC_ID is not configured; compute.discover-vpc must resolve data.fptcloud_vpc.this.id before dependent stages run.")
    elif not context["selected_vpc_looks_uuid"]:
        result.append("Explicit HC_VPC_ID is not UUID-shaped; verify it is the provider VPC ID, not the VPC display name.")
    if not context["tenant_id"]:
        result.append("HC_TENANT_ID is not configured. The provider currently uses tenant_name, but support may ask for tenant ID evidence.")
    if not context["region_id"]:
        result.append("HC_REGION_ID is not configured. The provider uses FPTCLOUD_REGION; set HC_REGION_ID if FPT support distinguishes display region from internal ID.")
    if context["region"] not in {"VN/HAN", "VN/SGN", "JP/JCSI2"}:
        result.append("Region is not one of the provider-documented values: VN/HAN, VN/SGN, JP/JCSI2.")
    if not env("HC_STORAGE_POLICY_ID"):
        result.append("HC_STORAGE_POLICY_ID is not configured; storage and VM checks depend on storage-policy discovery.")
    if not env("HC_SUBNET_ID"):
        result.append("HC_SUBNET_ID is not configured; security-group and VM checks depend on subnet discovery or network creation.")
    if not object_storage_regions():
        result.append("No enabled object-storage region configured; object-storage checks will be skipped.")
    return result


def effective_config() -> dict[str, Any]:
    context = base_context()
    return {
        "tenant_name": context["tenant_name"],
        "tenant_id": context["tenant_id"],
        "region": context["region"],
        "region_id": context["region_id"],
        "vpc_name": context["vpc_name"],
        "explicit_vpc_id": context["explicit_vpc_id"],
        "discovered_vpc_id": context["discovered_vpc_id"],
        "effective_vpc_id": context["effective_vpc_id"],
        "vpc_id": context["effective_vpc_id"],
        "vpc_id_source": context["vpc_id_source"],
        "subnet_id": env("HC_SUBNET_ID"),
        "storage_policy_id": env("HC_STORAGE_POLICY_ID"),
        "instance_storage_policy_id": env("HC_INSTANCE_STORAGE_POLICY_ID"),
        "instance_storage_policy_name": env("HC_INSTANCE_STORAGE_POLICY_NAME"),
        "instance_storage_policy_db_id": env("HC_INSTANCE_STORAGE_POLICY_DB_ID"),
        "images": {
            label: env(var_name)
            for label, var_name in INSTANCE_IMAGE_MATRIX
        },
        "flavor_id": env("HC_FLAVOR_ID"),
        "flavor_name": env("HC_FLAVOR_NAME"),
        "upsize_flavor_name": env("HC_UPSIZE_FLAVOR_NAME"),
        "ssh_key": redact("HC_SSH_KEY", env("HC_SSH_KEY")),
        "enabled_object_regions": object_storage_regions(),
    }


def write_report(data: dict[str, Any]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    latest = OUT_DIR / "latest.json"
    latest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Render sanitized FPT Cloud health-check diagnostics without apply.")
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout.")
    args = parser.parse_args()
    data = diagnostics()
    path = write_report(data)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"Wrote diagnostics: {path}")
        print(f"Provider: {data['provider']['source']} {data['provider']['version']}")
        for warning in data["warnings"]:
            print(f"WARNING: {warning}")


if __name__ == "__main__":
    main()
