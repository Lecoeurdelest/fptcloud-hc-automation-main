"""ChecklistLoader and ActionRegistry — P3.T2.

Loads checklist.yml, validates against JSON Schema, expands defaults,
normalises TC IDs, resolves spec.action → module via the action registry,
and infers default dependency wiring (C-016).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hc.checklist.schema import SchemaValidationError, validate_checklist
from hc.models.checkpoint import Checkpoint, ExpectedAssertion
from hc.models.task import RetryPolicy, TaskSpec


class RegistryError(ValueError):
    """Raised when action_registry.yml is invalid or an action is unknown."""


@dataclass(frozen=True)
class RegistryEntry:
    module: str | None
    executor: str  # "terraform" | "api_fallback"
    validators: list[str]
    resource_key_template: str
    default_depends_on_actions: list[str]
    requires_existing: bool
    gap: str | None


class ActionRegistry:
    """Loads and validates config/action_registry.yml (spec §5.1)."""

    def __init__(self, path: Path) -> None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "actions" not in raw:
            raise RegistryError(f"action_registry.yml must have a top-level 'actions' key: {path}")
        self._entries: dict[str, RegistryEntry] = {}
        for action, cfg in raw["actions"].items():
            if not isinstance(cfg, dict):
                raise RegistryError(f"action {action!r}: config must be a mapping")
            self._entries[action] = RegistryEntry(
                module=cfg.get("module"),
                executor=cfg.get("executor", "terraform"),
                validators=list(cfg.get("validators") or []),
                resource_key_template=str(cfg.get("resource_key_template", "")),
                default_depends_on_actions=list(cfg.get("default_depends_on_actions") or []),
                requires_existing=bool(cfg.get("requires_existing", False)),
                gap=cfg.get("gap"),
            )

    def get_entry(self, action: str) -> RegistryEntry:
        if action not in self._entries:
            raise RegistryError(
                f"unknown action {action!r}; known actions: {sorted(self._entries)}"
            )
        return self._entries[action]

    @property
    def actions(self) -> dict[str, RegistryEntry]:
        return dict(self._entries)


@dataclass
class LoadedEntry:
    """One resolved checklist entry, ready for the DependencyResolver and Producer."""

    checkpoint: Checkpoint
    task_spec: TaskSpec
    depends_on: list[str] = field(default_factory=list)  # explicit TC IDs
    inferred_depends_on: list[str] = field(default_factory=list)  # from registry defaults
    module: str | None = None
    executor: str = "terraform"
    gap: str | None = None

    @property
    def all_depends_on(self) -> list[str]:
        """Union of explicit and inferred dependencies, deduplicated."""
        seen: set[str] = set()
        result: list[str] = []
        for tc_id in self.depends_on + self.inferred_depends_on:
            if tc_id not in seen:
                seen.add(tc_id)
                result.append(tc_id)
        return result


@dataclass
class ChecklistDoc:
    """Parsed and resolved checklist document."""

    run_id: str
    tenant_id: str
    entries: list[LoadedEntry]

    def task_specs(self) -> list[TaskSpec]:
        return [e.task_spec for e in self.entries]


def _normalise_tc_id(raw_id: str | int) -> str:
    """Normalise a TC id: integer 1 → 'TC-001'; string 'TC-001' → 'TC-001'."""
    if isinstance(raw_id, int):
        return f"TC-{raw_id:03d}"
    s = str(raw_id).strip()
    if s.isdigit():
        return f"TC-{int(s):03d}"
    return s


def _expand_defaults(tc_raw: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Merge checklist-level defaults into a single test-case dict (test case wins)."""
    merged = dict(tc_raw)
    default_retry = defaults.get("retry_policy", {})
    tc_retry = merged.get("retry_policy", {})
    if default_retry and not tc_retry:
        merged["retry_policy"] = default_retry
    return merged


