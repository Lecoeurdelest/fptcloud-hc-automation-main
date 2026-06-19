"""VPC, subnet, storage-policy, image, and flavor discovery.

Wraps the provider data sources (via Terraform) and the selection rules. The
Premium-SSD storage-policy selection is exact-name match and must stay that way.
``discover_data_collection`` is the single Terraform-backed collector that the
image/flavor/storage-policy discovery functions share (and that tests patch).
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

from diagnose_health_inputs import looks_uuid

from healthcheck import config, state
from healthcheck import terraform_executor as tf
from healthcheck.classification import classify_context, is_quota_error
from healthcheck.logging import emit, queue_error, safe_name
from healthcheck.models import CandidateState, SubnetCandidateSelection
from healthcheck.reporting import format_failure
from healthcheck.spec_loader import runnable_spec, spec_preflight


# ── VPC context ───────────────────────────────────────────────────────────────
def vpc_lookup_key() -> str:
    values = config.target_vpcs()
    return (
        config.env("HC_VPC_NAME")
        or config.env("VPC_NAME")
        or config.env("VPC_ID")
        or (values[0] if values else "")
    )


def update_vpc_context(*, discovered_vpc_id: str = "") -> dict[str, str]:
    explicit = config.env("HC_VPC_ID")
    if discovered_vpc_id:
        state.run_context["discovered_vpc_id"] = discovered_vpc_id
    discovered = state.run_context.get("discovered_vpc_id", "")
    effective = explicit or discovered
    source = "explicit" if explicit else ("discovered" if discovered else "unresolved")
    state.run_context.update(
        {
            "vpc_name": vpc_lookup_key(),
            "explicit_vpc_id": explicit,
            "effective_vpc_id": effective,
            "vpc_id_source": source,
        }
    )
    return state.run_context


def vpc_diagnostics_message(prefix: str = "VPC resolution") -> str:
    ctx = update_vpc_context()
    return (
        f"{prefix}: "
        f"vpc_name={ctx.get('vpc_name') or '<unset>'}; "
        f"explicit_vpc_id={ctx.get('explicit_vpc_id') or '<unset>'}; "
        f"discovered_vpc_id={ctx.get('discovered_vpc_id') or '<unset>'}; "
        f"effective_vpc_id={ctx.get('effective_vpc_id') or '<unset>'}; "
        f"vpc_id_source={ctx.get('vpc_id_source') or 'unresolved'}"
    )


def target_vpc_entries() -> list[tuple[str, str]]:
    """Return ordered list of (vpc_name, raw_entry) target VPC pairs.

    The raw_entry is the VPC name/identifier used for provider discovery. Env
    VPC_IDS/VPC_ID override healthcheck.toml [targets].vpcs for compatibility.
    Returns at least the primary VPC from vpc_lookup_key() so the single-VPC
    path is preserved when only one entry is configured.
    """
    entries = config.target_vpcs()
    if not entries:
        primary = vpc_lookup_key()
        return [(primary, primary)] if primary else []
    return [(e, e) for e in entries]


def vpc_identifier_type(value: str) -> str:
    if not value:
        return "unset"
    if looks_uuid(value):
        return "uuid-shaped"
    return "display-name-or-non-uuid"


# ── Subnet candidate selection ────────────────────────────────────────────────
def existing_subnet_cidrs() -> list[str]:
    return config.existing_subnet_cidrs()


def cidr_overlap(candidate: str, existing: list[str]) -> tuple[str, str]:
    try:
        candidate_network = ipaddress.ip_network(candidate, strict=True)
    except ValueError as exc:
        return "", f"candidate subnet CIDR is invalid: {exc}"
    for raw in existing:
        try:
            existing_network = ipaddress.ip_network(raw, strict=True)
        except ValueError as exc:
            return raw, f"configured existing_subnet_cidrs contains invalid CIDR {raw}: {exc}"
        if candidate_network.overlaps(existing_network):
            return raw, ""
    return "", ""


def next_subnet_candidate(cidr: str) -> str:
    network = ipaddress.ip_network(cidr, strict=True)
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError("only IPv4 subnet candidate generation is supported")
    step = network.num_addresses * 10
    next_address = int(network.network_address) + step
    if next_address > int(ipaddress.IPv4Address("255.255.255.255")):
        raise ValueError("candidate subnet generation exceeded IPv4 address space")
    return str(
        ipaddress.ip_network(
            f"{ipaddress.IPv4Address(next_address)}/{network.prefixlen}", strict=True
        )
    )


def gateway_for_candidate(start_cidr: str, start_gateway: str, selected_cidr: str) -> str:
    start_network = ipaddress.ip_network(start_cidr, strict=True)
    selected_network = ipaddress.ip_network(selected_cidr, strict=True)
    gateway_ip = ipaddress.ip_address(start_gateway)
    offset = int(gateway_ip) - int(start_network.network_address)
    if offset <= 0 or offset >= start_network.num_addresses - 1:
        return str(next(selected_network.hosts()))
    return str(ipaddress.ip_address(int(selected_network.network_address) + offset))


def select_additional_subnet_candidate(
    start_cidr: str,
    start_gateway: str,
    existing: list[str],
    max_attempts: int,
    prior_rejected: list[str] | None = None,
    prior_sources: list[str] | None = None,
    prior_conflicting_subnets: list[str] | None = None,
) -> SubnetCandidateSelection:
    rejected: list[str] = list(prior_rejected or [])
    sources: list[str] = list(prior_sources or [])
    conflicting_subnets: list[str] = list(prior_conflicting_subnets or [])
    candidate = start_cidr
    overlap_reason = ""
    for attempt in range(1, max_attempts + 1):
        if candidate in rejected:
            candidate_source = (
                sources[rejected.index(candidate)]
                if candidate in rejected and rejected.index(candidate) < len(sources)
                else "runtime_conflict"
            )
            candidate_conflict = (
                conflicting_subnets[rejected.index(candidate)]
                if candidate in rejected and rejected.index(candidate) < len(conflicting_subnets)
                else ""
            )
            overlap_reason = f"{candidate} already rejected from {candidate_source}"
            if candidate_conflict:
                overlap_reason += f" by {candidate_conflict}"
            if attempt < max_attempts:
                try:
                    candidate = next_subnet_candidate(candidate)
                    continue
                except ValueError as exc:
                    return SubnetCandidateSelection(
                        "", "", attempt, rejected, overlap_reason, exhausted=True, error=str(exc)
                    )
            return SubnetCandidateSelection(
                "", "", attempt, rejected, overlap_reason, exhausted=True
            )
        overlap, error = cidr_overlap(candidate, existing)
        if error:
            return SubnetCandidateSelection("", "", attempt, rejected, overlap_reason, error=error)
        if not overlap:
            gateway = gateway_for_candidate(start_cidr, start_gateway, candidate)
            return SubnetCandidateSelection(candidate, gateway, attempt, rejected, overlap_reason)
        rejected.append(candidate)
        sources.append("preflight_inventory")
        conflicting_subnets.append("")
        overlap_reason = f"{candidate} overlaps existing subnet CIDR {overlap}"
        if attempt < max_attempts:
            try:
                candidate = next_subnet_candidate(candidate)
            except ValueError as exc:
                return SubnetCandidateSelection(
                    "", "", attempt, rejected, overlap_reason, exhausted=True, error=str(exc)
                )
    return SubnetCandidateSelection("", "", max_attempts, rejected, overlap_reason, exhausted=True)


def discover_existing_subnets(stage) -> None:
    if not stage:
        return
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["subnet-inventory"])
        return
    state.existing_subnet_inventory.clear()
    for cidr in existing_subnet_cidrs():
        state.existing_subnet_inventory.append(
            {
                "name": "operator-provided",
                "id": "",
                "cidr": cidr,
                "gateway": "",
                "vpc_id": state.run_context.get("effective_vpc_id", ""),
            }
        )
    if state.existing_subnet_inventory:
        emit(
            stage.id,
            "done",
            (
                "Loaded existing subnet inventory from HC_EXISTING_SUBNET_CIDRS or healthcheck.toml network.additional-subnet existing_subnet_cidrs; "
                f"cidrs={', '.join(item['cidr'] for item in state.existing_subnet_inventory)}; "
                "provider_listing=not_available_in_runner"
            ),
            ["HC_EXISTING_SUBNET_CIDRS", "healthcheck.toml:network.additional-subnet.existing_subnet_cidrs"],
        )
    else:
        emit(
            stage.id,
            "done",
            "No existing subnet inventory configured; provider/API listing is not available in this runner, so overlap can only be classified from provider errors.",
            ["subnet-inventory"],
        )


def select_additional_subnet_vars(
    vars: dict[str, Any], state_arg: CandidateState | None = None
) -> tuple[dict[str, Any], str, CandidateState]:
    cidr = state_arg.start_cidr if state_arg else str(vars.get("cidr") or "")
    gateway = state_arg.start_gateway if state_arg else str(vars.get("gateway_ip") or "")
    max_attempts = state_arg.max_attempts if state_arg else config.max_subnet_candidate_attempts()
    existing = [item["cidr"] for item in state.existing_subnet_inventory if item.get("cidr")]
    selected = dict(vars)
    candidate_state = state_arg or CandidateState(cidr, gateway, max_attempts)
    if not existing and not candidate_state.rejected_cidrs:
        emit(
            "network.select-additional-subnet-cidr",
            "done",
            f"total_attempts=1; rejected_cidrs=[]; selected_cidr={cidr or '<unset>'}; overlap_reason=<none>; inventory=unavailable",
            ["subnet-candidate-selection"],
        )
        return selected, "", candidate_state
    selection = select_additional_subnet_candidate(
        cidr,
        gateway,
        existing,
        max_attempts,
        list(candidate_state.rejected_cidrs),
        list(candidate_state.conflict_sources),
        list(candidate_state.conflicting_subnets),
    )
    rejected = ", ".join(selection.rejected_cidrs)
    if selection.error and not selection.exhausted:
        return (
            selected,
            f"Classification: configuration_invalid; {selection.error}; attempted_cidr={cidr or '<unset>'}; attempted_gateway={gateway or '<unset>'}",
            candidate_state,
        )
    prior_source_count = len(candidate_state.conflict_sources)
    added_rejections = max(0, len(selection.rejected_cidrs) - prior_source_count)
    updated_state = CandidateState(
        cidr,
        gateway,
        max_attempts,
        tuple(selection.rejected_cidrs),
        tuple(candidate_state.conflict_sources + (("preflight_inventory",) * added_rejections)),
        tuple(candidate_state.conflicting_subnets + (("",) * added_rejections)),
    )
    conflict_sources = ", ".join(updated_state.conflict_sources)
    conflicting_subnets = ", ".join(value for value in updated_state.conflicting_subnets if value)
    if selection.exhausted:
        emit(
            "network.select-additional-subnet-cidr",
            "skipped",
            (
                f"total_attempts={selection.candidate_attempt_count}; rejected_cidrs=[{rejected}]; "
                f"conflict_sources=[{conflict_sources}]; "
                "selected_cidr=<none>; "
                f"overlap_reason={selection.overlap_reason or selection.error or '<none>'}"
            ),
            ["subnet-candidate-selection"],
        )
        return (
            selected,
            (
                "Classification: subnet_cidr_exhausted; "
                f"candidate_attempt_count={selection.candidate_attempt_count}; "
                f"rejected_cidrs=[{rejected}]; "
                f"conflict_sources=[{conflict_sources}]; "
                f"overlap_reason={selection.overlap_reason or selection.error or '<none>'}; "
                "skipping before Terraform apply"
            ),
            updated_state,
        )
    selected["cidr"] = selection.selected_cidr
    selected["gateway_ip"] = selection.selected_gateway
    emit(
        "network.select-additional-subnet-cidr",
        "done",
        (
            f"total_attempts={selection.candidate_attempt_count}; rejected_cidrs=[{rejected}]; "
            f"conflict_sources=[{conflict_sources}]; "
            f"conflicting_subnets=[{conflicting_subnets}]; "
            f"selected_cidr={selection.selected_cidr}; selected_gateway={selection.selected_gateway}; "
            f"overlap_reason={selection.overlap_reason or '<none>'}"
        ),
        ["subnet-candidate-selection"],
    )
    return selected, "", updated_state


def append_provider_overlap(
    state_arg: CandidateState, cidr: str, conflicting_subnet: str
) -> CandidateState:
    if cidr in state_arg.rejected_cidrs:
        return state_arg
    return CandidateState(
        state_arg.start_cidr,
        state_arg.start_gateway,
        state_arg.max_attempts,
        state_arg.rejected_cidrs + (cidr,),
        state_arg.conflict_sources + ("provider_error",),
        state_arg.conflicting_subnets + (conflicting_subnet,),
    )


# ── Terraform-backed discovery ────────────────────────────────────────────────
def discover_vpc(stage) -> str:
    update_vpc_context()
    if not stage:
        return state.run_context["effective_vpc_id"]
    ok, reason = runnable_spec(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["data.fptcloud_vpc.this"])
        return state.run_context["effective_vpc_id"]
    missing = [name for name in stage.required_inputs if not config.env(name)]
    if missing:
        emit(
            stage.id,
            "skipped",
            f"Missing required spec inputs: {', '.join(missing)}; {vpc_diagnostics_message()}",
            ["data.fptcloud_vpc.this"],
        )
        return state.run_context["effective_vpc_id"]

    explicit = state.run_context["explicit_vpc_id"]
    lookup = state.run_context["vpc_name"]
    if not lookup:
        if explicit:
            emit(
                stage.id,
                "done",
                f"Using explicit HC_VPC_ID; no VPC lookup key configured; {vpc_diagnostics_message()}",
                ["HC_VPC_ID"],
            )
        else:
            emit(
                stage.id,
                "skipped",
                f"No VPC ID can be resolved because no VPC lookup key is configured; {vpc_diagnostics_message()}",
                ["data.fptcloud_vpc.this"],
            )
        return state.run_context["effective_vpc_id"]

    workspace = state.RUN_ROOT / safe_name(stage.id)
    workspace.mkdir(parents=True, exist_ok=True)
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

data "fptcloud_vpc" "this" {{
  name = "{lookup}"
}}

output "value" {{
  value = data.fptcloud_vpc.this.id
}}
""".lstrip(),
        encoding="utf-8",
    )
    init = tf.run(
        ["terraform", "init", "-input=false", "-no-color"],
        workspace,
        timeout=300,
        stage=f"{stage.id}-init",
    )
    if init.returncode != 0:
        reason = (init.stderr or init.stdout)[-1200:]
        context = classify_context(
            stage=stage.id,
            resource_type="fptcloud_vpc",
            address="data.fptcloud_vpc.this",
            module_path=workspace,
            reason=reason,
        )
        queue_error(stage.id, workspace, ["data.fptcloud_vpc.this"], format_failure(context))
        if explicit:
            state.stage_status[stage.id] = "done"
        return state.run_context["effective_vpc_id"]
    apply = tf.run(
        ["terraform", "apply", "-auto-approve", "-no-color", "-input=false"],
        workspace,
        timeout=300,
        stage=f"{stage.id}-apply",
    )
    if apply.returncode != 0:
        reason = (apply.stderr or apply.stdout)[-1200:]
        context = classify_context(
            stage=stage.id,
            resource_type="fptcloud_vpc",
            address="data.fptcloud_vpc.this",
            module_path=workspace,
            reason=reason,
        )
        queue_error(stage.id, workspace, ["data.fptcloud_vpc.this"], format_failure(context))
        if explicit:
            state.stage_status[stage.id] = "done"
        return state.run_context["effective_vpc_id"]
    output = tf.run(
        ["terraform", "output", "-raw", "value"], workspace, timeout=120, stage=f"{stage.id}-output"
    )
    discovered = output.stdout.strip() if output.returncode == 0 else ""
    update_vpc_context(discovered_vpc_id=discovered)
    if explicit and discovered and explicit != discovered:
        emit(
            stage.id,
            "done",
            f"WARNING: explicit HC_VPC_ID differs from discovered VPC ID; using explicit value. {vpc_diagnostics_message()}",
            ["HC_VPC_ID", "data.fptcloud_vpc.this"],
        )
    elif state.run_context["effective_vpc_id"]:
        emit(
            stage.id,
            "done",
            f"VPC ID resolution succeeded. {vpc_diagnostics_message()}",
            ["data.fptcloud_vpc.this"],
        )
    else:
        emit(
            stage.id,
            "skipped",
            f"VPC ID resolution did not return an ID. {vpc_diagnostics_message()}",
            ["data.fptcloud_vpc.this"],
        )
    return state.run_context["effective_vpc_id"]


