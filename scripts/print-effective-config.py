from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagnose_health_inputs import diagnostics, effective_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Print sanitized resolved health-check configuration.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of key=value lines.")
    parser.add_argument("--with-warnings", action="store_true", help="Include diagnostic warnings.")
    args = parser.parse_args()

    config = effective_config()
    if args.with_warnings:
        data = {"effective_config": config, "warnings": diagnostics()["warnings"]}
    else:
        data = config

    if args.json:
        print(json.dumps(data, indent=2))
        return

    items = data["effective_config"].items() if args.with_warnings else data.items()
    for key, value in items:
        if isinstance(value, list):
            value = ",".join(value)
        print(f"{key}={value}")

    if args.with_warnings:
        for warning in data["warnings"]:
            print(f"WARNING: {warning}")


if __name__ == "__main__":
    main()
