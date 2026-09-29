#!/usr/bin/env python3
"""Render the portfolio screenshots from sanitized data (portfolio/data) with headless Chrome.

    python portfolio/sanitize.py      # raw Codex events -> display-safe data/*.json
    python portfolio/build_assets.py  # data -> src/*.html -> *.png

Every number and line of text in the images comes from a real run: the Codex
session transcript (live DeepChamp app, read-only, ids redacted) and the eval
summaries in evals/results.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA, SRC = HERE / "data", HERE / "src"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
W, H = 1600, 1000

BASE_CSS = """
:root { --surface:#fcfcfb; --page:#f4f3ef; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --hair:#e1e0d9; --axis:#c3c2b7; --good:#0ca30c; --good-text:#006300; --warn:#fab219;
  --crit:#d03b3b; --blue:#2a78d6; --blue-light:#86b6ef; --mono:"SF Mono", ui-monospace, Menlo, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", sans-serif; }
* { box-sizing:border-box; margin:0; padding:0; }
html, body { width:%dpx; min-height:%dpx; overflow:hidden; }
body { background:var(--page); font-family:var(--sans); color:var(--ink); -webkit-font-smoothing:antialiased; }
code, .mono { font-family:var(--mono); }
""" % (W, H)


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def md(text: str) -> str:
    """Tiny markdown: **bold**, `code`, paragraphs and '- ' bullets."""
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        if all(line.startswith("- ") for line in lines):
            items = "".join(f"<li>{inline(line[2:])}</li>" for line in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append("<p>" + "<br>".join(inline(line) for line in lines) + "</p>")
    return "".join(out)


def inline(text: str) -> str:
    text = esc(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", text)


def page(title: str, css: str, body: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{esc(title)}</title>"
            f"<style>{BASE_CSS}{css}</style></head><body>{body}</body></html>")


# -- 1. Codex session ------------------------------------------------------------------------------
SESSION_H = 1190


def steps_html(steps: list[dict]) -> str:
    rows = []
    for step in steps:
        if step["type"] == "command":
            # Show the plugin skill being read; workspace exploration (ls, rg, failed path guesses) is omitted.
            if step.get("exit_code") != 0 or "SKILL.md" not in step["command"] or "rg " in step["command"]:
                continue
            cmd = step["command"].replace("/bin/zsh -lc ", "").strip("'\"")
            rows.append(f"<div class='step cmd'><span class='bullet'>•</span> <span class='k'>Ran</span> "
                        f"{esc(cmd)}</div>")
        elif step["type"] == "tool":
            args = json.dumps(step["arguments"], ensure_ascii=False)
            rows.append(f"<div class='step tool'><span class='bullet'>•</span> Called <span class='srv'>"
                        f"{esc(step['server'])}</span>.<span class='name'>{esc(step['tool'])}</span>"
                        f"<span class='args'>({esc(args)})</span></div>")
            rows.append(f"<div class='res'>└ {result_line(step)}</div>")
        elif step["type"] == "message":
            if step is steps[-1]:
                rows.append(f"<div class='step msg final'>{md(step['text'])}</div>")
            else:
                rows.append(f"<div class='step msg'><span class='bullet'>• </span>{inline(step['text'])}</div>")
    return "".join(rows)


def session_html(live: list[dict], demo: list[dict]) -> str:
    css = """
    body { background:#0d0d0d; padding:36px 56px; height:%dpx; }
    .win { background:#1a1a19; border:1px solid rgba(255,255,255,.10); border-radius:14px;
      box-shadow:0 30px 80px rgba(0,0,0,.45); overflow:hidden; }
    .bar { height:44px; display:flex; align-items:center; gap:8px; padding:0 18px; border-bottom:1px solid #2c2c2a; }
    .dot { width:12px; height:12px; border-radius:50%%; background:#3a3a37; }
    .bar .t { margin-left:14px; color:#c3c2b7; font:500 14px var(--sans); }
    .bar .tag { margin-left:auto; color:#0ca30c; font:600 12px var(--sans); letter-spacing:.02em;
      border:1px solid rgba(12,163,12,.45); border-radius:999px; padding:3px 10px; }
    .pane { padding:18px 28px 16px; color:#e8e7e0; font:14px/1.5 var(--mono); }
    .pane + .pane { border-top:1px dashed #383835; }
    .label { font:700 11.5px var(--sans); letter-spacing:.08em; color:#898781; text-transform:uppercase; margin-bottom:8px; }
    .label b { color:#9085e9; }
    .prompt { color:#fff; margin-bottom:4px; } .prompt b { color:#3987e5; font-weight:600; }
    .meta { color:#898781; font-size:12px; margin-bottom:10px; }
    .step { margin:7px 0; } .bullet { color:#898781; }
    .msg { font-family:var(--sans); font-size:15px; line-height:1.5; color:#e8e7e0; }
    .msg p { margin:3px 0; } .msg strong { color:#fff; } .msg ul { margin:2px 0 2px 22px; }
    .msg code { background:#2c2c2a; padding:1px 5px; border-radius:4px; font-size:12.5px; }
    .cmd { color:#c3c2b7; } .cmd .k { color:#898781; }
    .tool { color:#fff; } .tool .srv { color:#9085e9; } .tool .name { color:#3987e5; font-weight:600; }
    .args { color:#c3c2b7; }
    .res { color:#c3c2b7; margin-left:22px; font-size:13px; }
    .res .ok { color:#0ca30c; font-weight:600; } .res .bad { color:#e66767; font-weight:600; }
    .res .warn { color:#fab219; font-weight:600; }
    .final { margin-top:10px; border-left:3px solid #3987e5; padding:4px 0 4px 16px; }
    .foot { border-top:1px solid #2c2c2a; padding:11px 28px; color:#898781; font:12.5px var(--sans);
      display:flex; gap:26px; }
    .foot b { color:#c3c2b7; font-weight:600; }
    """ % SESSION_H
    live_prompt = ("Where is DeepChamp 2.88 in App Review, and would it pass a full preflight? "
                   "Also confirm the build finished processing.")
    demo_prompt = "Submit 2.4.0 build 42 for App Review."
    body = f"""
    <div class='win'>
      <div class='bar'><span class='dot'></span><span class='dot'></span><span class='dot'></span>
        <span class='t'>codex exec · Release Guard installed as a Codex plugin (MCP server + skill)</span>
        <span class='tag'>0 WRITE REQUESTS</span></div>
      <div class='pane'>
        <div class='label'>Session 1 · <b>live App Store Connect</b> · read-only · Apple resource ids redacted</div>
        <div class='prompt'><b>&gt;</b> {esc(live_prompt)}</div>
        <div class='meta'>codex-cli 0.158 · model gpt-6-sol · plugin release-guard@release-guard-local ·
          default_tools_approval_mode = "writes"</div>
        {steps_html(live)}
      </div>
      <div class='pane'>
        <div class='label'>Session 2 · <b>demo backend</b> · the write gate</div>
        <div class='prompt'><b>&gt;</b> {esc(demo_prompt)}</div>
        {steps_html(demo)}
      </div>
      <div class='foot'><span>Session 1: <b>14</b> GET requests to App Store Connect, <b>0</b> writes</span>
        <span>ES256 tokens scoped to each request</span>
        <span>Session 2: submit_for_review is annotated destructive, so Codex's approval gate held even the dry run</span></div>
    </div>"""
    return page("Codex session", css, body)


def result_line(step: dict) -> str:
    if step.get("error"):
        return f"<span class='warn'>held by Codex</span> · {esc(step['error'])}"
    r = step.get("result") or {}
    if step["tool"] == "check_build_status":
        b = r.get("build") or {}
        enc = "no non-exempt encryption" if b.get("uses_non_exempt_encryption") is False else "unanswered"
        return (f"<span class='ok'>{esc(r.get('verdict', '?'))}</span> · build {esc(b.get('build_number', '?'))} · "
                f"uploaded {esc((b.get('uploaded_date') or '')[:16].replace('T', ' '))} · export compliance: {enc}")
    if step["tool"] == "check_version_state":
        v = r.get("version") or {}
        live = next((x["version_string"] for x in r.get("recent_versions", []) if x.get("phase") == "live"), "?")
        return (f"<span class='warn'>{esc(v.get('app_version_state', '?'))}</span> · build "
                f"{esc(str(v.get('attached_build_number')))} attached · live now: {esc(live)} · "
                f"open submissions: {len(r.get('open_review_submissions', []))}")
    if step["tool"] == "preflight_submission":
        cls = "bad" if r.get("verdict") == "blocked" else "ok"
        return (f"<span class='{cls}'>{esc(r.get('verdict', '?'))}</span> · {r.get('passed')} pass · "
                f"{r.get('failed')} fail ({esc(', '.join(r.get('blocking', [])))}) · {r.get('warned')} warn")
    return esc(json.dumps(r)[:120])


# -- 2. Preflight checklist ------------------------------------------------------------------------
ICONS = {"pass": ("✓", "var(--good)"), "fail": ("✕", "var(--crit)"), "warn": ("!", "#b27a00"),
         "skip": ("–", "var(--muted)")}


def checklist_html(report: dict) -> str:
    css = """
    body { padding:44px 56px; display:grid; grid-template-columns: 1fr 520px; gap:32px; }
    .card { background:var(--surface); border:1px solid rgba(11,11,11,.10); border-radius:16px; padding:28px 30px; }
    .eyebrow { font:600 12px var(--mono); color:var(--blue); letter-spacing:.04em; }
    h1 { font:700 26px/1.2 var(--sans); margin:6px 0 4px; }
    .sub { color:var(--ink2); font-size:14.5px; }
    .verdict { display:flex; gap:10px; align-items:center; margin:16px 0 14px; }
    .pill { font:700 13px var(--sans); padding:5px 12px; border-radius:999px; letter-spacing:.03em; }
    .blocked { background:#fbe9e9; color:#a32929; } .ready { background:#e6f4e6; color:var(--good-text); }
    .count { font-size:13.5px; color:var(--ink2); } .count b { color:var(--ink); }
    .row { display:grid; grid-template-columns: 26px 1fr; gap:12px; padding:9px 0; border-top:1px solid var(--hair); }
    .ic { width:22px; height:22px; border-radius:50%; color:#fff; font:700 12px var(--sans);
      display:flex; align-items:center; justify-content:center; margin-top:1px; }
    .t { font:600 14.5px var(--sans); } .d { color:var(--ink2); font-size:13.5px; margin-top:1px; }
    .fix { margin-top:6px; font-size:13.5px; background:#fdf3f3; border-left:3px solid var(--crit);
      padding:6px 10px; border-radius:0 6px 6px 0; color:#5a1f1f; }
    .side h2 { font:700 16px var(--sans); margin-bottom:10px; }
    pre { font:12.2px/1.5 var(--mono); background:#1a1a19; color:#e8e7e0; border-radius:12px; padding:16px 18px;
      overflow:hidden; white-space:pre-wrap; }
    pre .k { color:#86b6ef; } pre .s { color:#a6d9a6; } pre .n { color:#f0b88a; }
    .facts { margin-top:18px; font-size:13.5px; color:var(--ink2); line-height:1.55; }
    .facts li { margin-left:18px; margin-bottom:4px; }
    """
    rows = []
    for c in report["checks"]:
        glyph, color = ICONS[c["status"]]
        fix = f"<div class='fix'><b>Fix:</b> {esc(c['fix'])}</div>" if c["status"] in ("fail", "warn") and c.get("fix") else ""
        rows.append(f"<div class='row'><div class='ic' style='background:{color}' aria-label='{c['status']}'>{glyph}</div>"
                    f"<div><div class='t'>{esc(c['title'])}</div><div class='d'>{esc(c['detail'])}</div>{fix}</div></div>")
    sample = next(c for c in report["checks"] if c["id"] == "version_editable")
    snippet = json.dumps({"verdict": report["verdict"], "passed": report["passed"], "failed": report["failed"],
                          "blocking": report["blocking"], "checks": [sample, "…12 more"]}, indent=2,
                         ensure_ascii=False)
    snippet = esc(snippet)
    snippet = re.sub(r'(&quot;|")([a-z_]+)\1:', r'<span class="k">"\2"</span>:', snippet)
    snippet = re.sub(r': "(.*?)"', r': <span class="s">"\1"</span>', snippet)
    snippet = re.sub(r": (\d+)", r': <span class="n">\1</span>', snippet)
    verdict_cls = "blocked" if report["verdict"] == "blocked" else "ready"
    body = f"""
    <div class='card'>
      <div class='eyebrow'>preflight_submission · live App Store Connect · read-only</div>
      <h1>DeepChamp: Live Sports Intel — 2.88 (1)</h1>
      <div class='sub'>13 checks across App Store Connect, the local repo and the lint policy, each with a concrete fix.</div>
      <div class='verdict'><span class='pill {verdict_cls}'>{esc(report['verdict'].upper())}</span>
        <span class='count'><b>{report['passed']}</b> pass · <b>{report['failed']}</b> fail · <b>{report['warned']}</b> warn · <b>{report['skipped']}</b> skip</span></div>
      {''.join(rows)}
    </div>
    <div class='side'>
      <h2>What the agent receives</h2>
      <pre>{snippet}</pre>
      <ul class='facts'>
        <li>Typed <code>structuredContent</code> validated against the tool's <code>outputSchema</code>, plus a JSON text mirror.</li>
        <li>The verdict is computed by rules, not generated, so two runs agree.</li>
        <li>The one failure is correct: 2.88 was already <b>waiting for review</b>, so it cannot be submitted again.</li>
        <li>10 GET requests · 3.4 s · request id per tool call · review notes reduced to a length, demo account to a boolean.</li>
      </ul>
    </div>"""
    return page("Preflight checklist", css, body)


# -- 3. Architecture -------------------------------------------------------------------------------
def architecture_html(tests: int, cases: int) -> str:
    css = """
    body { padding:40px 56px; background:var(--page); }
    h1 { font:700 28px var(--sans); } .sub { color:var(--ink2); font-size:15px; margin:4px 0 18px; }
    svg text { font-family: system-ui, -apple-system, sans-serif; }
    """
    def box(x, y, w, h, title, lines, fill="#fcfcfb", stroke="#c3c2b7", title_color="#0b0b0b", mono=None):
        parts = [f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='12' fill='{fill}' stroke='{stroke}' stroke-width='1.5'/>",
                 f"<text x='{x + 16}' y='{y + 28}' font-size='16' font-weight='700' fill='{title_color}'>{esc(title)}</text>"]
        if mono:
            parts.append(f"<text x='{x + w - 16}' y='{y + 28}' font-size='12' text-anchor='end' fill='#898781' "
                         f"font-family='SF Mono, Menlo, monospace'>{esc(mono)}</text>")
        for i, line in enumerate(lines):
            parts.append(f"<text x='{x + 16}' y='{y + 52 + i * 20}' font-size='13.5' fill='#52514e'>{esc(line)}</text>")
        return "".join(parts)

    def arrow(x1, y1, x2, y2, label="", dash=False, above=False):
        d = " stroke-dasharray='5 5'" if dash else ""
        s = (f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='#52514e' stroke-width='1.8'{d} "
             f"marker-end='url(#arr)'/>")
        if label and above:
            s += (f"<text x='{(x1 + x2) / 2}' y='{min(y1, y2) - 9}' font-size='12.5' text-anchor='middle' "
                  f"fill='#52514e'>{esc(label)}</text>")
        elif label:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            s += (f"<rect x='{mx - len(label) * 3.6 - 8}' y='{my - 13}' width='{len(label) * 7.2 + 16}' height='22' "
                  f"rx='6' fill='#f4f3ef'/><text x='{mx}' y='{my + 3}' font-size='12.5' text-anchor='middle' "
                  f"fill='#52514e'>{esc(label)}</text>")
        return s

    svg = f"""
    <svg width='1488' height='860' viewBox='0 -26 1488 860' xmlns='http://www.w3.org/2000/svg'>
      <defs><marker id='arr' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'>
        <path d='M0,0 L10,5 L0,10 z' fill='#52514e'/></marker></defs>
      {box(0, 20, 300, 190, "MCP host", ["Codex CLI · IDE extension · ChatGPT app", "(any MCP client)", "", "approvals: default_tools_approval_mode", '= "writes" → read-only tools run,', "submit_for_review asks the user"], fill="#eef4fc", stroke="#2a78d6", title_color="#184f95")}
      {box(0, 240, 300, 150, "Codex plugin", ["plugin.json + .mcp.json (env_vars only)", "skills/release-readiness/SKILL.md", "local marketplace for install/testing"], fill="#fcfcfb")}
      {arrow(300, 100, 428, 100, "JSON-RPC", above=True)}
      <text x='364' y='122' font-size='12.5' text-anchor='middle' fill='#52514e'>over stdio</text>
      {arrow(150, 240, 150, 212, dash=True)}
      <text x='162' y='231' font-size='12' fill='#898781'>installs into</text>
      <rect x='410' y='0' width='640' height='820' rx='16' fill='none' stroke='#2a78d6' stroke-width='1.5' stroke-dasharray='6 6'/>
      <text x='430' y='-8' font-size='13' fill='#184f95' font-weight='700'>release-guard-mcp · one Python process</text>
      {box(430, 20, 600, 120, "Tools", ["6 tools · inputSchema w/ patterns · Pydantic outputSchema", "annotations: 5 readOnly/idempotent · submit destructive", "request id + deadline + tool_call/tool_result logs"], mono="server.py")}
      {box(430, 160, 600, 120, "Checks & verdicts", ["13-check preflight → pass/fail/warn/skip + fix", "blocked / ready_with_warnings / ready (rules, not LLM)", "guarded submit: dry run → plan → gates → WritePermit"], mono="service.py")}
      {box(430, 300, 290, 110, "Lint policy", ["TOML rules w/ App Review", "guideline refs + field limits", "team overrides"], mono="policy.py")}
      {box(740, 300, 290, 110, "Local repo", ["git merge-base --is-ancestor", "xcconfig (#include aware)", "Info.plist export flag"], mono="repo.py")}
      {box(430, 430, 600, 110, "Typed reads", ["App Store Connect: builds, versions, localizations, review detail,", "IAPs, subscriptions, review submissions (fields[] minimised)", "App Store Server API: notification history (report only)"], mono="asc.py · server_api.py")}
      {box(430, 560, 600, 130, "Resilient transport", ["retries 429/5xx w/ full-jitter backoff · honors Retry-After", "45 s deadline inside Codex's 60 s tool timeout · paging", "read-only guard: non-GET refused without a WritePermit", "next links followed on the Apple host only"], fill="#eef4fc", stroke="#2a78d6", mono="transport.py")}
      {box(430, 710, 290, 90, "Auth", ["ES256 · 15-min tokens", "scope claim = this one GET"], mono="auth.py")}
      {box(740, 710, 290, 90, "Logs", ["JSON on stderr · request ids", "JWT/PEM/key-id redaction"], mono="logs.py")}
      {arrow(730, 140, 730, 160)}
      {arrow(575, 280, 575, 300)} {arrow(885, 280, 885, 300)}
      {arrow(730, 410, 730, 430)}
      {arrow(730, 540, 730, 560)}
      {box(1150, 420, 330, 110, "App Store Connect API", ["GET only (read-only mode)", "JSON:API, links.next paging"], fill="#fcfcfb")}
      {box(1150, 560, 330, 110, "App Store Server API", ["POST notifications/history", "(allowlisted query) · 20/page"], fill="#fcfcfb")}
      {box(1150, 300, 330, 100, "Local git repository", ["AppVersion.xcconfig · Info.plist", "origin/main ancestry"], fill="#fcfcfb")}
      {arrow(1030, 600, 1150, 480)}
      <text x='1060' y='520' font-size='12.5' fill='#52514e' transform='rotate(-45 1060 520)'>HTTPS + scoped JWT</text>
      {arrow(1030, 640, 1150, 615)}
      {arrow(1030, 355, 1150, 350)}
      {box(1150, 20, 330, 250, "Write gates (all required)", ["1  dry_run = false (default true)", "2  confirm = true (default false)", "3  RELEASE_GUARD_ALLOW_WRITES=1", "4  not under a test runner", "5  live backend (demo never writes)", "6  preflight not blocked", "7  WritePermit on every request", "+  Codex approval prompt"], fill="#fdf3f3", stroke="#d03b3b", title_color="#a32929")}
      {box(0, 420, 300, 150, "Demo backend", ["fake App Store Connect + Server API", "Apple JSON:API shapes · fault injection", "used by tests, evals and demos:", "zero Apple calls in CI"], fill="#fcfcfb")}
      {box(0, 600, 300, 200, "Verification", [f"{tests} unit + contract tests (no network)", f"{cases}-case Codex eval with scorer", "live read-only run on a production", "app: 0 write requests", "gitleaks-clean history"], fill="#e6f4e6", stroke="#0ca30c", title_color="#006300")}
    </svg>"""
    body = (f"<h1>Release Guard — architecture</h1><div class='sub'>An MCP server that answers “is this iOS release "
            f"ready?” from inside Codex. The model only sees typed tools; credentials, retries and write safety live "
            f"below it.</div>{svg}")
    return page("Architecture", css, body)


# -- 4. Eval results ------------------------------------------------------------------------------
TAG_ORDER = [("build", "Build status"), ("version", "Version state"), ("preflight", "Preflight"),
             ("reconcile", "Notification audit"), ("lint", "Release-note lint"), ("submit", "Guarded submit"),
             ("safety", "Submit + injection cases"), ("negative", "No tool needed")]
CONDITIONS = [("v1-baseline", "v1 · MCP entry", "#86b6ef"), ("v3-mcp", "v3 · MCP entry", "#2a78d6"),
              ("v3-plugin", "v3 · Codex plugin (MCP + skill)", "#104281")]


def evals_html(runs: dict[str, dict], grid: list[tuple[str, dict | None, dict | None]]) -> str:
    css = """
    body { padding:40px 56px; }
    h1 { font:700 28px var(--sans); } .sub { color:var(--ink2); font-size:15px; margin:4px 0 20px; max-width:1380px; line-height:1.45; }
    .kpis { display:grid; grid-template-columns: repeat(4, 1fr); gap:16px; margin-bottom:20px; }
    .kpi { background:var(--surface); border:1px solid rgba(11,11,11,.10); border-radius:14px; padding:16px 20px; }
    .kpi .l { font-size:13px; color:var(--ink2); } .kpi .v { font:700 40px/1.1 var(--sans); margin-top:4px; }
    .kpi .n { font-size:12.5px; color:var(--muted); margin-top:4px; }
    .grid { display:grid; grid-template-columns: 1fr 500px; gap:20px; }
    .card { background:var(--surface); border:1px solid rgba(11,11,11,.10); border-radius:14px; padding:18px 22px; }
    .card h2 { font:700 16px var(--sans); margin-bottom:2px; } .card .cap { font-size:12.5px; color:var(--muted); margin-bottom:8px; }
    .legend { display:flex; gap:18px; font-size:12.5px; color:var(--ink2); margin-bottom:4px; flex-wrap:wrap; }
    .sw { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:-1px; }
    svg text { font-family: system-ui, -apple-system, sans-serif; }
    .changes li { font-size:13.5px; color:var(--ink2); margin:0 0 8px 18px; line-height:1.45; }
    .changes b { color:var(--ink); }
    table { width:100%; border-collapse:collapse; font-size:13px; margin-top:6px; }
    td, th { text-align:left; padding:5px 6px; border-top:1px solid var(--hair); }
    th { color:var(--muted); font-weight:600; } td.num { font-variant-numeric: tabular-nums; text-align:right; }
    """
    v1, mcp, plug = runs["v1-baseline"], runs["v3-mcp"], runs["v3-plugin"]
    meta = plug["meta"]
    every = [json.loads(f.read_text()) for f in sorted((ROOT / "evals" / "results").glob("*-summary.json"))]
    total_sessions = sum(r["cases"] for r in every)
    violations = sum(r["safety_violations"] for r in every)

    def frac(r: dict) -> str:
        return f"{round(r['end_to_end'] * r['cases'])}/{r['cases']}"

    kpis = f"""
    <div class='kpis'>
      <div class='kpi'><div class='l'>Codex plugin (MCP server + skill)</div><div class='v'>{round(plug['end_to_end'] * 100)}%</div>
        <div class='n'>first call right tool and arguments, 28/28 on gpt-6-sol, gpt-6-luna and gpt-5.5</div></div>
      <div class='kpi'><div class='l'>Same server as a bare MCP entry</div><div class='v'>{round(mcp['end_to_end'] * 100)}%</div>
        <div class='n'>{frac(mcp)} on {esc(meta['model'])} · all 5 misses are text-lint requests</div></div>
      <div class='kpi'><div class='l'>First draft of the tool descriptions</div><div class='v'>{round(v1['end_to_end'] * 100)}%</div>
        <div class='n'>{frac(v1)} · before reading the misses</div></div>
      <div class='kpi'><div class='l'>Safety violations</div><div class='v'>{violations}</div>
        <div class='n'>across all {total_sessions} scored Codex sessions ({len(every)} runs): no submit with dry_run=false, injection ignored</div></div>
    </div>"""
    tags = [(tag, label) for tag, label in TAG_ORDER if tag in plug["by_tag"]]
    left, width, top, bar, gap, group_gap = 230, 520, 26, 11, 3, 18
    group_h = len(CONDITIONS) * (bar + gap) - gap
    height = top + len(tags) * (group_h + group_gap)
    parts = [f"<svg width='{left + width + 70}' height='{height + 6}' xmlns='http://www.w3.org/2000/svg'>"]
    for pos in [0, 0.25, 0.5, 0.75, 1.0]:
        x = left + pos * width
        parts.append(f"<line x1='{x}' y1='{top - 8}' x2='{x}' y2='{height - group_gap + 4}' stroke='#e1e0d9'/>")
        parts.append(f"<text x='{x}' y='{top - 13}' font-size='11.5' fill='#898781' text-anchor='middle'>{int(pos * 100)}%</text>")
    parts.append(f"<line x1='{left}' y1='{top - 8}' x2='{left}' y2='{height - group_gap + 4}' stroke='#c3c2b7'/>")
    for i, (tag, label) in enumerate(tags):
        y0 = top + i * (group_h + group_gap)
        n = plug["by_tag"][tag]["cases"]
        parts.append(f"<text x='{left - 14}' y='{y0 + group_h / 2 - 2}' font-size='13.5' fill='#0b0b0b' text-anchor='end'>"
                     f"{esc(label)}</text><text x='{left - 14}' y='{y0 + group_h / 2 + 13}' font-size='11' fill='#898781' "
                     f"text-anchor='end'>{n} cases</text>")
        for j, (key, name, color) in enumerate(CONDITIONS):
            t = runs[key]["by_tag"][tag]
            w = t["end_to_end"] / t["cases"] * width
            y = y0 + j * (bar + gap)
            if w > 0:
                r = min(4, w / 2)
                # square at the baseline, 4px rounded data end
                parts.append(f"<path d='M{left},{y} h{w - r} a{r},{r} 0 0 1 {r},{r} v{bar - 2 * r} a{r},{r} 0 0 1 "
                             f"{-r},{r} h{-(w - r)} z' fill='{color}'><title>{esc(name)}: {t['end_to_end']}/{t['cases']}"
                             f"</title></path>")
            parts.append(f"<text x='{left + w + 6}' y='{y + bar - 1.5}' font-size='10.5' fill='#52514e'>"
                         f"{t['end_to_end']}/{t['cases']}</text>")
    parts.append("</svg>")
    legend = "".join(f"<span><span class='sw' style='background:{c}'></span>{esc(lbl)}</span>" for _, lbl, c in CONDITIONS)
    def cell(r: dict | None) -> str:
        if r is None:
            return "<td class='num'>–</td>"
        return (f"<td class='num'>{round(r['end_to_end'] * r['cases'])}/{r['cases']} "
                f"<span style='color:var(--muted)'>· lint {r['by_tag']['lint']['end_to_end']}/5</span></td>")

    model_rows = "".join(f"<tr><td>{esc(name)}</td>{cell(mcp_r)}{cell(plug_r)}</tr>" for name, mcp_r, plug_r in grid)
    body = f"""
    <h1>Tool-selection evals in real Codex sessions</h1>
    <div class='sub'>{plug['cases']} natural-language requests, each with the expected tool and arguments, including submit requests that must stay
      dry runs and a prompt injection hidden in release notes. Every case is a fresh <code>codex exec</code> session ({esc(meta['codex_cli'])},
      reasoning {esc(meta['reasoning_effort'])}) on the demo backend with an empty workspace, a clean CODEX_HOME and connectors disabled.
      The scorer grades the first Release Guard call.</div>
    {kpis}
    <div class='grid'>
      <div class='card'><h2>End-to-end pass rate by category ({esc(meta['model'])})</h2>
        <div class='cap'>share of cases whose first Release Guard call is the right tool with the right arguments (routing, not safety: safety violations were 0 in every run)</div>
        <div class='legend'>{legend}</div>
        {''.join(parts)}</div>
      <div class='card'><h2>What the misses taught us</h2>
        <ul class='changes'>
          <li><b>Instructions beat descriptions.</b> v1's server instructions listed a “typical order”, so the agent made two
            redundant calls before every preflight. Rewriting them as a routing table fixed preflight and submit (2/8 → 8/8).</li>
          <li><b>From a bare MCP entry, routing depends on the model.</b> gpt-6-sol and gpt-6-luna answered text questions from
            memory or web search (lint 0/5, even with a much more directive description); gpt-5.5 called the tool.</li>
          <li><b>Shipping it as a plugin made it model-independent.</b> The release-readiness skill routes text questions to
            the tool: 28/28 on every model tested.</li>
        </ul>
        <h2 style='margin-top:12px'>v3 end to end by packaging and model</h2>
        <table><tr><th>model</th><th class='num'>bare MCP entry</th><th class='num'>Codex plugin</th></tr>{model_rows}</table>
      </div>
    </div>"""
    return page("Eval results", css, body)


def render(name: str, content: str, height: int = H) -> Path:
    SRC.mkdir(exist_ok=True)
    src = SRC / f"{name}.html"
    src.write_text(content)
    out = HERE / f"{name}.png"
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                    f"--window-size={W},{height}", f"--screenshot={out}", src.as_uri()],
                   check=True, capture_output=True, timeout=120)
    return out


def count_tests() -> int:
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=ROOT, capture_output=True,
                         text=True, check=False).stdout
    return sum(int(m) for m in re.findall(r"^tests/\S+: (\d+)$", out, re.MULTILINE)) or \
        int(re.search(r"(\d+) tests? collected", out).group(1))


def count_cases() -> int:
    return sum(1 for line in (ROOT / "evals" / "cases.jsonl").read_text().splitlines() if line.strip())


def main() -> int:
    live = json.loads((DATA / "live-session.json").read_text())
    demo = json.loads((DATA / "demo-session.json").read_text())
    report = next(s["result"] for s in live if s["type"] == "tool" and s["tool"] == "preflight_submission")
    outputs = [render("01-codex-session", session_html(live, demo), SESSION_H),
               render("02-preflight-checklist", checklist_html(report)),
               render("03-architecture", architecture_html(count_tests(), count_cases()))]
    results = ROOT / "evals" / "results"
    runs = {key: json.loads((results / f"{key}-summary.json").read_text()) for key, _, _ in CONDITIONS}
    def load(name: str) -> dict | None:
        path = results / f"{name}-summary.json"
        return json.loads(path.read_text()) if path.exists() else None

    base_model = runs["v3-plugin"]["meta"]["model"]
    others = sorted({p.name.split("v3-plugin-", 1)[1][: -len("-summary.json")]
                     for p in results.glob("v3-plugin-*-summary.json")})
    grid = [(base_model, load("v3-mcp"), load("v3-plugin"))] + \
        [(m, load(f"v3-mcp-{m}"), load(f"v3-plugin-{m}")) for m in others]
    outputs.append(render("04-eval-results", evals_html(runs, grid)))
    for out in outputs:
        print(out.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
