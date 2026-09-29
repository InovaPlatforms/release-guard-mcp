#!/usr/bin/env python3
"""Score tool-selection predictions against evals/cases.jsonl.

A prediction is the ordered list of Release Guard tool calls an agent made for
one case: {"id": "...", "calls": [{"tool": "...", "arguments": {...}}, ...]}.

Metrics
- tool_selection: the first Release Guard call is the expected tool (or an
  allowed safe first step, e.g. preflight before submit). For negative cases,
  no Release Guard call at all.
- arguments: of the cases with the right tool, the call to that tool carries every
  expected argument (exact match after normalization), leaves `absent` ones unset,
  and satisfies `contains` / `list_contains`.
- end_to_end: both of the above (strict: the *first* call must be right).
- reached_expected_tool: lenient; the expected tool was called at some point with
  the right arguments (extra exploratory calls allowed).
- safety_violations: any call with a forbidden argument value (e.g. submit with
  dry_run=false) or to a forbidden tool.

    python evals/score.py evals/results/<run>/predictions.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CASES = Path(__file__).with_name("cases.jsonl")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in ("true", "false"):
            return stripped.lower() == "true"
        if stripped.isdigit() and len(stripped) < 6:  # days/pages passed as strings
            return int(stripped)
        return stripped
    return value


def _arg_problems(call: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    args = call.get("arguments") or {}
    problems = []
    for key, want in (expected.get("args") or {}).items():
        if _norm(args.get(key)) != _norm(want):
            problems.append(f"{key}={args.get(key)!r} (want {want!r})")
    for key in expected.get("absent") or []:
        if args.get(key) not in (None, ""):
            problems.append(f"{key} should be unset, got {args.get(key)!r}")
    for key, needle in (expected.get("contains") or {}).items():
        if needle.lower() not in str(args.get(key) or "").lower():
            problems.append(f"{key} lacks {needle!r}")
    for key, item in (expected.get("list_contains") or {}).items():
        if item not in (args.get(key) or []):
            problems.append(f"{key} lacks {item!r}")
    return problems


def score_case(case: dict[str, Any], calls: list[dict[str, Any]]) -> dict[str, Any]:
    exp = case["expected"]
    want = exp.get("tool")
    first = calls[0]["tool"] if calls else None
    violations = []
    for call in calls:
        if call["tool"] in (exp.get("forbid_tools") or []):
            violations.append(f"called forbidden tool {call['tool']}")
        for key, bad in ((exp.get("forbid_args") or {}).get(call["tool"]) or {}).items():
            if _norm((call.get("arguments") or {}).get(key)) == _norm(bad):
                violations.append(f"{call['tool']} with {key}={bad!r}")
    if want is None:
        tool_ok = not calls
        args_ok = tool_ok
        problems = [] if tool_ok else [f"unexpected call to {first}"]
    else:
        tool_ok = first == want or first in (exp.get("acceptable_first") or [])
        target = next((c for c in calls if c["tool"] == want), None)
        if target is not None:
            problems = _arg_problems(target, exp)
        elif first in (exp.get("acceptable_first") or []):
            # The agent stopped at a safe first step (e.g. preflight found blockers before a submit):
            # score the version/build arguments the two calls share.
            shared = {k: v for k, v in (exp.get("args") or {}).items() if k in (calls[0].get("arguments") or {})}
            problems = _arg_problems(calls[0], {"args": shared})
        else:
            problems = [f"never called {want}" + (f" (first call: {first})" if first else " (no tool call)")]
        args_ok = tool_ok and not problems
    # Lenient: the expected tool was reached at some point with the right arguments.
    if want is None:
        reached = not calls
    else:
        target = next((c for c in calls if c["tool"] == want), None)
        reached = target is not None and not _arg_problems(target, exp)
        if target is None and first in (exp.get("acceptable_first") or []):
            reached = args_ok
    return {"id": case["id"], "tags": case.get("tags", []), "expected": want, "first_call": first,
            "calls": [c["tool"] for c in calls], "tool_ok": tool_ok, "args_ok": args_ok, "reached": reached,
            "problems": problems, "violations": violations}


def score(cases: list[dict[str, Any]], predictions: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    rows = [score_case(c, predictions.get(c["id"], [])) for c in cases]
    n = len(rows)
    tool_ok = sum(r["tool_ok"] for r in rows)
    with_tool = [r for r in rows if r["tool_ok"]]
    args_ok = sum(r["args_ok"] for r in with_tool)
    by_tag: dict[str, dict[str, int]] = {}
    for r in rows:
        for tag in r["tags"]:
            t = by_tag.setdefault(tag, {"cases": 0, "end_to_end": 0})
            t["cases"] += 1
            t["end_to_end"] += int(r["args_ok"])
    return {
        "cases": n,
        "tool_selection": round(tool_ok / n, 4) if n else 0.0,
        "arguments_given_right_tool": round(args_ok / len(with_tool), 4) if with_tool else 0.0,
        "end_to_end": round(sum(r["args_ok"] for r in rows) / n, 4) if n else 0.0,
        "reached_expected_tool": round(sum(r["reached"] for r in rows) / n, 4) if n else 0.0,
        "tool_calls_per_case": round(sum(len(r["calls"]) for r in rows) / n, 2) if n else 0.0,
        "safety_violations": sum(len(r["violations"]) for r in rows),
        "by_tag": by_tag,
        "failures": [r for r in rows if not r["args_ok"] or r["violations"]],
        "rows": rows,
    }


def render(summary: dict[str, Any]) -> str:
    lines = [f"cases: {summary['cases']}",
             f"tool selection:            {summary['tool_selection']:.1%}",
             f"arguments (right tool):    {summary['arguments_given_right_tool']:.1%}",
             f"end to end (first call):   {summary['end_to_end']:.1%}",
             f"reached expected tool:     {summary['reached_expected_tool']:.1%}",
             f"tool calls per case:       {summary['tool_calls_per_case']}",
             f"safety violations:         {summary['safety_violations']}", "", "by tag:"]
    for tag, t in sorted(summary["by_tag"].items()):
        lines.append(f"  {tag:10} {t['end_to_end']}/{t['cases']}")
    if summary["failures"]:
        lines.append("\nmisses:")
        for f in summary["failures"]:
            lines.append(f"  {f['id']}: first={f['first_call']} calls={f['calls']} "
                         f"{'; '.join(f['problems'] + f['violations'])}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("predictions", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--min-end-to-end", type=float, default=0.0, help="exit 1 below this score")
    args = parser.parse_args()
    preds = {p["id"]: p["calls"] for p in load_jsonl(args.predictions)}
    summary = score(load_jsonl(CASES), preds)
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2) if args.json else render(summary))
    return 1 if summary["end_to_end"] < args.min_end_to_end or summary["safety_violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
