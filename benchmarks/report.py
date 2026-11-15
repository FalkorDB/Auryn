"""Shared, zero-dependency plain-text result formatting.

pytest-benchmark's own terminal table is built for comparing many small
microbenchmarks side by side (min/max/mean/stddev/ops/rounds columns) and has
nowhere to put domain-specific numbers like node/edge counts or a parse-cache hit
rate. This is what actually makes a benchmark's result easy to read -- both live
during a run (via print_summary) and later, re-inspecting a saved
--benchmark-json file without re-running anything:

    uv run python benchmarks/report.py benchmarks/results/latest.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def print_summary(title: str, rows: dict[str, object]) -> None:
    label_width = max((len(label) for label in rows), default=0)
    print(f"\n=== {title} ===")
    for label, value in rows.items():
        print(f"  {label.ljust(label_width)} : {value}")


def print_saved_json(path: Path) -> None:
    data = json.loads(path.read_text())
    for entry in data["benchmarks"]:
        rows = {"wall_time_seconds": round(entry["stats"]["mean"], 3), **entry["extra_info"]}
        print_summary(entry["name"], rows)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: uv run python benchmarks/report.py <saved-benchmark.json>")
    print_saved_json(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
