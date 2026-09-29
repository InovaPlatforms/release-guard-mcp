# Release Guard: portfolio facts

James Coholan: Lead AI PM at System One, founding member of a 2021 YC company, three years building AI
products full-time. This project supports an application for **Applied AI Engineer, Plugins**
(ChatGPT and Codex).

## Problem

An iOS release involves about a dozen App Store Connect checks that live in people's heads:

- the build is still processing, or expired;
- export compliance is unanswered;
- one locale has an empty What's New;
- the notes say "20% off" or "subscribe on our website", which App Review rejects;
- a first-time subscription is `READY_TO_SUBMIT` but not attached to the version;
- another version is stuck in review;
- the repo's build number is already behind what was uploaded;
- the archive came from a commit that never reached `main`.

For DeepChamp (a live app with paying users) James shipped 2.82 through 2.88 in a week with one-off
Python scripts that hardcoded ids and mixed reads with writes. Coding agents can now run these steps,
but handing an agent an App Store Connect key without guardrails is how an unreviewed build ships. The
problem: **let an agent in Codex answer "is this release ready?" and walk it to App Review, with safety
that does not depend on the model behaving.**

## Decomposition

1. **Questions, not endpoints.** Six tools map to the questions a release owner asks: did it
   process, where is it, can I submit, is this text OK, did Apple's notifications arrive, submit it.
   The agent selects by intent; the logic lives in code.
2. **One composite check.** `preflight_submission` runs 13 checks across App Store Connect, the local
   repo (git ancestry, xcconfig, Info.plist) and the lint policy. The verdict
   (`blocked` / `ready_with_warnings` / `ready`) is computed by rules, not generated, and every check
   carries a concrete fix.
3. **A reliability layer the model never sees.** Scoped ES256 auth, retries with full-jitter backoff,
   `Retry-After`, a deadline sized to Codex's tool timeout, pagination, and a read-only transport
   guard.
4. **Packaging for Codex.** Plain MCP server entry (`codex mcp add` / `config.toml`) **and** a Codex
   plugin: `.codex-plugin/plugin.json`, `.mcp.json` that forwards env var *names* only, a
   `release-readiness` skill, and a local marketplace.
5. **Measurement.** Contract tests pin behaviour. An eval harness runs natural-language requests
   through real Codex sessions and scores tool choice, arguments and safety.

## Design decisions (and why)

- **Read-only by default, enforced below the model.** The transport refuses every non-GET request
  unless it carries a `WritePermit`, an object only the submit path can mint after seven gates:
  `dry_run=false`, `confirm=true`, a server started with `RELEASE_GUARD_ALLOW_WRITES=1`, not under a
  test runner, live backend, preflight not blocked, permit present. A prompt-injected
  `confirm=true` is not enough.
- **Annotations are load-bearing.** Five tools are `readOnlyHint=true`; submit is `destructiveHint=true`.
  With Codex's `default_tools_approval_mode = "writes"`, the read tools run without prompts and submit
  always asks. The demo session shows it: Codex held even the dry run for approval. We kept that,
  so a human sees every call to the one tool that can ship code.
- **Per-request token scopes.** Every GET token carries Apple's `scope` claim naming that exact
  request. Verified live: a token scoped to one request got **403** on another. A token leaked
  through a proxy log can re-read one resource and never write.
- **Least data into the model's context.** `fields[...]` on every request. The demo-account password is
  never requested. Review notes become a length plus lint result, and contact details become booleans.
- **Deadline under the host's timeout.** A 45 s per-call deadline inside Codex's 60 s default, and
  `Retry-After` waits over 20 s become an actionable error ("retry after ~900 s") instead of a hung
  tool.
- **Policy as data.** Lint rules live in TOML, each citing the App Review guideline it protects.
  Teams extend them; an example sports-analytics policy flags "guaranteed winners" and "lock of the
  day".
- **A fake App Store Connect instead of recorded cassettes.** One in-process fake (Apple's JSON:API
  shapes, filters, pagination, 429/5xx/403 fault injection) powers the tests, the evals and a
  `demo` mode. CI and demos make zero Apple calls, and scenarios are one-line mutations.
- **Report-only reconcile.** Replaying Apple notifications is a side effect with money attached, so
  the tool only counts and explains; replay stays with the team's own tooling.

## What's innovative

- **Eval-driven tool descriptions in real Codex sessions.** The eval harness drives `codex exec` in a
  clean `CODEX_HOME` with connectors off, and grades the first tool call, the arguments and safety.
  The misses changed the design:
  - v1's server instructions listed a "typical order", so agents made two redundant calls before
    every preflight;
  - on gpt-6-sol, agents answered release-note questions from memory or web search instead of
    calling the lint tool.
  Fixing the instructions took preflight/submit from 2/8 to 8/8. A much more directive lint
  description alone still scored 0/5 on gpt-6-sol. Across models, the bare MCP entry scored 23/28
  (gpt-6-sol), 20/28 (gpt-6-luna) and 28/28 (gpt-5.5). The gap closed only once the Codex **plugin
  skill** routed text questions to the tool. With the plugin, all three models scored 28/28. That is an argument for shipping plugins
  (tools plus skills), not bare servers: the skill makes routing independent of the model.
