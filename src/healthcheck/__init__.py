"""Live FPT Cloud health-check harness, split from scripts/run_health_checks.py.

This package is a behavior-preserving refactor of the monolithic runner. The
module boundaries follow specs/ (config, spec_loader, logging, reporting,
discovery, terraform_executor, instance_runner, classification, cleanup,
runner), backed by two supporting modules: ``state`` (per-run constants + shared
mutable run state) and ``models`` (dataclasses).

``scripts/run_health_checks.py`` remains as a thin compatibility facade that
re-exports this package's names and provides the CLI entrypoint.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the package's dependencies importable regardless of how it is loaded:
#   - diagnose_health_inputs lives under scripts/
#   - hc.inventory.fptcloud_inventory lives under src/
_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(_ROOT / "src"), str(_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
