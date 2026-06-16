"""ChecklistProducer — P3.T4, P3.T5, P3.T6.

Orchestrates checklist loading → dependency resolution → Redis enqueue.

Design notes
------------
P3.T5 (optimistic quota): the producer performs NO quota precheck. It assumes
quota is sufficient and lets the Terraform executor surface a provider quota
rejection (C-012, FR-016). There is no quota-query code here by design.

P3.T6 (resumability): on re-submit with the same run_id the Redis dedup ZSET
(`ZADD NX`) will return 0 for every task that was already enqueued, so
`RedisQueue.enqueue()` returns `DUPLICATE` for all of them. No explicit
Postgres state check is needed for the unit-testable dedup path. The full
Postgres-based re-enqueue path (after a Redis wipe, C-015) is wired in Phase 5
when the Postgres writer is live (T-1213 is an integration test, phase P3.T6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hc.checklist.loader import ActionRegistry, ChecklistDoc, ChecklistLoader
from hc.checklist.renderer import TemplateRenderer
from hc.checklist.resolver import DependencyResolver
from hc.models.task import EnqueueResult, TaskSpec
from hc.queue.redis_queue import RedisQueue


@dataclass
class ProducerResult:
    run_id: str
    loaded: int = 0
    enqueued: int = 0
    duplicates: int = 0
    skipped_gap: int = 0
    dry_run: bool = False
    plan: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "run_id": self.run_id,
            "loaded": self.loaded,
            "enqueued": self.enqueued,
            "duplicates": self.duplicates,
            "skipped_gap": self.skipped_gap,
            "dry_run": self.dry_run,
        }
        if self.dry_run:
            d["plan"] = self.plan
        return d


class ChecklistProducer:
    """Load a checklist.yml and enqueue ready tasks onto the Redis Stream.

    Parameters
    ----------
    registry:
        Loaded ActionRegistry.  Callers usually pass
        ``ActionRegistry(Path("config/action_registry.yml"))``.
    queue:
        RedisQueue instance (unit tests supply a fakeredis-backed one).
    renderer:
        TemplateRenderer used to resolve ${context.*} vars.  Defaults to a
        fresh instance (no context vars needed for Phase 3 static checklists).
    """

    def __init__(
        self,
        registry: ActionRegistry,
        queue: RedisQueue,
        renderer: TemplateRenderer | None = None,
    ) -> None:
        self._registry = registry
        self._queue = queue
        self._renderer = renderer or TemplateRenderer()
        self._loader = ChecklistLoader(registry)

    def run(
        self,
        checklist_path: Path,
        run_id: str | None = None,
        context: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> ProducerResult:
        """Load *checklist_path* and enqueue the first wave of ready tasks.

        In dry-run mode no tasks are enqueued; a ``plan`` list is returned
        instead describing what would be enqueued (T-0313).

        Parameters
        ----------
        checklist_path:
            Path to the checklist.yml file.
        run_id:
            Overrides the run_id from the checklist file.
        context:
            Context dict for ${context.*} variable resolution.  Must contain
            only JSON-serialisable scalars (TemplateRenderer validates this).
        dry_run:
            If True, load and resolve but enqueue nothing.
        """
        ctx = context or {}
        doc: ChecklistDoc = self._loader.load(checklist_path, run_id=run_id)
        result = ProducerResult(run_id=doc.run_id, loaded=len(doc.entries), dry_run=dry_run)

        # Resolve vars through the TemplateRenderer for each entry.
        resolved_specs: list[TaskSpec] = []
        for entry in doc.entries:
            rendered_vars = self._renderer.render(entry.task_spec.spec.get("vars", {}), ctx)
            # Rebuild spec with rendered vars (spec_hash stays unchanged — it was
            # computed from the pre-render authored spec at load time).
            resolved_spec = dict(entry.task_spec.spec)
            resolved_spec["vars"] = rendered_vars
            resolved_task = entry.task_spec.model_copy(update={"spec": resolved_spec})
            resolved_specs.append(resolved_task)

        # Build dependency resolver over all entries (explicit + inferred deps).
        dep_pairs = [(entry.task_spec.tc_id, entry.all_depends_on) for entry in doc.entries]
        resolver = DependencyResolver(dep_pairs)

        # For Phase 3, enqueue only the first wave (tasks with no dependencies).
        # The full dependency-unblocking loop (watching Postgres for PASS verdicts
        # and re-enqueueing children) is wired in Phase 5 (P5.T1/P5.T3).
        ready_tc_ids = set(resolver.ready_tasks(completed=set()))
        tc_id_to_task = {t.tc_id: t for t in resolved_specs}
        tc_id_to_entry = {e.task_spec.tc_id: e for e in doc.entries}

        for tc_id in resolver.topological_order:
            if tc_id not in ready_tc_ids:
                continue
            task = tc_id_to_task[tc_id]
            entry = tc_id_to_entry[tc_id]

            if dry_run:
                plan_item: dict[str, Any] = {
                    "tc_id": tc_id,
                    "task_id": task.task_id,
                    "action": task.spec.get("action"),
                    "module": entry.module,
                    "executor": entry.executor,
                    "depends_on": entry.all_depends_on,
                }
                if entry.gap:
                    plan_item["gap"] = entry.gap
                result.plan.append(plan_item)
            else:
                enqueue_result = self._queue.enqueue(task)
                if enqueue_result == EnqueueResult.ENQUEUED:
                    result.enqueued += 1
                else:
                    result.duplicates += 1

        return result
