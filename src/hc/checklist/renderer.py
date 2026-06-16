"""TemplateRenderer — P3.T2.1.

Resolves ${context.<key>} references in TaskSpec.vars before handing them to
the TerraformExecutor. The renderer MUST be deterministic for a given
(raw_vars, context) tuple so that spec_hash stays stable (NFR-009, §2.5.1).

Phase 3 implements Level 2 (interpolated). Level 1 (static) is the identity
path when no ${context.*} references are present. Level 3 (plugin-driven) is
deferred to Phase 7.
"""

from __future__ import annotations

import re
from typing import Any

_CONTEXT_REF = re.compile(r"\$\{context\.([^}]+)\}")

# Scalar types that are considered deterministic (JSON-serialisable primitives).
_DETERMINISTIC_TYPES = (str, int, float, bool, type(None))


class RendererError(ValueError):
    """Raised when the context contains non-deterministic values or a key is missing."""


class TemplateRenderer:
    """Resolve ${context.<key>} references in vars dicts (§2.5.1 Level 2)."""

    def render(self, vars: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Return a new dict with all ${context.*} references resolved.

        Raises RendererError if *context* contains a non-deterministic value
        (anything that is not a JSON-serialisable scalar) or if a referenced
        key is absent from *context*.
        """
        self._validate_context(context)
        return {k: self._resolve(v, context) for k, v in vars.items()}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_context(self, context: dict[str, Any]) -> None:
        for key, value in context.items():
            if not isinstance(value, _DETERMINISTIC_TYPES):
                raise RendererError(
                    f"non-deterministic context value for {key!r}: "
                    f"{type(value).__name__} is not a JSON-serialisable scalar"
                )

    def _resolve(self, obj: Any, context: dict[str, Any]) -> Any:
        if isinstance(obj, str):
            return self._resolve_string(obj, context)
        if isinstance(obj, dict):
            return {k: self._resolve(v, context) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._resolve(item, context) for item in obj]
        return obj

    def _resolve_string(self, s: str, context: dict[str, Any]) -> str:
        def _replace(m: re.Match[str]) -> str:
            key = m.group(1)
            if key not in context:
                raise RendererError(
                    f"template references context key {key!r} which is not in the context dict"
                )
            return str(context[key])

        return _CONTEXT_REF.sub(_replace, s)
