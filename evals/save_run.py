#!/usr/bin/env python3
"""Copy a run's predictions into evals/results and write a re-scored summary.

    python evals/save_run.py evals/runs/<run> v2 "v2 descriptions, MCP server entry"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from score import load_jsonl, score


def main() -> int:
    run, name, note = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    meta = json.loads((run / "summary.json").read_text())["meta"]
    meta["condition"] = note
    preds = load_jsonl(run / "predictions.jsonl")
    summary = score(load_jsonl(HERE / "cases.jsonl"), {p["id"]: p["calls"] for p in preds})
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / f"{name}-predictions.jsonl").write_text("".join(json.dumps(p) + "\n" for p in preds))
    (out / f"{name}-summary.json").write_text(
        json.dumps({"meta": meta, **{k: v for k, v in summary.items() if k != "rows"}}, indent=2) + "\n")
    print(name, summary["end_to_end"], summary["reached_expected_tool"], summary["safety_violations"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
