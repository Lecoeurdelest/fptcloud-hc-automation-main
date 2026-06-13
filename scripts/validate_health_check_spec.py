from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "specs" / "health-check.json"
DOC_PATH = ROOT / "docs" / "health-check-spec.md"
VALID_STATUSES = {"automated", "partially_automated", "manual_only", "blocked", "unsupported"}
REQUIRED_FIELDS = {
    "id",
    "manual_check_item",
    "automation_status",
    "required_inputs",
    "required_cloud_resources",
    "expected_result",
    "validation_method",
    "cleanup_behavior",
    "dependency_stages",
    "failure_classification",
    "safe_for_daily_run",
}


def load_spec(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    stages = data.get("stages")
    if not isinstance(stages, list) or not stages:
        return ["spec must contain a non-empty stages list"]

    ids: set[str] = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            errors.append(f"stage[{index}] must be an object")
            continue
        missing = sorted(REQUIRED_FIELDS - set(stage))
        if missing:
            errors.append(f"stage[{index}] missing fields: {', '.join(missing)}")
            continue

        stage_id = str(stage["id"])
        if stage_id in ids:
            errors.append(f"duplicate stage id: {stage_id}")
        ids.add(stage_id)

        status = stage["automation_status"]
        if status not in VALID_STATUSES:
            errors.append(f"{stage_id}: invalid automation_status {status!r}")

        for list_field in ("required_inputs", "required_cloud_resources", "dependency_stages"):
            if not isinstance(stage[list_field], list):
                errors.append(f"{stage_id}: {list_field} must be a list")

        if not isinstance(stage["safe_for_daily_run"], bool):
            errors.append(f"{stage_id}: safe_for_daily_run must be boolean")

        cleanup = str(stage["cleanup_behavior"]).lower()
        if stage["safe_for_daily_run"] and stage["required_cloud_resources"]:
            if "destroy" not in cleanup and "no resources" not in cleanup:
                errors.append(f"{stage_id}: safe stage with cloud resources must define destroy/no-resource cleanup")

    for stage in stages:
        if not isinstance(stage, dict) or "id" not in stage:
            continue
        for dependency in stage.get("dependency_stages", []):
            if dependency not in ids:
                errors.append(f"{stage['id']}: dependency {dependency} is not defined")

    doc = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
    for stage_id in ids:
        if stage_id not in doc:
            errors.append(f"{stage_id}: missing from docs/health-check-spec.md")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the health-check specification.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable validation result.")
    args = parser.parse_args()

    errors = validate(load_spec(SPEC_PATH))
    result = {"ok": not errors, "errors": errors}
    if args.json:
        print(json.dumps(result, indent=2))
    elif errors:
        print("Spec validation failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Spec validation passed.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
