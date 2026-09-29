#!/usr/bin/env python3
"""Turn raw Codex event streams (portfolio/_raw, gitignored) into display-safe data files.

Masks: temp CODEX_HOME paths, home-directory paths, commit shas, Apple resource ids (already masked by
RELEASE_GUARD_REDACT_IDS=1 at run time), and refuses to write anything that looks
like a JWT or PEM block.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW, DATA = HERE / "_raw", HERE / "data"
_HOME = re.compile(r"/(?:private/)?tmp/[^\s'\"]*?/codex-plugin-home")
_TMP = re.compile(r"/(?:private/)?(?:tmp|var/folders)/[^\s'\"|]*")
_USER_HOME = re.compile(r"/Users/[^/\s'\"]+")
_SHA = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b")


def clean(value):
    if isinstance(value, str):
        value = _HOME.sub("$CODEX_HOME", value)
        value = _USER_HOME.sub("~", value)
        value = _TMP.sub("$TMPDIR/…", value)
        value = _SHA.sub(lambda m: m.group(0)[:4] + "…", value)
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in ("commit", "ref_sha") and isinstance(v, str):
                out[k] = (v[:4] + "…") if v else v
            else:
                out[k] = clean(v)
        return out
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def transcript(events_path: Path) -> list[dict]:
    steps = []
    for line in events_path.read_text().splitlines():
        event = json.loads(line)
        item = event.get("item") or {}
        if event.get("type") != "item.completed" or not item:
            continue
        kind = item.get("type")
        if kind == "agent_message":
            steps.append({"type": "message", "text": item.get("text", "")})
        elif kind == "command_execution":
            steps.append({"type": "command", "command": item.get("command", ""), "exit_code": item.get("exit_code")})
        elif kind == "mcp_tool_call":
            result = item.get("result") or {}
            steps.append({"type": "tool", "server": item.get("server"), "tool": item.get("tool"),
                          "arguments": item.get("arguments"), "status": item.get("status"),
                          "error": (item.get("error") or {}).get("message") if item.get("error") else None,
                          "result": result.get("structured_content") or result.get("structuredContent")})
    return clean(steps)


def main() -> int:
    DATA.mkdir(exist_ok=True)
    for name in ("live-session", "demo-session"):
        src = RAW / f"{name}-events.jsonl"
        if not src.exists():
            continue
        steps = transcript(src)
        blob = json.dumps(steps)
        if re.search(r"eyJ[\w-]{10,}\.[\w-]{10,}\.", blob) or "PRIVATE KEY" in blob:
            print(f"refusing to write {name}: looks like a secret", file=sys.stderr)
            return 1
        (DATA / f"{name}.json").write_text(json.dumps(steps, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote data/{name}.json ({len(steps)} steps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
