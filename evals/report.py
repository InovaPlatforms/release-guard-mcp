#!/usr/bin/env python3
"""Print a Markdown table of every saved eval run in evals/results."""

from __future__ import annotations

import json
from pathlib import Path

ORDER = ["v1-baseline", "v2", "v3-mcp", "v3-plugin"]


def main() -> None:
    results = Path(__file__).resolve().parent / "results"
    runs = {p.name[: -len("-summary.json")]: json.loads(p.read_text()) for p in results.glob("*-summary.json")}
    keys = [k for k in ORDER if k in runs] + sorted(k for k in runs if k not in ORDER)
    print("| Run | Condition | Model | End to end (first call) | Reached expected tool | Arguments, given right tool "
          "| Safety violations |")
    print("|---|---|---|---|---|---|---|")
    for key in keys:
        r = runs[key]
        n = r["cases"]
        print(f"| `{key}` | {r['meta'].get('condition', '')} | {r['meta']['model']} | "
              f"{r['end_to_end']:.1%} ({round(r['end_to_end'] * n)}/{n}) | {r['reached_expected_tool']:.1%} | "
              f"{r['arguments_given_right_tool']:.1%} | {r['safety_violations']} |")


if __name__ == "__main__":
    main()
