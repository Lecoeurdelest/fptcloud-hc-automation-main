#!/usr/bin/env python3
"""Check coverage thresholds from coverage.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="coverage.json")
    parser.add_argument("--min-queue", type=float, default=85.0)
    args = parser.parse_args()
    path = Path(args.report)
    if not path.exists():
        print(f"Coverage report not found: {path}", file=sys.stderr)
        return 1
    data = json.loads(path.read_text())
    files = data.get("files", {})
    queue_lines = 0
    queue_covered = 0
    for fname, stats in files.items():
        if "hc\\queue" in fname or "hc/queue" in fname:
            queue_lines += stats["summary"]["num_statements"]
            queue_covered += stats["summary"]["covered_lines"]
    if queue_lines == 0:
        print("No queue coverage data", file=sys.stderr)
        return 1
    pct = 100.0 * queue_covered / queue_lines
    print(f"Queue coverage: {pct:.1f}% ({queue_covered}/{queue_lines})")
    if pct < args.min_queue:
        print(f"FAIL: below minimum {args.min_queue}%", file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
