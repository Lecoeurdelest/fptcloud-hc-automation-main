"""Per-run constants, paths, status sets, stage IDs, and shared mutable state.

This is the single owner of run-scoped state. Other modules read and mutate it
via ``from healthcheck import state`` so there is exactly one canonical object
per run. Mutable containers (``events``, ``run_context``, …) keep their identity
when re-exported by the facade, so existing tests that mutate ``runner.events``
keep working.

Rebindable scalars that the tests patch (``RUN_ROOT``, ``SETTLE_SECONDS``) must
be referenced as ``state.RUN_ROOT`` / ``state.SETTLE_SECONDS`` by callers so a
single ``monkeypatch.setattr(healthcheck.state, ...)`` affects every module.

Leaf module: imports only stdlib + healthcheck.models. Password generation lives
here (no peer deps) and is surfaced through ``config`` for the spec's module map.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from string import ascii_lowercase, digits
from typing import Any

from healthcheck.models import QueueItem


def project_root() -> Path:
    configured = os.environ.get("HC_PROJECT_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "specs" / "health-check.json").exists() and (cwd / "modules").exists():
        return cwd
    package_root = Path(__file__).resolve().parents[1]
    if (package_root / "specs" / "health-check.json").exists():
        return package_root
    return Path(__file__).resolve().parents[2]


ROOT = project_root()
RUN_ID = time.strftime("hc-%Y%m%d-%H%M%S")
RUN_STARTED_AT = time.monotonic()
RUN_ROOT = ROOT / "runs" / RUN_ID
INSTANCE_RUN_SUFFIX = "".join(secrets.choice(ascii_lowercase + digits) for _ in range(12))
PASSWORD_SPECIALS = "?@*%$&!#"
PASSWORD_MIN_LENGTH = 12


def generate_instance_password(length: int = 24) -> str:
    if length < PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"generated instance password length must be at least {PASSWORD_MIN_LENGTH}"
        )
    alphabet = ascii_lowercase + ascii_lowercase.upper() + digits + PASSWORD_SPECIALS
    required = [
        secrets.choice(ascii_lowercase),
        secrets.choice(ascii_lowercase.upper()),
        secrets.choice(digits),
        secrets.choice(PASSWORD_SPECIALS),
    ]
    remaining = [secrets.choice(alphabet) for _ in range(length - len(required))]
    chars = required + remaining
    for index in range(len(chars) - 1, 0, -1):
        swap = secrets.randbelow(index + 1)
        chars[index], chars[swap] = chars[swap], chars[index]
    return "".join(chars)


GENERATED_INSTANCE_PASSWORD = generate_instance_password()
LOG_PATH = ROOT / "log.html"
JSON_LOG_PATH = ROOT / "log.json"
MODULES = ROOT / "modules"
TEMPLATE = ROOT / "src" / "hc" / "reporter" / "html_log.html"
LOCK_ROOT = ROOT / "runs" / ".locks"
SPEC_PATH = ROOT / "specs" / "health-check.json"


SECRET_ENV_PARTS = ("TOKEN", "PASSWORD", "SECRET", "KEY_MATERIAL", "PRIVATE")
SECRET_ENV_EXACT = {"HC_SSH_KEY"}
REQUIRED_ENV_PRESENCE = (
    "FPTCLOUD_TOKEN",
    "FPTCLOUD_REGION",
    "FPTCLOUD_TENANT_NAME",
)


SETTLE_SECONDS = int(os.environ.get("HC_SETTLE_SECONDS", "20"))
PENDING_POLL_SECONDS = int(os.environ.get("HC_PENDING_POLL_SECONDS", "15"))
PENDING_TIMEOUT_SECONDS = int(os.environ.get("HC_PENDING_TIMEOUT_SECONDS", "300"))
# Governed by specs/health-check.json INSTANCE_ERROR_QUEUE_RETRY_POLICY.max_attempts_per_case.
MAX_INSTANCE_CREATE_ATTEMPTS = 3

READY_STATUSES = {
    "ACTIVE",
    "AVAILABLE",
    "COMPLETED",
    "CREATED",
    "ENABLED",
    "OK",
    "PASS",
    "PASSED",
    "POWERED_ON",
    "READY",
    "RUNNING",
    "SUCCESS",
}
PENDING_STATUSES = {
    "BUILD",
    "BUILDING",
    "CREATE",
    "CREATING",
    "DEPLOYING",
    "IN_PROGRESS",
    "PENDING",
    "PROVISIONING",
    "STARTING",
    "UPDATING",
}

SUBNET_VALIDATION_STAGE = "compute.validate-subnet-inputs"
SUBNET_CREATE_STAGE = "compute.create-subnet"
SUBNET_EVIDENCE_STAGE = "compute.collect-subnet-create-evidence"
VPC_DISCOVERY_STAGE = "compute.discover-vpc"
EXISTING_SUBNETS_STAGE = "network.discover-existing-subnets"
INSTANCE_VALIDATE_STAGE = "compute.validate-instance-inputs"
INSTANCE_PASSWORD_POLICY_STAGE = "compute.validate-instance-password-policy"
INSTANCE_CREATE_STAGE = "compute.create-instance"
INSTANCE_CLEANUP_STAGE = "compute.cleanup-instance"
INSTANCE_IMAGE_DISCOVERY_STAGE = "compute.discover-instance-images"
INSTANCE_FLAVOR_DISCOVERY_STAGE = "compute.discover-instance-flavor"
INSTANCE_NETWORK_VALIDATE_STAGE = "compute.validate-instance-network-inputs"
INSTANCE_STORAGE_POLICY_VALIDATE_STAGE = "compute.validate-instance-storage-policy"
INSTANCE_HOSTNAME_VALIDATE_STAGE = "compute.validate-instance-hostname"
INSTANCE_QUOTA_INSPECT_STAGE = "compute.inspect-instance-quota"
INSTANCE_QUOTA_VALIDATE_STAGE = "compute.validate-instance-quota"
INSTANCE_ROUND_SELECT_STAGE = "compute.select-instance-round"
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


# ── Shared mutable run state ──────────────────────────────────────────────────
events: list[dict[str, str]] = []
pending_queue: list[QueueItem] = []
error_queue: list[QueueItem] = []
stage_status: dict[str, str] = {}
run_context: dict[str, Any] = {
    "vpc_name": "",
    "explicit_vpc_id": "",
    "discovered_vpc_id": "",
    "effective_vpc_id": "",
    "vpc_id_source": "unresolved",
    "effective_subnet_id": "",
    "subnet_id_source": "unresolved",
    "effective_storage_policy_id": "",
    "storage_policy_id_source": "unresolved",
    "storage_policy_requested": "",
    "selected_storage_policy_name": "",
    "selected_storage_policy_id": "",
    "selected_storage_policy_db_id": "",
    "selected_storage_policy_provider_field_used": "storage_policy_id",
    "selected_storage_policy_quota_status": "not_available",
    "discovered_storage_policies": [],
    "validated_instance_hostnames": {},
    "discovered_instance_images": {},
    "instance_image_sources": {},
    "discovered_instance_flavor": "",
    "discovered_instance_flavor_name": "",
    "instance_flavor_source": "unresolved",
    "instance_quota": {},
    "selected_instance_round": {},
    "run_status": "running",
    "run_blocked": False,
    "user_action_required": False,
    "remaining_images_not_attempted": [],
}
existing_subnet_inventory: list[dict[str, str]] = []
instance_validation: dict[str, Any] = {"valid": False, "vars": {}, "diagnostics": {}, "errors": []}
