"""DependencyResolver — P3.T3.

Topological sort with cycle detection over a list of (tc_id, depends_on_tc_ids)
pairs. Exposes ready_tasks(completed) for the Producer to use when deciding which
tasks to enqueue next.
"""

from __future__ import annotations

from collections import deque


class CyclicDependencyError(ValueError):
    """Raised when the dependency graph contains a cycle."""


class DependencyResolver:
    """Topological dependency graph for checklist test cases (P3.T3).

    Parameters
    ----------
    entries:
        Iterable of ``(tc_id, depends_on_tc_ids)`` pairs.  ``tc_id`` is the
        canonical identifier (e.g. ``"TC-001"``); ``depends_on_tc_ids`` is the
        list of tc_ids that must be PASS before this task may be enqueued.
    """

    def __init__(self, entries: list[tuple[str, list[str]]]) -> None:
        self._deps: dict[str, list[str]] = {}
        for tc_id, deps in entries:
            self._deps[tc_id] = list(deps)
        self._order = self._topological_sort()

    def _topological_sort(self) -> list[str]:
        """Kahn's algorithm — raises CyclicDependencyError if a cycle exists."""
        all_ids = set(self._deps)
        # In-degree count: number of unresolved parents per node.
        in_degree: dict[str, int] = {tc_id: 0 for tc_id in all_ids}
        # Reverse adjacency: parent → list of children.
        children: dict[str, list[str]] = {tc_id: [] for tc_id in all_ids}

        for tc_id, deps in self._deps.items():
            for parent in deps:
                if parent not in all_ids:
                    # A dependency references a tc_id not in the checklist.
                    # Treat it as already satisfied (external/manual dependency).
                    continue
                in_degree[tc_id] += 1
                children[parent].append(tc_id)

        queue: deque[str] = deque(tc_id for tc_id, deg in in_degree.items() if deg == 0)
        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for child in children[node]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if len(order) != len(all_ids):
            cycle_nodes = [tc for tc, deg in in_degree.items() if deg > 0]
            raise CyclicDependencyError(f"dependency cycle detected among: {sorted(cycle_nodes)}")
        return order

    @property
    def topological_order(self) -> list[str]:
        """TC IDs in a valid execution order (parents before children)."""
        return list(self._order)

    def ready_tasks(self, completed: set[str]) -> list[str]:
        """Return tc_ids whose every dependency is in *completed*, preserving topo order.

        Only tasks not yet in *completed* are returned.
        """
        not_done = set(self._deps) - completed
        return [
            tc_id
            for tc_id in self._order
            if tc_id in not_done and all(dep in completed for dep in self._deps[tc_id])
        ]