def discover_value(
    name: str, source: str, expression: str, vpc_id: str, stage_id: str | None = None
) -> str:
    stage_name = stage_id or f"discover-{name}"
    workspace = state.RUN_ROOT / safe_name(stage_name)
    workspace.mkdir(parents=True, exist_ok=True)
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

data "{source}" "this" {{
  vpc_id = "{vpc_id}"
}}

output "value" {{
  value = {expression}
}}
""".lstrip(),
        encoding="utf-8",
    )
    init = tf.run(
        ["terraform", "init", "-input=false", "-no-color"],
        workspace,
        timeout=300,
        stage=f"{stage_name}-init",
    )
    if init.returncode != 0:
        reason = (init.stderr or init.stdout)[-1000:]
        context = classify_context(
            stage=stage_name,
            resource_type=source,
            address=f"data.{source}.this",
            module_path=workspace,
            reason=reason,
        )
        queue_error(stage_name, workspace, [source], format_failure(context))
        return ""
    apply = tf.run(
        ["terraform", "apply", "-auto-approve", "-no-color", "-input=false"],
        workspace,
        timeout=300,
        stage=f"{stage_name}-apply",
    )
    if apply.returncode != 0:
        reason = (apply.stderr or apply.stdout)[-1000:]
        context = classify_context(
            stage=stage_name,
            resource_type=source,
            address=f"data.{source}.this",
            module_path=workspace,
            reason=reason,
        )
        queue_error(stage_name, workspace, [source], format_failure(context))
        return ""
    output = tf.run(
        ["terraform", "output", "-raw", "value"],
        workspace,
        timeout=120,
        stage=f"{stage_name}-output",
    )
    value = output.stdout.strip() if output.returncode == 0 else ""
    status = "done" if value else "skipped"
    emit(stage_name, status, f"Discovered {name}: {value or 'not found'}", [source])
    return value


def discover_filtered_value(
    *,
    name: str,
    source: str,
    collection: str,
    output_attr: str,
    filter_key: str,
    filter_value: str,
    vpc_id: str,
    stage_id: str,
) -> str:
    workspace = state.RUN_ROOT / safe_name(stage_id)
    workspace.mkdir(parents=True, exist_ok=True)
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

data "{source}" "this" {{
  vpc_id = "{vpc_id}"
  filter {{
    key = "{filter_key}"
    values = ["{filter_value}"]
  }}
}}

output "value" {{
  value = try(data.{source}.this.{collection}[0].{output_attr}, "")
}}
""".lstrip(),
        encoding="utf-8",
    )
    init = tf.run(
        ["terraform", "init", "-input=false", "-no-color"],
        workspace,
        timeout=300,
        stage=f"{stage_id}-init",
    )
    if init.returncode != 0:
        reason = (init.stderr or init.stdout)[-1000:]
        context = classify_context(
            stage=stage_id,
            resource_type=source,
            address=f"data.{source}.this",
            module_path=workspace,
            reason=reason,
        )
        queue_error(stage_id, workspace, [source], format_failure(context))
        return ""
    apply = tf.run(
        ["terraform", "apply", "-auto-approve", "-no-color", "-input=false"],
        workspace,
        timeout=300,
        stage=f"{stage_id}-apply",
    )
    if apply.returncode != 0:
        reason = (apply.stderr or apply.stdout)[-1000:]
        context = classify_context(
            stage=stage_id,
            resource_type=source,
            address=f"data.{source}.this",
            module_path=workspace,
            reason=reason,
        )
        queue_error(stage_id, workspace, [source], format_failure(context))
        return ""
    output = tf.run(
        ["terraform", "output", "-raw", "value"], workspace, timeout=120, stage=f"{stage_id}-output"
    )
    value = output.stdout.strip() if output.returncode == 0 else ""
    emit(
        stage_id,
        "done" if value else "failed",
        f"Resolved {name} by {filter_key}: {value or 'not found'}",
        [source],
    )
    return value