def _compute_authored_spec_hash(spec: dict[str, Any]) -> str:
    """Hash only the user-authored spec fields (action + vars + gap).

    Excludes loader-added fields (module, executor) so that registry changes
    do not invalidate existing task hashes.
    """
    authored = {k: v for k, v in spec.items() if k not in ("module", "executor")}
    canonical = json.dumps(authored, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _infer_depends_on(
    action: str,
    registry: ActionRegistry,
    tc_id_by_action: dict[str, list[str]],
) -> list[str]:
    """Infer dependency TC IDs from registry's default_depends_on_actions (C-016)."""
    entry = registry.get_entry(action)
    result: list[str] = []
    for dep_action in entry.default_depends_on_actions:
        result.extend(tc_id_by_action.get(dep_action, []))
    return result


class ChecklistLoader:
    """Loads checklist.yml and resolves actions via the ActionRegistry (P3.T2)."""

    def __init__(self, registry: ActionRegistry) -> None:
        self._registry = registry

    def load(self, checklist_path: Path, run_id: str | None = None) -> ChecklistDoc:
        """Parse, validate, and resolve *checklist_path*.

        *run_id* overrides the run_id in the file (Producer CLI uses this).
        """
        raw = yaml.safe_load(checklist_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SchemaValidationError("checklist.yml must be a YAML mapping")
        validate_checklist(raw)

        effective_run_id = run_id or str(raw["run_id"])
        tenant_id = str(raw["tenant_id"])
        defaults = raw.get("defaults") or {}
        test_cases_raw: list[dict[str, Any]] = raw["test_cases"]

        # First pass: collect tc_id → action mapping for dependency inference.
        tc_id_by_action: dict[str, list[str]] = {}
        for tc_raw in test_cases_raw:
            tc_id = _normalise_tc_id(tc_raw["id"])
            action: str = tc_raw["spec"]["action"]
            tc_id_by_action.setdefault(action, []).append(tc_id)

        entries: list[LoadedEntry] = []
        for tc_raw in test_cases_raw:
            tc_raw = _expand_defaults(tc_raw, defaults)
            tc_id = _normalise_tc_id(tc_raw["id"])
            action = tc_raw["spec"]["action"]
            reg_entry = self._registry.get_entry(action)

            # Build the resolved spec (user-authored fields + loader-added module/executor).
            authored_spec: dict[str, Any] = {
                "action": action,
                "vars": tc_raw["spec"].get("vars") or {},
            }
            if tc_raw["spec"].get("gap"):
                authored_spec["gap"] = tc_raw["spec"]["gap"]

            spec_hash = _compute_authored_spec_hash(authored_spec)

            # Add module / executor for the executor to use (not included in hash).
            runtime_spec = dict(authored_spec)
            if reg_entry.module is not None:
                runtime_spec["module"] = reg_entry.module
            runtime_spec["executor"] = reg_entry.executor

            # Build expected assertions list.
            expected = [
                ExpectedAssertion(
                    **{k: v for k, v in exp.items() if k in ExpectedAssertion.model_fields}
                )
                for exp in tc_raw.get("expected", [])
            ]

            checkpoint = Checkpoint(
                id=tc_id,
                category=tc_raw["category"],
                description=tc_raw["description"],
                spec=runtime_spec,
                expected=expected,
                depends_on=tc_raw.get("depends_on") or [],
            )

            # Build retry policy from merged defaults.
            retry_raw = tc_raw.get("retry_policy", {})
            retry_policy = RetryPolicy(**retry_raw) if retry_raw else RetryPolicy()

            task_spec = TaskSpec(
                run_id=effective_run_id,
                tc_id=tc_id,
                tenant_id=tenant_id,
                spec_hash=spec_hash,
                category=tc_raw["category"],
                description=tc_raw["description"],
                spec=runtime_spec,
                retry_policy=retry_policy,
            )

            explicit_deps: list[str] = [
                _normalise_tc_id(d) for d in (tc_raw.get("depends_on") or [])
            ]
            inferred_deps = _infer_depends_on(action, self._registry, tc_id_by_action)
            # Exclude self from inferred deps (e.g. create_subnet depends_on create_subnet).
            inferred_deps = [d for d in inferred_deps if d != tc_id]
            # Exclude tc_ids already in explicit_deps.
            inferred_deps = [d for d in inferred_deps if d not in explicit_deps]

            entries.append(
                LoadedEntry(
                    checkpoint=checkpoint,
                    task_spec=task_spec,
                    depends_on=explicit_deps,
                    inferred_depends_on=inferred_deps,
                    module=reg_entry.module,
                    executor=reg_entry.executor,
                    gap=reg_entry.gap or tc_raw.get("gap"),
                )
            )

        return ChecklistDoc(run_id=effective_run_id, tenant_id=tenant_id, entries=entries)
