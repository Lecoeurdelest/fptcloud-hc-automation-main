from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "runs" / "diagnostics"
SPEC_PATH = ROOT / "specs" / "health-check.json"
LOCK_CANDIDATES = [
    ROOT / "modules" / "subnet" / ".terraform.lock.hcl",
    ROOT / "runs" / "fptcloud-connect-check" / ".terraform.lock.hcl",
]

SECRET_KEYS = ("TOKEN", "PASSWORD", "SECRET", "KEY_MATERIAL", "PRIVATE")
REDACT_EXACT = {"HC_SSH_KEY"}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


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
    return [value.strip() for value in raw.split(",") if value.strip()]


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
                "cidr": env("HC_ADDITIONAL_SUBNET_CIDR"),
                "gateway_ip": env("HC_ADDITIONAL_SUBNET_GATEWAY"),
                "vpc_id": selected_vpc,
            },
            "existing_subnet_cidrs": [
                value.strip()
                for value in env("HC_EXISTING_SUBNET_CIDRS").split(",")
                if value.strip()
            ],
            "max_subnet_candidate_attempts": spec_constants().get("MAX_SUBNET_CANDIDATE_ATTEMPTS"),
            "candidate_strategy": "deterministic increment by ten same-size network blocks, preserving gateway host offset",
            "will_run": bool(env("HC_ADDITIONAL_SUBNET_CIDR") and env("HC_ADDITIONAL_SUBNET_GATEWAY")),
        },
        "vm": {
            "required_env": ["HC_IMAGE_NAME", "HC_FLAVOR_NAME", "HC_UPSIZE_FLAVOR_NAME", "HC_SSH_KEY"],
            "configured": {
                "image_name": env("HC_IMAGE_NAME"),
                "flavor_name": env("HC_FLAVOR_NAME"),
                "upsize_flavor_name": env("HC_UPSIZE_FLAVOR_NAME"),
                "ssh_key": redact("HC_SSH_KEY", env("HC_SSH_KEY")),
                "subnet_id": subnet_id,
                "storage_policy_id": storage_policy_id,
            },
        },
        "object_storage": {
            "enabled_regions": [
                value.strip()
                for value in env("HC_ENABLED_OBJECT_REGIONS", env("HC_OBJECT_REGION", "")).split(",")
                if value.strip()
            ],
        },
    }


def diagnostics() -> dict[str, Any]:
    context = base_context()
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "provider": provider_lock(),
        "provider_config": context,
        "stage_inputs": stage_inputs(),
        "warnings": warnings(context),
    }


def warnings(context: dict[str, Any]) -> list[str]:
    result: list[str] = []
    if not context["explicit_vpc_id"] and not context["vpc_lookup_key"]:
        result.append("No HC_VPC_ID, HC_VPC_NAME, VPC_NAME, VPC_ID, or VPC_IDS lookup key configured.")
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
    if not env("HC_ENABLED_OBJECT_REGIONS") and not env("HC_OBJECT_REGION"):
        result.append("No enabled object-storage region configured; backup checks will be skipped.")
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
        "image_name": env("HC_IMAGE_NAME"),
        "flavor_name": env("HC_FLAVOR_NAME"),
        "upsize_flavor_name": env("HC_UPSIZE_FLAVOR_NAME"),
        "ssh_key": redact("HC_SSH_KEY", env("HC_SSH_KEY")),
        "enabled_object_regions": [
            value.strip()
            for value in env("HC_ENABLED_OBJECT_REGIONS", env("HC_OBJECT_REGION", "")).split(",")
            if value.strip()
        ],
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
