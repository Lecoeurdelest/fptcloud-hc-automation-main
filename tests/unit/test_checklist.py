"""Phase 3 checklist tests — T-0301 through T-0321 (excluding T-0319 / P7.T1).

ID mapping:
  T-0301 schema validates correct checklist
  T-0302 schema rejects missing run_id
  T-0303 schema rejects unknown fields
  T-0304 schema rejects invalid cidr format
  T-0305 loader expands defaults into each test case
  T-0306 loader normalises IDs: 1 → TC-001
  T-0307 spec_hash changes when spec content changes
  T-0308 spec_hash stable when spec content identical
  T-0309 DependencyResolver topological sort: linear chain A→B→C
  T-0310 DependencyResolver rejects cycle A→B→A
  T-0311 DependencyResolver ready_tasks returns only unblocked tasks
  T-0312 DependencyResolver ready_tasks unblocks children when parent PASS
  T-0313 Producer dry-run enqueues 0 tasks, prints plan
  T-0314 Producer resumability: re-submit same run_id → 0 new enqueues
  T-0315 Gap items have expected.type: manual
  T-0316 Action registry maps create_vm to module vm
  T-0317 Action registry rejects unknown action name
  T-0318 Action registry infers default_depends_on_actions correctly
  T-0320 TemplateRenderer resolves ${context.*} refs deterministically
  T-0321 TemplateRenderer rejects non-deterministic context
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from hc.checklist.loader import ActionRegistry, ChecklistLoader, RegistryError
from hc.checklist.producer import ChecklistProducer
from hc.checklist.renderer import RendererError, TemplateRenderer
from hc.checklist.resolver import CyclicDependencyError, DependencyResolver
from hc.checklist.schema import SchemaValidationError, validate_checklist
from hc.queue.redis_queue import RedisQueue

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

REGISTRY_PATH = Path(__file__).parents[2] / "config" / "action_registry.yml"


def _registry() -> ActionRegistry:
    return ActionRegistry(REGISTRY_PATH)


def _loader() -> ChecklistLoader:
    return ChecklistLoader(_registry())


def _minimal_doc(
    *,
    run_id: str = "r1",
    tenant_id: str = "t1",
    test_cases: list[dict[str, Any]] | None = None,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a minimal valid checklist document dict."""
    doc: dict[str, Any] = {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "test_cases": test_cases
        or [
            {
                "id": "TC-001",
                "category": "compute",
                "description": "test",
                "spec": {"action": "create_subnet", "vars": {"cidr": "10.0.0.0/24"}},
                "expected": [{"type": "tf_state"}],
            }
        ],
    }
    if defaults:
        doc["defaults"] = defaults
    return doc


def _write_checklist(tmp_path: Path, doc: dict[str, Any]) -> Path:
    p = tmp_path / "checklist.yml"
    p.write_text(yaml.dump(doc), encoding="utf-8")
    return p


# ──────────────────────────────────────────────────────────────────────────────
# T-0301 → T-0304  JSON Schema
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_schema_validates_correct_checklist() -> None:
    """T-0301 — JSON Schema validates a correct checklist.yml."""
    validate_checklist(_minimal_doc())  # must not raise


@pytest.mark.unit
def test_schema_rejects_missing_run_id() -> None:
    """T-0302 — JSON Schema rejects missing run_id."""
    doc = _minimal_doc()
    del doc["run_id"]
    with pytest.raises(SchemaValidationError, match="run_id"):
        validate_checklist(doc)


@pytest.mark.unit
def test_schema_rejects_unknown_fields() -> None:
    """T-0303 — JSON Schema rejects unknown top-level fields."""
    doc = _minimal_doc()
    doc["unknown_field"] = "oops"
    with pytest.raises(SchemaValidationError):
        validate_checklist(doc)


@pytest.mark.unit
def test_schema_rejects_invalid_cidr() -> None:
    """T-0304 — JSON Schema rejects invalid cidr format in spec.vars."""
    doc = _minimal_doc(
        test_cases=[
            {
                "id": "TC-001",
                "category": "compute",
                "description": "bad cidr",
                "spec": {"action": "create_subnet", "vars": {"cidr": "not-a-cidr"}},
                "expected": [{"type": "tf_state"}],
            }
        ]
    )
    with pytest.raises(SchemaValidationError, match="cidr"):
        validate_checklist(doc)


# ──────────────────────────────────────────────────────────────────────────────
# T-0305 → T-0308  ChecklistLoader
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_loader_expands_defaults(tmp_path: Path) -> None:
    """T-0305 — ChecklistLoader expands defaults.retry_policy into test cases."""
    doc = _minimal_doc(
        defaults={"retry_policy": {"max_attempts": 5, "base_seconds": 60}},
    )
    p = _write_checklist(tmp_path, doc)
    loaded = _loader().load(p)
    entry = loaded.entries[0]
    assert entry.task_spec.retry_policy.max_attempts == 5
    assert entry.task_spec.retry_policy.base_seconds == 60


