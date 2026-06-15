from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "specs" / "health-check.json"
DOCS_DIR = ROOT / "docs"
README_PATH = ROOT / "README.md"
IMPLEMENTATION_PATHS = [ROOT / "scripts", ROOT / "src", ROOT / "modules"]
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
REQUIRED_ARTIFACTS = {
    "log.html",
    "run-log.html",
    "implementation-notes.md",
    "spec-coverage-report.md",
    "spec-compliance-report.md",
    "runs/<run_id>/",
    "runs/diagnostics/latest.json",
}
STAGE_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"((?:general|compute|network|object-storage|ticket|backup)\.[a-z0-9-]+(?::[a-z0-9-]+)?)"
    r"(?![A-Za-z0-9_-])"
)
NON_STAGE_REFERENCES = {"network.prefixlen"}


def load_spec(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def implementation_texts() -> list[tuple[Path, str]]:
    texts: list[tuple[Path, str]] = []
    for base in IMPLEMENTATION_PATHS:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".tf", ".ps1", ".sh"}:
                texts.append((path, path.read_text(encoding="utf-8", errors="ignore")))
    return texts


def non_spec_reference_errors() -> list[str]:
    errors: list[str] = []
    paths: list[Path] = []
    if DOCS_DIR.exists():
        paths.extend(path for path in DOCS_DIR.rglob("*") if path.is_file())
    if README_PATH.exists():
        paths.append(README_PATH)
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "non-authoritative" not in text:
            errors.append(f"{path.relative_to(ROOT)}: non-spec reference material must be explicitly non-authoritative and reference specs")
    return errors


def stage_ids_in_implementation() -> dict[str, set[Path]]:
    found: dict[str, set[Path]] = {}
    for path, text in implementation_texts():
        for match in STAGE_ID_PATTERN.findall(text):
            found.setdefault(match, set()).add(path.relative_to(ROOT))
    return found


def validate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    stages = data.get("stages")
    if not isinstance(stages, list) or not stages:
        return ["spec must contain a non-empty stages list"]

    ids: set[str] = set()
    classifications = set(data.get("failure_classifications", []))
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

        if stage["failure_classification"] not in classifications:
            errors.append(f"{stage_id}: failure_classification {stage['failure_classification']!r} is not listed in failure_classifications")

        for list_field in ("required_inputs", "required_cloud_resources", "dependency_stages"):
            if not isinstance(stage[list_field], list):
                errors.append(f"{stage_id}: {list_field} must be a list")

        if not isinstance(stage["safe_for_daily_run"], bool):
            errors.append(f"{stage_id}: safe_for_daily_run must be boolean")

        # Governed by specs/health-check.json constants.INSTANCE_CLEANUP_POLICY:
        # Compute Instance stages may be safe by retaining resources by default and
        # allowing deletion only for explicit quota cleanup.
        cleanup = str(stage["cleanup_behavior"]).lower()
        if stage["safe_for_daily_run"] and stage["required_cloud_resources"]:
            allowed_cleanup_policy = any(
                marker in cleanup
                for marker in ("destroy", "no resources", "retain", "quota cleanup")
            )
            if not allowed_cleanup_policy:
                errors.append(f"{stage_id}: safe stage with cloud resources must define destroy/no-resource/retain cleanup")

    for stage in stages:
        if not isinstance(stage, dict) or "id" not in stage:
            continue
        for dependency in stage.get("dependency_stages", []):
            if dependency not in ids:
                errors.append(f"{stage['id']}: dependency {dependency} is not defined")

    report_events = set(data.get("report_events", []))
    generated_artifacts = data.get("generated_artifacts", [])
    if not isinstance(report_events, set):
        errors.append("report_events must be a list")
        report_events = set()
    declared_artifacts = {
        str(item.get("path"))
        for item in generated_artifacts
        if isinstance(item, dict)
    }
    missing_artifacts = sorted(REQUIRED_ARTIFACTS - declared_artifacts)
    if missing_artifacts:
        errors.append(f"generated_artifacts missing required declarations: {', '.join(missing_artifacts)}")

    allowed_event_suffixes = {":lock", ":destroy", ":context", ":inputs"}
    for found, paths in sorted(stage_ids_in_implementation().items()):
        if found in NON_STAGE_REFERENCES:
            continue
        base = found.split(":", 1)[0]
        suffix = found[len(base):]
        if found in ids or base in ids or found in report_events:
            continue
        if suffix in allowed_event_suffixes and "<stage>" + suffix in report_events and base in ids:
            continue
        path_list = ", ".join(str(path) for path in sorted(paths))
        errors.append(f"{found}: implementation reference lacks spec coverage ({path_list})")

    errors.extend(non_spec_reference_errors())

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
