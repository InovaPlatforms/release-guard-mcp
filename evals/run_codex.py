#!/usr/bin/env python3
"""Run every eval case through a real Codex CLI session and score the tool calls.

Each case starts a fresh `codex exec --json` with Release Guard registered through
`-c mcp_servers.release_guard.*` overrides, the demo backend (no Apple calls), a
read-only sandbox, an empty working directory and a clean CODEX_HOME that holds
only a link to your existing Codex login (no personal AGENTS.md, memories or
plugins leak into the eval).

    python evals/run_codex.py --model gpt-6-sol --effort medium --concurrency 4
    python evals/score.py evals/runs/<run>/predictions.jsonl
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from score import load_jsonl, render, score

CODEX_CANDIDATES = [os.environ.get("CODEX_BIN") or "", shutil.which("codex") or "",
                    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"]


# Keep the agent's toolbox to Release Guard plus Codex's shell: no ChatGPT connectors (e.g. mail search),
# browser or computer use, so tool selection is measured on this server alone.
ISOLATE_FEATURES = ("apps", "browser_use", "browser_use_external", "computer_use", "in_app_browser",
                    "image_generation")


def codex_bin() -> str:
    for candidate in CODEX_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("Codex CLI not found; set CODEX_BIN")


def clean_codex_home() -> Path:
    """A throwaway CODEX_HOME with only a symlink to the existing login."""
    real = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    home = Path(tempfile.mkdtemp(prefix="rg-eval-codex-home-"))
    if not (real / "auth.json").exists():
        raise SystemExit("No Codex login found; run `codex login` first")
    (home / "auth.json").symlink_to(real / "auth.json")
    return home


def run_case(case: dict[str, Any], args: argparse.Namespace, codex_home: Path, out: Path) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(prefix="rg-eval-work-"))
    log_file = out / "server-logs" / f"{case['id']}.log"
    env_table = (f'{{RELEASE_GUARD_BACKEND="demo",ASC_APP_ID="1234567890",'
                 f'RELEASE_GUARD_LOG_FILE="{log_file}"}}')
    cmd = [codex_bin(), "exec", "--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
           "-s", "read-only", "-C", str(work), "-m", args.model,
           *[arg for feature in ISOLATE_FEATURES for arg in ("--disable", feature)],
           "-c", f'model_reasoning_effort="{args.effort}"',
           "-c", 'approval_policy="never"',
           "-c", f'mcp_servers.release_guard.command="{sys.executable}"',
           "-c", 'mcp_servers.release_guard.args=["-m","release_guard"]',
           "-c", f"mcp_servers.release_guard.env={env_table}",
           "-c", 'mcp_servers.release_guard.default_tools_approval_mode="approve"',
           case["prompt"]]
    started = time.monotonic()
    env = dict(os.environ, CODEX_HOME=str(codex_home))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout, env=env, check=False,
                              stdin=subprocess.DEVNULL)
        stdout, code = proc.stdout, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, code = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""), -1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    seconds = round(time.monotonic() - started, 1)
    calls: dict[str, dict[str, Any]] = {}
    commands = 0
    final = ""
    usage: dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if item.get("type") == "mcp_tool_call" and item.get("server") == "release_guard":
            entry = calls.setdefault(item["id"], {"tool": item.get("tool"), "arguments": item.get("arguments")})
            if event.get("type") == "item.completed":
                entry["status"] = item.get("status")
                entry["is_error"] = bool(item.get("error")) or bool((item.get("result") or {}).get("isError"))
        elif item.get("type") == "command_execution" and event.get("type") == "item.completed":
            commands += 1
        elif item.get("type") == "agent_message" and event.get("type") == "item.completed":
            final = item.get("text") or final
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
    (out / "events" / f"{case['id']}.jsonl").write_text(stdout)
    return {"id": case["id"], "calls": list(calls.values()), "commands": commands, "final_message": final,
            "seconds": seconds, "exit_code": code, "usage": usage}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="gpt-6-sol")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--only", nargs="*", help="case ids to run")
    args = parser.parse_args()

    cases = load_jsonl(HERE / "cases.jsonl")
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = HERE / "runs" / f"{stamp}-{args.model}-{args.effort}"
    (out / "events").mkdir(parents=True)
    (out / "server-logs").mkdir()
    version = subprocess.run([codex_bin(), "--version"], capture_output=True, text=True,
                             check=False).stdout.strip()
    home = clean_codex_home()
    try:
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(run_case, c, args, home, out): c["id"] for c in cases}
            preds = []
            for fut in cf.as_completed(futures):
                pred = fut.result()
                preds.append(pred)
                tools = ", ".join(c["tool"] for c in pred["calls"]) or "-"
                print(f"{pred['id']:9} {pred['seconds']:6.1f}s  {tools}", flush=True)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    order = {c["id"]: i for i, c in enumerate(cases)}
    preds.sort(key=lambda p: order[p["id"]])
    with (out / "predictions.jsonl").open("w") as fh:
        for pred in preds:
            fh.write(json.dumps(pred) + "\n")
    summary = score(cases, {p["id"]: p["calls"] for p in preds})
    meta = {"codex_cli": version, "model": args.model, "reasoning_effort": args.effort,
            "backend": "demo (fake App Store Connect, no network)", "run": out.name,
            "disabled_features": list(ISOLATE_FEATURES),
            "median_seconds": sorted(p["seconds"] for p in preds)[len(preds) // 2] if preds else None,
            "shell_commands_run": sum(p["commands"] for p in preds)}
    result = {"meta": meta, **{k: v for k, v in summary.items() if k != "rows"}, "rows": summary["rows"]}
    (out / "summary.json").write_text(json.dumps(result, indent=2))
    print("\n" + json.dumps(meta, indent=2) + "\n" + render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