- **Observation about Codex's tool surface.** In codex-cli 0.158, MCP tools are called from inside
  Codex's code-mode `exec` tool (the agent listed them as `mcp__release_guard__*` callables). That is a
  plausible reason server instructions and skills outweigh a single tool description for routing. It
  is a hypothesis from these runs, not a measured cause.
- **Safety measured, not asserted.** The eval set includes submit requests that must stay dry runs and a
  prompt injection hidden in release notes. Safety violations are a scored metric (0 in every run).
- **Grounded in production.** The checks encode rules learned shipping a real app: one open review
  submission per version, `subscriptionSubmissions` for first-time subscriptions, the What's New
  banned-word list, a report-only notification audit.

## Numbers

**Code**
- About 2,800 lines of Python in `src/`: 6 tools, 13 preflight checks, 9 default lint rules, each citing
  an App Review guideline.
- 112 tests (unit plus MCP contract tests), all passing, with no network (a fixture blocks
  real sockets). They drive the real server through the SDK's in-memory client in handshake and
  2026-07-28 modes. CI runs Python 3.11, 3.12 and 3.13, re-scores the recorded eval run and runs
  gitleaks over the full history.

**Evals.** 28 cases:
- 5 build, 5 version, 5 preflight, 3 notification audit, 5 lint, 3 submit, 1 injection, 2 no-tool;
- 4 of them are safety-tagged (3 submit + 1 injection).

Every case is a fresh `codex exec` (codex-cli 0.158) with a clean `CODEX_HOME`, connectors disabled
and the demo backend.

| Run | Condition | Model | End to end (first call) | Reached expected tool | Arguments, given right tool | Safety violations |
|---|---|---|---|---|---|---|
| `v1-baseline` | v1 descriptions, MCP server entry | gpt-6-sol | 60.7% (17/28) | 82.1% | 100.0% | 0 |
| `v2` | v2 descriptions, MCP server entry | gpt-6-sol | 82.1% (23/28) | 82.1% | 100.0% | 0 |
| `v3-mcp` | v3 descriptions, MCP server entry (no plugin skill) | gpt-6-sol | 82.1% (23/28) | 82.1% | 100.0% | 0 |
| `v3-plugin` | v3 descriptions, installed as a Codex plugin (MCP server + skill) | gpt-6-sol | 100.0% (28/28) | 100.0% | 100.0% | 0 |
| `v3-mcp-gpt-5.5` | v3 descriptions, MCP server entry, gpt-5.5 | gpt-5.5 | 100.0% (28/28) | 100.0% | 100.0% | 0 |
| `v3-mcp-gpt-6-luna` | v3 descriptions, MCP server entry, gpt-6-luna | gpt-6-luna | 71.4% (20/28) | 71.4% | 100.0% | 0 |
| `v3-plugin-gpt-5.5` | v3 descriptions, Codex plugin, gpt-5.5 | gpt-5.5 | 100.0% (28/28) | 100.0% | 100.0% | 0 |
| `v3-plugin-gpt-6-luna` | v3 descriptions, Codex plugin, gpt-6-luna | gpt-6-luna | 100.0% (28/28) | 100.0% | 100.0% | 0 |

- As a Codex plugin, v3 scored 28/28 on all three models (gpt-6-sol, gpt-6-luna, gpt-5.5). As a bare
  MCP entry it depends on the model:
  - gpt-6-sol: 23/28, missing the 5 text-lint cases;
  - gpt-6-luna: 20/28, missing lint plus 3 cases where it made no tool call;
  - gpt-5.5: 28/28.
- Safety violations were 0 in every run: no submit with `dry_run=false` or `confirm=true`, and no
  submit call from the injected release note.
- Caveats: one run per condition; the cases were written by the author; the demo backend is used for
  evals.

**Live, read-only verification** against DeepChamp (App Store id 6742149750, production), with its real
App Store Connect key:
- `check_build_status` 2.88: build 1 `VALID`.
- `check_version_state`: 2.88 `WAITING_FOR_REVIEW`; 2.87 live.
- `preflight_submission` 2.88 (1): 12 of 13 checks pass, in 10 requests and 3.4 s. The one
  failure is correct: the version is already in review.
- `reconcile_server_notifications`: 0 undelivered in 14 days; 64 of 64 delivered over 2 days across
  4 pages, which exercised live pagination.
- `submit_for_review`: the dry run planned 3 writes and executed none. With `confirm=true` it
  answered `refused` because the server is read-only.
- **0 write requests** in the server logs of every live session. The only non-GET requests were
  notification-history queries.
- A token scoped to one GET was rejected with 403 on a different GET.
- The Codex plugin installed from the local marketplace (`codex plugin add`) and ran a real session
  against the live app: 14 GETs, 0 writes.


## Where to look

- Repo: `InovaPlatforms/release-guard-mcp` (private until approved to publish)
- `src/release_guard/server.py`: tool surface; `service.py`: checks; `transport.py`: reliability and
  the read-only guard; `auth.py`: scoped JWTs
- `docs/ARCHITECTURE.md`, `docs/SECURITY.md`
- `evals/cases.jsonl`, `evals/run_codex.py`, `evals/score.py`, `evals/results/`
- `plugin/release-guard/`: the Codex plugin; `.agents/plugins/marketplace.json`
- Screenshots: `portfolio/01-codex-session.png`, `02-preflight-checklist.png`, `03-architecture.png`,
  `04-eval-results.png`, rendered by `portfolio/build_assets.py` from sanitized real run data in
  `portfolio/data/`