def discover_data_collection(
    stage_id: str, source: str, collection: str, vpc_id: str
) -> tuple[list[dict[str, Any]], str]:
    workspace = state.RUN_ROOT / safe_name(stage_id)
    workspace.mkdir(parents=True, exist_ok=True)
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

data "{source}" "this" {{
  vpc_id = "{vpc_id}"
}}

output "value" {{
  value = jsonencode(data.{source}.this.{collection})
}}
""".lstrip(),
        encoding="utf-8",
    )
    init = tf.run(
        ["terraform", "init", "-input=false", "-no-color"],
        workspace,
        timeout=300,
        stage=f"{stage_id}-init",
    )
    if init.returncode != 0:
        return [], (init.stderr or init.stdout)[-1000:]
    plan = tf.run(
        ["terraform", "plan", "-out=tfplan", "-no-color", "-input=false"],
        workspace,
        timeout=300,
        stage=f"{stage_id}-plan",
    )
    if plan.returncode != 0:
        return [], (plan.stderr or plan.stdout)[-1000:]
    output = tf.run(
        ["terraform", "show", "-json", "-no-color", "tfplan"],
        workspace,
        timeout=120,
        stage=f"{stage_id}-show-plan",
    )
    if output.returncode != 0:
        return [], (output.stderr or output.stdout)[-1000:]
    try:
        plan_json = json.loads(output.stdout or "{}")
        raw_value = (
            plan_json.get("planned_values", {})
            .get("outputs", {})
            .get("value", {})
            .get("value", "[]")
        )
        values = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except json.JSONDecodeError as exc:
        return [], f"Could not parse {source} output: {exc}"
    return [item for item in values if isinstance(item, dict)], ""


# ── Image discovery ───────────────────────────────────────────────────────────
def normalized_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def image_matches(label: str, image: dict[str, Any]) -> bool:
    haystack = normalized_text(f"{image.get('name', '')} {image.get('catalog', '')}")
    if image.get("is_gpu") is True:
        return False
    requirements = {
        "windows-2012": ("windows", "2012"),
        "windows-2016": ("windows", "2016"),
        "windows-2019": ("windows", "2019"),
        "windows-2022": ("windows", "2022"),
        "ubuntu-16-04": ("ubuntu", "1604"),
        "ubuntu-18-04": ("ubuntu", "1804"),
        "ubuntu-20-04": ("ubuntu", "2004"),
        "ubuntu-22-04": ("ubuntu", "2204"),
    }
    tokens = requirements[label]
    if tokens[0] not in haystack:
        return False
    version = tokens[1]
    return version in haystack or version[:2] + "04" in haystack


def image_patterns(label: str) -> list[str]:
    patterns = {
        "windows-2012": ["windows.*2012", "windows server 2012"],
        "windows-2016": ["windows.*2016", "windows server 2016"],
        "windows-2019": ["windows.*2019", "windows server 2019"],
        "windows-2022": ["windows.*2022", "windows server 2022"],
        "ubuntu-16-04": [
            "Ubuntu-16-04",
            "ubuntu-16.04",
            "Ubuntu 16.04",
            "Ubuntu Server 16.04",
            "ubuntu.*1604",
        ],
        "ubuntu-18-04": [
            "Ubuntu-18-04",
            "ubuntu-18.04",
            "Ubuntu 18.04",
            "Ubuntu Server 18.04",
            "ubuntu.*1804",
        ],
        "ubuntu-20-04": [
            "Ubuntu-20-04",
            "ubuntu-20.04",
            "Ubuntu 20.04",
            "Ubuntu Server 20.04",
            "ubuntu.*2004",
        ],
        "ubuntu-22-04": [
            "Ubuntu-22-04",
            "ubuntu-22.04",
            "Ubuntu 22.04",
            "Ubuntu Server 22.04",
            "ubuntu.*2204",
        ],
    }
    return patterns[label]


def ubuntu_candidate_names(images: list[dict[str, Any]]) -> list[str]:
    names = []
    for image in images:
        text = f"{image.get('name', '')} {image.get('catalog', '')}"
        if "ubuntu" in text.lower() and image.get("name"):
            names.append(str(image.get("name")))
    return sorted(set(names))


def select_image_candidate(label: str, images: list[dict[str, Any]]) -> tuple[str, list[str]]:
    candidates = [image for image in images if image_matches(label, image)]
    names = sorted(str(image.get("name") or "") for image in candidates if image.get("name"))
    return (names[-1] if names else "", names)


def discover_instance_images(stage) -> None:
    if not stage:
        return
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(
            stage.id,
            "skipped",
            f"provider_capability=supported; {reason}",
            ["data.fptcloud_image.this"],
        )
        return
    vpc_id = str(state.run_context.get("effective_vpc_id") or "")
    resolved: dict[str, str] = {}
    sources: dict[str, str] = {}
    details: list[str] = ["provider_capability=supported"]
    unresolved: list[str] = []
    unavailable: list[str] = []
    for label, var_name in state.INSTANCE_IMAGE_MATRIX:
        if config.env(var_name):
            resolved[label] = config.env(var_name)
            sources[label] = "explicit_env"
    provider_images: list[dict[str, Any]] = []
    if len(resolved) < len(state.INSTANCE_IMAGE_MATRIX):
        provider_images, error = discover_data_collection(
            stage.id, "fptcloud_image", "images", vpc_id
        )
        if error:
            context = classify_context(
                stage=stage.id,
                resource_type="fptcloud_image",
                address="data.fptcloud_image.this",
                module_path=state.RUN_ROOT / safe_name(stage.id),
                reason=error,
            )
            queue_error(
                stage.id,
                state.RUN_ROOT / safe_name(stage.id),
                ["data.fptcloud_image.this"],
                format_failure(context),
            )
            return
    ubuntu_candidates = ubuntu_candidate_names(provider_images)
    for label, _var_name in state.INSTANCE_IMAGE_MATRIX:
        candidates: list[str] = []
        if label not in resolved:
            selected, candidates = select_image_candidate(label, provider_images)
            if selected:
                resolved[label] = selected
                sources[label] = "provider_datasource"
            else:
                sources[label] = "unresolved"
                unresolved.append(label)
                if label in {"ubuntu-16-04", "ubuntu-18-04"}:
                    unavailable.append(label)
        status = (
            "resolved"
            if resolved.get(label)
            else ("image_unavailable_in_region" if label in unavailable else "unresolved")
        )
        details.append(
            f"{label}:status={status}; source={sources[label]}; image={resolved.get(label) or '<unresolved>'}; "
            f"candidate_count={len(candidates)}; patterns_tried={','.join(image_patterns(label))}"
        )
    state.run_context["discovered_instance_images"] = resolved
    state.run_context["instance_image_sources"] = sources
    state.run_context["unavailable_instance_images"] = unavailable
    resolved_count = len(
        [label for label, _var_name in state.INSTANCE_IMAGE_MATRIX if resolved.get(label)]
    )
    unresolved_count = len(state.INSTANCE_IMAGE_MATRIX) - resolved_count
    require_all = config.env_bool_default("HC_REQUIRE_ALL_INSTANCE_IMAGES", False)
    summary = (
        f"resolution_status={'resolved' if unresolved_count == 0 else 'partial'}; "
        f"resolved_count={resolved_count}; unresolved_count={unresolved_count}; require_all_images={require_all}; "
        f"ubuntu_candidate_images={json.dumps(ubuntu_candidates)}"
    )
    if unresolved:
        status = "skipped" if require_all else "done"
        message = (
            f"Classification: instance_image_unresolved; {summary}; {' | '.join(details)}; "
            f"unresolved={', '.join(unresolved)}; unavailable={', '.join(unavailable) or '<none>'}"
        )
        emit(stage.id, status, message, ["data.fptcloud_image.this"])
        if not require_all:
            state.stage_status[stage.id] = "done"
        return
    state.stage_status[stage.id] = "done"
    emit(stage.id, "done", f"{summary}; {' | '.join(details)}", ["data.fptcloud_image.this"])


# ── Flavor discovery ──────────────────────────────────────────────────────────
def flavor_matches(flavor: dict[str, Any]) -> bool:
    cpu = flavor.get("cpu")
    memory = flavor.get("memory_mb")
    flavor_type = str(flavor.get("type") or "").upper()
    gpu = flavor.get("gpu_memory_gb")
    return (
        cpu == 2
        and memory == 2048
        and (not flavor_type or flavor_type == "VM_SIZE")
        and gpu in (None, 0)
    )


def select_flavor_candidate(
    flavors: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    candidates = [flavor for flavor in flavors if flavor_matches(flavor)]
    names = sorted(str(flavor.get("name") or "") for flavor in candidates if flavor.get("name"))
    if not names:
        return None, []
    selected_name = names[0]
    for flavor in candidates:
        if flavor.get("name") == selected_name:
            return flavor, names
    return None, names


def discover_instance_flavor(stage) -> None:
    if not stage:
        return
    ok, reason = spec_preflight(stage)
    if not ok:
        emit(
            stage.id,
            "skipped",
            f"provider_capability=supported; {reason}",
            ["data.fptcloud_flavor.this"],
        )
        return
    vpc_id = str(state.run_context.get("effective_vpc_id") or "")
    source = "unresolved"
    selected_name = ""
    selected_id = ""
    candidate_count = 0
    if config.env("HC_FLAVOR_ID"):
        selected_name = discover_filtered_value(
            name="flavor",
            source="fptcloud_flavor",
            collection="flavors",
            output_attr="name",
            filter_key="id",
            filter_value=config.env("HC_FLAVOR_ID"),
            vpc_id=vpc_id,
            stage_id="compute.resolve-instance-flavor",
        )
        selected_id = config.env("HC_FLAVOR_ID")
        source = "explicit_env" if selected_name else "unresolved"
    elif config.env("HC_FLAVOR_NAME"):
        selected_name = config.env("HC_FLAVOR_NAME")
        source = "explicit_env"
    else:
        flavors, error = discover_data_collection(stage.id, "fptcloud_flavor", "flavors", vpc_id)
        if error:
            context = classify_context(
                stage=stage.id,
                resource_type="fptcloud_flavor",
                address="data.fptcloud_flavor.this",
                module_path=state.RUN_ROOT / safe_name(stage.id),
                reason=error,
            )
            queue_error(
                stage.id,
                state.RUN_ROOT / safe_name(stage.id),
                ["data.fptcloud_flavor.this"],
                format_failure(context),
            )
            return
        selected, candidates = select_flavor_candidate(flavors)
        candidate_count = len(candidates)
        if selected:
            selected_name = str(selected.get("name") or "")
            selected_id = str(selected.get("id") or "")
            source = "provider_datasource"
    state.run_context["discovered_instance_flavor"] = selected_id or selected_name
    state.run_context["discovered_instance_flavor_name"] = selected_name
    state.run_context["instance_flavor_source"] = source
    message = (
        f"provider_capability=supported; flavor_status={'resolved' if selected_name else 'unresolved'}; "
        f"source={source}; flavor_name={selected_name or '<unresolved>'}; flavor_id={selected_id or '<unresolved>'}; "
        f"target_cpu=2; target_memory_mb=2048; candidate_count={candidate_count}"
    )
    if not selected_name:
        emit(
            stage.id,
            "skipped",
            f"Classification: instance_flavor_unresolved; {message}",
            ["data.fptcloud_flavor.this"],
        )
        return
    state.stage_status[stage.id] = "done"
    emit(stage.id, "done", message, ["data.fptcloud_flavor.this"])


# ── Storage-policy discovery & selection (Premium-SSD exact match) ─────────────
def collected_storage_policies() -> list[dict[str, str]]:
    policies = config.spec_constants().get("COLLECTED_INSTANCE_STORAGE_POLICIES", [])
    return [dict(policy) for policy in policies if isinstance(policy, dict)]


def preferred_instance_storage_policy_name() -> str:
    selection = config.spec_constants().get("INSTANCE_STORAGE_POLICY_SELECTION", {})
    configured = (
        str(selection.get("preferred_name") or "Premium-SSD")
        if isinstance(selection, dict)
        else "Premium-SSD"
    )
    return config.env("HC_INSTANCE_STORAGE_POLICY_NAME") or configured


def storage_policy_name_matches(policy: dict[str, Any], requested_name: str) -> bool:
    return str(policy.get("name") or "") == requested_name


def discovered_storage_policies() -> list[dict[str, str]]:
    policies = state.run_context.get("discovered_storage_policies") or []
    return [dict(policy) for policy in policies if isinstance(policy, dict)]


def available_storage_policies() -> list[dict[str, str]]:
    collected = collected_storage_policies()
    provider_policies = discovered_storage_policies()
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, str]] = []
    for policy in [*provider_policies, *collected]:
        key = (
            str(policy.get("name") or "").lower(),
            str(policy.get("id") or "").lower(),
            str(policy.get("id_db") or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(policy)
    return merged


def storage_policy_by(field: str, value: str) -> dict[str, str]:
    if not value:
        return {}
    for policy in available_storage_policies():
        if str(policy.get(field) or "").lower() == value.lower():
            return policy
    return {}


def storage_policy_by_id(value: str) -> dict[str, str]:
    return storage_policy_by("id", value) or storage_policy_by("id_db", value)


def provider_uses_storage_policy_db_id() -> bool:
    return False


def normalized_storage_policy(policy: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(policy.get("name") or ""),
        "id": str(policy.get("id") or ""),
        "id_db": str(policy.get("id_db") or policy.get("idDB") or policy.get("db_id") or ""),
    }


def storage_policy_candidate_names(policies: list[dict[str, str]]) -> list[str]:
    return [str(policy.get("name") or "") for policy in policies if policy.get("name")]


def discover_storage_policy_stage(stage, explicit_storage_policy_id: str, vpc_id: str) -> str:
    if not stage:
        return explicit_storage_policy_id
    requested_name = preferred_instance_storage_policy_name()
    if explicit_storage_policy_id:
        ok, reason = runnable_spec(stage)
        emit(
            stage.id,
            "done" if ok else "skipped",
            (
                f"Using explicit HC_STORAGE_POLICY_ID for non-instance storage discovery; "
                f"storage_policy_requested={requested_name}; "
                f"instance_storage_policy_selection=preferred_exact_name; "
                f"provider_field_used=storage_policy_id"
            )
            if ok
            else reason,
            ["HC_STORAGE_POLICY_ID"],
        )
        return explicit_storage_policy_id

    ok, reason = spec_preflight(stage)
    if not ok:
        emit(stage.id, "skipped", reason, ["data.fptcloud_storage_policy.this"])
        return ""
    if not vpc_id:
        emit(
            stage.id,
            "skipped",
            f"No VPC ID can be resolved for provider discovery; {vpc_diagnostics_message()}",
            ["data.fptcloud_storage_policy.this"],
        )
        return ""

    emit(
        stage.id,
        "started",
        f"Using effective_vpc_id={vpc_id} for storage policy discovery",
        ["data.fptcloud_storage_policy.this"],
    )
    raw_policies, error = discover_data_collection(
        stage.id, "fptcloud_storage_policy", "storage_policies", vpc_id
    )
    if error:
        context = classify_context(
            stage=stage.id,
            resource_type="fptcloud_storage_policy",
            address="data.fptcloud_storage_policy.this",
            module_path=state.RUN_ROOT / safe_name(stage.id),
            reason=error,
        )
        queue_error(
            stage.id,
            state.RUN_ROOT / safe_name(stage.id),
            ["data.fptcloud_storage_policy.this"],
            format_failure(context),
        )
        return ""

    policies = [normalized_storage_policy(policy) for policy in raw_policies]
    policies = [
        policy
        for policy in policies
        if policy.get("name") or policy.get("id") or policy.get("id_db")
    ]
    state.run_context["discovered_storage_policies"] = policies
    selected = next(
        (
            policy
            for policy in available_storage_policies()
            if storage_policy_name_matches(policy, requested_name)
        ),
        {},
    )
    source = "provider_exact_name" if selected else "provider_inventory"
    if selected:
        state.run_context["effective_storage_policy_id"] = selected.get("id", "")
        state.run_context["storage_policy_id_source"] = source
    state.stage_status[stage.id] = "done"
    emit(
        stage.id,
        "done",
        (
            f"storage_policy_requested={requested_name}; "
            f"exact_match_found={bool(selected)}; "
            f"selected_storage_policy_name={selected.get('name') or '<unresolved>'}; "
            f"selected_storage_policy_id={selected.get('id') or '<unresolved>'}; "
            f"selected_storage_policy_db_id={selected.get('id_db') or '<unresolved>'}; "
            f"provider_field_used=storage_policy_id; "
            f"storage_policy_source={source}; "
            f"candidate_names={json.dumps(storage_policy_candidate_names(policies), sort_keys=True)}"
        ),
        ["data.fptcloud_storage_policy.this"],
    )
    return selected.get("id", "")


def storage_policy_fallback_spec() -> dict[str, Any]:
    raw = config.spec_constants().get("INSTANCE_STORAGE_POLICY_FALLBACK", {})
    return dict(raw) if isinstance(raw, dict) else {}


def fallback_storage_policy_allowed(
    classification: str, current_name: str, fallback_attempts: int
) -> bool:
    if is_quota_error(classification):
        return False
    spec = storage_policy_fallback_spec()
    try:
        max_attempts = int(spec.get("max_fallback_attempts", 0))
    except (TypeError, ValueError):
        max_attempts = 0
    return (
        bool(spec.get("enabled"))
        and classification == spec.get("on_classification")
        and current_name == spec.get("from")
        and fallback_attempts < max_attempts
        and bool(storage_policy_by("name", str(spec.get("to") or "")))
    )


def select_instance_storage_policy(discovered_policy_id: str = "") -> dict[str, str]:
    provider_field_used = "storage_policy_id"
    requested_name = preferred_instance_storage_policy_name()
    policy = next(
        (
            candidate
            for candidate in available_storage_policies()
            if storage_policy_name_matches(candidate, requested_name)
        ),
        {},
    )
    selected_name = str(policy.get("name") or "")
    selected_id = str(policy.get("id") or "")
    selected_db_id = str(policy.get("id_db") or "")
    provider_value = selected_id
    source = "preferred_exact_name" if provider_value else "unresolved"
    return {
        "requested_name": requested_name,
        "name": selected_name,
        "id": selected_id,
        "id_db": selected_db_id,
        "provider_value": provider_value,
        "source": source if provider_value else "unresolved",
        "provider_field_used": provider_field_used,
        "classification": "" if provider_value else "storage_policy_preferred_not_found",
    }


def apply_selected_storage_policy(
    selection: dict[str, str], *, quota_status: str = "not_available"
) -> None:
    state.run_context["storage_policy_requested"] = selection.get("requested_name", "")
    state.run_context["selected_storage_policy_name"] = selection.get("name", "")
    state.run_context["selected_storage_policy_id"] = selection.get("id", "")
    state.run_context["selected_storage_policy_db_id"] = selection.get("id_db", "")
    state.run_context["selected_storage_policy_provider_field_used"] = selection.get(
        "provider_field_used", "storage_policy_id"
    )
    state.run_context["selected_storage_policy_quota_status"] = quota_status
    state.run_context["effective_storage_policy_id"] = selection.get("provider_value", "")
    state.run_context["storage_policy_id_source"] = selection.get("source", "unresolved")
