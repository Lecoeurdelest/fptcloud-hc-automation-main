"""Compatibility facade + CLI for the FPT Cloud health-check harness.

The implementation now lives in the ``healthcheck`` package
(src/healthcheck/*). This module re-exports that package's names so existing
importers and tests that load ``run_health_checks`` as a flat module keep
working: shared mutable state (``events``, ``run_context``, ``stage_status``,
``instance_validation``, ``existing_subnet_inventory``, the queues) is the same
object the package mutates, and constants/functions resolve to their package
homes.

Run it as a CLI:  ``py -3.11 scripts/run_health_checks.py [--stage ID]
[--view log.json --filter MODE]``.
"""
from __future__ import annotations

import subprocess  # noqa: F401  (kept for backward-compat attribute access)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Submodule objects (so `run_health_checks.<module>.<attr>` also works).
from healthcheck import (  # noqa: E402,F401
    classification,
    cleanup,
    config,
    discovery,
    instance_runner,
    logging,
    models,
    object_storage_runner,
    reporting,
    runner,
    spec_loader,
    stage_plan,
    state,
    terraform_executor,
)

# Flat re-exports (mirror the historical single-module namespace).
from healthcheck.state import *  # noqa: E402,F401,F403
from healthcheck.models import *  # noqa: E402,F401,F403
from healthcheck.config import *  # noqa: E402,F401,F403
from healthcheck.classification import *  # noqa: E402,F401,F403
from healthcheck.logging import *  # noqa: E402,F401,F403
from healthcheck.reporting import *  # noqa: E402,F401,F403
from healthcheck.terraform_executor import *  # noqa: E402,F401,F403
from healthcheck.spec_loader import *  # noqa: E402,F401,F403
from healthcheck.stage_plan import *  # noqa: E402,F401,F403
from healthcheck.discovery import *  # noqa: E402,F401,F403
from healthcheck.instance_runner import *  # noqa: E402,F401,F403
from healthcheck.object_storage_runner import *  # noqa: E402,F401,F403
from healthcheck.cleanup import *  # noqa: E402,F401,F403
from healthcheck.runner import *  # noqa: E402,F401,F403

# Pin names that collide across modules to their historical meaning:
#   - ``run`` is the Terraform subprocess wrapper (NOT runner.run orchestration).
run = terraform_executor.run  # noqa: F811
_ImageCreateResult = models._ImageCreateResult

# The historical entrypoint name.
main = runner.main


if __name__ == "__main__":
    main()