@pytest.mark.unit
def test_loader_normalises_integer_ids(tmp_path: Path) -> None:
    """T-0306 — ChecklistLoader normalises IDs: integer 1 → 'TC-001'."""
    doc = _minimal_doc(
        test_cases=[
            {
                "id": 1,
                "category": "compute",
                "description": "test",
                "spec": {"action": "create_subnet", "vars": {"cidr": "10.0.0.0/24"}},
                "expected": [{"type": "tf_state"}],
            }
        ]
    )
    p = _write_checklist(tmp_path, doc)
    loaded = _loader().load(p)
    assert loaded.entries[0].task_spec.tc_id == "TC-001"
    assert loaded.entries[0].checkpoint.id == "TC-001"


@pytest.mark.unit
def test_spec_hash_changes_when_spec_changes(tmp_path: Path) -> None:
    """T-0307 — spec_hash changes when spec content changes."""
    base = _minimal_doc()
    p1 = tmp_path / "c1.yml"
    p2 = tmp_path / "c2.yml"
    p1.write_text(yaml.dump(base), encoding="utf-8")

    altered = _minimal_doc(
        test_cases=[
            {
                "id": "TC-001",
                "category": "compute",
                "description": "test",
                "spec": {"action": "create_subnet", "vars": {"cidr": "10.1.0.0/24"}},
                "expected": [{"type": "tf_state"}],
            }
        ]
    )
    p2.write_text(yaml.dump(altered), encoding="utf-8")

    loader = _loader()
    h1 = loader.load(p1).entries[0].task_spec.spec_hash
    h2 = loader.load(p2).entries[0].task_spec.spec_hash
    assert h1 != h2


@pytest.mark.unit
def test_spec_hash_stable_for_identical_content(tmp_path: Path) -> None:
    """T-0308 — spec_hash is stable when spec content is identical."""
    doc = _minimal_doc()
    p = _write_checklist(tmp_path, doc)
    loader = _loader()
    h1 = loader.load(p).entries[0].task_spec.spec_hash
    h2 = loader.load(p).entries[0].task_spec.spec_hash
    assert h1 == h2


# ──────────────────────────────────────────────────────────────────────────────
# T-0309 → T-0312  DependencyResolver
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_dependency_resolver_linear_sort() -> None:
    """T-0309 — DependencyResolver topological sort: linear chain A→B→C."""
    entries = [
        ("TC-003", ["TC-002"]),
        ("TC-001", []),
        ("TC-002", ["TC-001"]),
    ]
    resolver = DependencyResolver(entries)
    order = resolver.topological_order
    assert order.index("TC-001") < order.index("TC-002") < order.index("TC-003")


@pytest.mark.unit
def test_dependency_resolver_rejects_cycle() -> None:
    """T-0310 — DependencyResolver rejects cycle A→B→A."""
    entries = [
        ("TC-001", ["TC-002"]),
        ("TC-002", ["TC-001"]),
    ]
    with pytest.raises(CyclicDependencyError):
        DependencyResolver(entries)


@pytest.mark.unit
def test_dependency_resolver_ready_tasks_only_unblocked() -> None:
    """T-0311 — ready_tasks returns only tasks whose deps are satisfied."""
    entries = [
        ("TC-001", []),
        ("TC-002", ["TC-001"]),
        ("TC-003", ["TC-001", "TC-002"]),
    ]
    resolver = DependencyResolver(entries)
    # Nothing completed: only root is ready.
    assert resolver.ready_tasks(completed=set()) == ["TC-001"]


@pytest.mark.unit
def test_dependency_resolver_unblocks_children_on_parent_pass() -> None:
    """T-0312 — ready_tasks unblocks children when parent is in completed."""
    entries = [
        ("TC-001", []),
        ("TC-002", ["TC-001"]),
        ("TC-003", ["TC-001", "TC-002"]),
    ]
    resolver = DependencyResolver(entries)
    ready = resolver.ready_tasks(completed={"TC-001"})
    assert "TC-002" in ready
    assert "TC-003" not in ready

    ready2 = resolver.ready_tasks(completed={"TC-001", "TC-002"})
    assert "TC-003" in ready2


# ──────────────────────────────────────────────────────────────────────────────
# T-0313, T-0314  Producer
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_producer_dry_run_enqueues_nothing(
    queue: RedisQueue,
    tmp_path: Path,
) -> None:
    """T-0313 — Producer dry-run enqueues 0 tasks and returns a plan."""
    doc = _minimal_doc()
    p = _write_checklist(tmp_path, doc)
    prod = ChecklistProducer(registry=_registry(), queue=queue)
    result = prod.run(p, run_id="run-dry", dry_run=True)

    assert result.dry_run is True
    assert result.enqueued == 0
    assert result.duplicates == 0
    assert len(result.plan) >= 1
    assert queue.peek(count=100) == []


