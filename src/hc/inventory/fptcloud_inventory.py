"""Read-only FPT Cloud instance inventory reader.

Governed by specs/06-QUOTA-AWARE-ROLLING-STRATEGY.md §3 and FR-003:
  - All operations here are READ-ONLY (HTTP GET only).
  - No mutation APIs (POST/PUT/DELETE) are called.
  - Used by the quota-recovery path to find the oldest reclaimable HC instance.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# HC instance name patterns — must match specs/06-QUOTA-AWARE-ROLLING-STRATEGY.md §7.1
# hcvm-<os>-<random>  |  hcw<6random> (Windows ≤15)  |  hcl-<8random> (Linux)
HC_NAME_RE = re.compile(r"^hc(?:vm-[a-z0-9]|w[a-z0-9]{6,14}$|l-[a-z0-9]{8})", re.IGNORECASE)

# Tags that prove health-check ownership — spec §7.2 / FR-017
HC_TAG_MANAGED_KEY = "managed_by"
HC_TAG_MANAGED_VALUE = "health-check"
HC_TAG_BOOL_KEY = "health_check"
HC_TAG_BOOL_VALUE = "true"

# Instance statuses considered "reclaimable" by priority order (spec §7.2 A)
RECLAIM_PRIORITY = {"POWERED_OFF": 0, "STOPPED": 0, "POWERED_ON": 1, "ACTIVE": 1, "RUNNING": 1}


@dataclass
class TagEntry:
    id: str
    key: str
    value: str
    scope_type: str = ""
    color: str = ""


@dataclass
class InventoryInstance:
    """One instance record from the FPT Cloud API."""
    instance_id: str
    name: str
    status: str
    vpc_id: str
    created_at: str = ""
    os_label: str = ""  # populated from hc_os_label tag when available
    tags: list[TagEntry] = field(default_factory=list)

    def is_hc_name(self) -> bool:
        return bool(HC_NAME_RE.match(self.name))

    def is_hc_tagged(self) -> bool:
        """True if instance carries at least one proof-of-ownership tag."""
        for t in self.tags:
            if t.key == HC_TAG_MANAGED_KEY and t.value == HC_TAG_MANAGED_VALUE:
                return True
            if t.key == HC_TAG_BOOL_KEY and t.value.lower() == HC_TAG_BOOL_VALUE:
                return True
        return False

    def is_eligible_for_reclamation(self, current_run_id: str) -> bool:
        """Both name pattern AND tag required; never the current run's VM."""
        if not self.is_hc_name():
            return False
        if not self.is_hc_tagged():
            return False
        for t in self.tags:
            if t.key == "hc_run_id" and t.value == current_run_id:
                return False
        return True

    def age_key(self) -> tuple[str, str]:
        """Sort key: (hc_created_at or created_at, instance_id) — oldest first."""
        hc_ts = ""
        for t in self.tags:
            if t.key == "hc_created_at":
                hc_ts = t.value
                break
        ts = hc_ts or self.created_at or "9999"
        return (ts, self.instance_id)

    def reclaim_priority(self) -> int:
        """Lower value = higher reclaim priority (spec §7.2: successful → stopped → running)."""
        tag_keys = {t.key for t in self.tags}
        if "hc_validated" in tag_keys:
            return 0
        return RECLAIM_PRIORITY.get(self.status.upper(), 2)


def _http_get(url: str, token: str, timeout: int = 30) -> dict[str, Any] | list[Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} GET {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"URL error GET {url}: {exc.reason}") from exc


def list_instance_tags(
    instance_id: str,
    vpc_id: str,
    api_url: str,
    token: str,
    timeout: int = 30,
) -> list[TagEntry]:
    """GET /v2/vpc/{vpc_id}/instance/{id}/tags — read-only."""
    url = f"{api_url.rstrip('/')}/v2/vpc/{vpc_id}/instance/{instance_id}/tags"
    try:
        data = _http_get(url, token, timeout)
    except RuntimeError:
        return []
    records: list[Any] = data if isinstance(data, list) else (data.get("data") or data.get("tags") or [])
    result: list[TagEntry] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        result.append(
            TagEntry(
                id=str(item.get("id") or ""),
                key=str(item.get("key") or ""),
                value=str(item.get("value") or ""),
                scope_type=str(item.get("scope_type") or item.get("scopeType") or ""),
                color=str(item.get("color") or ""),
            )
        )
    return result


def list_vpc_instances(
    vpc_id: str,
    api_url: str,
    token: str,
    timeout: int = 30,
    *,
    fetch_tags_for_hc_names: bool = True,
) -> list[InventoryInstance]:
    """GET /v2/vpc/{vpc_id}/instance — read-only list of all instances in the VPC.

    For instances whose name matches HC_NAME_RE, tags are fetched individually
    to verify ownership proof (spec §7.2 eligibility). Non-HC-named instances
    are returned without tags (fast path).
    """
    url = f"{api_url.rstrip('/')}/v2/vpc/{vpc_id}/instance"
    try:
        data = _http_get(url, token, timeout)
    except RuntimeError:
        return []

    raw_list: list[Any] = (
        data if isinstance(data, list)
        else (data.get("data") or data.get("instances") or [])
    )

    results: list[InventoryInstance] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        instance_id = str(item.get("id") or item.get("instance_id") or "")
        name = str(item.get("name") or item.get("host_name") or "")
        status = str(item.get("status") or "")
        created_at = str(item.get("created_at") or item.get("createdAt") or "")
        if not instance_id or not name:
            continue

        inst = InventoryInstance(
            instance_id=instance_id,
            name=name,
            status=status,
            vpc_id=vpc_id,
            created_at=created_at,
        )

        if fetch_tags_for_hc_names and inst.is_hc_name():
            inst.tags = list_instance_tags(instance_id, vpc_id, api_url, token, timeout)
            for t in inst.tags:
                if t.key == "hc_os_label":
                    inst.os_label = t.value
                    break

        results.append(inst)

    return results


def select_oldest_reclaimable(
    instances: list[InventoryInstance],
    current_run_id: str,
) -> InventoryInstance | None:
    """Return the single best candidate for reclamation or None.

    Selection (spec §7.2):
      1. Filter: eligible (HC name + HC tag + not current-run VM)
      2. Sort: by (reclaim_priority ASC, age_key ASC) — oldest successful first
      3. Return the first; if empty, return None (fail-closed)
    """
    candidates = [i for i in instances if i.is_eligible_for_reclamation(current_run_id)]
    if not candidates:
        return None
    candidates.sort(key=lambda i: (i.reclaim_priority(), i.age_key()))
    return candidates[0]