@pytest.mark.unit
def test_producer_resumability_dedup(
    queue: RedisQueue,
    tmp_path: Path,
) -> None:
    """T-0314 — Re-submit with same run_id produces 0 new enqueues (dedup)."""
    doc = _minimal_doc()
    p = _write_checklist(tmp_path, doc)
    prod = ChecklistProducer(registry=_registry(), queue=queue)

    first = prod.run(p, run_id="run-resume")
    second = prod.run(p, run_id="run-resume")

    assert first.enqueued >= 1
    assert second.enqueued == 0
    assert second.duplicates == first.enqueued


# ──────────────────────────────────────────────────────────────────────────────
# T-0315  Gap items
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_gap_items_have_manual_expected_type(tmp_path: Path) -> None:
    """T-0315 — Gap items in the checklist have expected.type == 'manual'."""
    checklist_path = Path(__file__).parents[2] / "checklist.yml"
    if not checklist_path.exists():
        pytest.skip("checklist.yml not found")

    loaded = _loader().load(checklist_path)
    gap_entries = [e for e in loaded.entries if e.gap]

    assert gap_entries, "expected at least one gap entry in checklist.yml"
    for entry in gap_entries:
        manual_assertions = [a for a in entry.checkpoint.expected if a.type == "manual"]
        assert manual_assertions, (
            f"{entry.task_spec.tc_id} is a gap item but has no expected.type=manual assertion"
        )


# ──────────────────────────────────────────────────────────────────────────────
# T-0316 → T-0318  Action Registry
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_registry_maps_create_vm_to_module_vm() -> None:
    """T-0316 — Action registry maps create_vm to module 'vm'."""
    reg = _registry()
    entry = reg.get_entry("create_vm")
    assert entry.module == "vm"


@pytest.mark.unit
def test_registry_rejects_unknown_action() -> None:
    """T-0317 — Action registry raises RegistryError for an unknown action."""
    reg = _registry()
    with pytest.raises(RegistryError, match="unknown action"):
        reg.get_entry("does_not_exist")


@pytest.mark.unit
def test_registry_infers_default_depends_on_actions(tmp_path: Path) -> None:
    """T-0318 — Loader infers default_depends_on_actions from registry for create_vm."""
    doc = _minimal_doc(
        test_cases=[
            {
                "id": "TC-001",
                "category": "compute",
                "description": "subnet",
                "spec": {"action": "create_subnet", "vars": {"cidr": "10.0.0.0/24"}},
                "expected": [{"type": "tf_state"}],
            },
            {
                "id": "TC-002",
                "category": "compute",
                "description": "vm",
                "spec": {
                    "action": "create_vm",
                    "vars": {"os": "ubuntu-22-04", "cpu": 2, "ram_gb": 2, "disk_gb": 40},
                },
                "expected": [{"type": "tf_state"}],
            },
        ]
    )
    p = _write_checklist(tmp_path, doc)
    loaded = _loader().load(p)
    vm_entry = next(e for e in loaded.entries if e.task_spec.tc_id == "TC-002")
    # create_vm has default_depends_on_actions: [create_subnet]
    # TC-001 uses create_subnet → should appear in inferred deps of TC-002.
    assert "TC-001" in vm_entry.inferred_depends_on


# ──────────────────────────────────────────────────────────────────────────────
# T-0320, T-0321  TemplateRenderer
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_renderer_resolves_context_refs_deterministically() -> None:
    """T-0320 — Identical (vars, context) produces identical resolved_vars."""
    renderer = TemplateRenderer()
    vars_in = {"subnet_id": "${context.vpc_id}", "name": "hc-${context.env}"}
    context = {"vpc_id": "vpc-abc123", "env": "staging"}

    result1 = renderer.render(vars_in, context)
    result2 = renderer.render(vars_in, context)

    assert result1 == result2
    assert result1["subnet_id"] == "vpc-abc123"
    assert result1["name"] == "hc-staging"


@pytest.mark.unit
def test_renderer_passes_static_vars_unchanged() -> None:
    """T-0320 (static path) — Vars with no ${context.*} refs are identity-mapped."""
    renderer = TemplateRenderer()
    vars_in = {"cidr": "10.0.0.0/24", "count": 3}
    result = renderer.render(vars_in, {})
    assert result == vars_in


@pytest.mark.unit
def test_renderer_rejects_nondeterministic_context() -> None:
    """T-0321 — TemplateRenderer raises RendererError for non-serialisable context values."""
    renderer = TemplateRenderer()
    vars_in = {"name": "${context.ts}"}
    # datetime is not a JSON-serialisable scalar.
    context: dict[str, Any] = {"ts": datetime.datetime.now()}
    with pytest.raises(RendererError, match="non-deterministic"):
        renderer.render(vars_in, context)


@pytest.mark.unit
def test_renderer_raises_on_missing_context_key() -> None:
    """T-0320 (error path) — RendererError when referenced context key is absent."""
    renderer = TemplateRenderer()
    vars_in = {"id": "${context.missing_key}"}
    with pytest.raises(RendererError, match="missing_key"):
        renderer.render(vars_in, {})
