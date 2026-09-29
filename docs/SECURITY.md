# Security model

Release Guard sits between a language model and an account that can ship an app to
millions of phones. The design assumption is that the model can be wrong or
manipulated, for example by instructions hidden in release notes. So every safety
property is enforced in code, below the model, and none depends on the model
choosing to behave.

## Key handling

- **Where keys come from.** `ASC_PRIVATE_KEY_PATH` / `ASC_IAP_KEY_PATH` point at
  `.p8` files. Key ids, issuer id and paths come from the environment the MCP host
  passes (Codex `env` / `env_vars`). Nothing is hardcoded, and the Codex plugin's
  `.mcp.json` forwards variable *names* only (`env_vars`), never values.
- **When they are read.** The key file is read lazily on the first tool call that
  needs it, and held only in process memory. A world- or group-readable key file
  logs a `key_permissions_too_open` warning (`chmod 600`).
- **Never shown.** `PrivateKey`, `AscTokenProvider` and `ServerApiTokenProvider`
  have redacted `repr()`s. The key material, key ids and issuer id are registered
  with a log redactor that also masks any JWT, PEM block or bearer token in every
  log line. `release-guard-mcp --check` reports configuration as booleans only.
  Tests assert that tool output and logs contain none of these values.
- **Short-lived tokens.** 15-minute ES256 tokens (Apple allows up to 20), cached in
  memory only.
- **Per-request scope.** GET tokens carry Apple's `scope` claim naming the one
  request they are for (`GET /v1/builds?filter[app]=…`). A token that leaks
  through a proxy log or a crash report can re-read that one resource and nothing
  else, and can never write. Verified against the live API: a token scoped to one
  request was rejected with 403 on a different request.

## Least privilege

- **Recommended App Store Connect role.** Checks need read access to apps, builds,
  versions, localizations, review details, in-app purchases and review submissions.
  Apple's roles are coarse, so use a **dedicated team key** for Release Guard.
  Revoke it independently of your CI or upload keys, and give it the least role
  that returns those resources in your team (Developer covers builds and versions;
  in-app purchase reads may need App Manager). A section the key cannot read comes
  back as a `skip` with the 403 explained, not a crash.
- **App Store Server API.** Uses a separate In-App Purchase key, and only the
  notification-history endpoint.
- **Fields.** Every request asks only for the fields it needs. The demo-account
  password is never requested. Review notes are summarized as a length and lint
  result. The demo account name and contact email are reduced to booleans.
- **Network.** Only `api.appstoreconnect.apple.com` and `api.storekit(-sandbox).apple.com`.
  Pagination links pointing at any other host are refused, so a token cannot be
  exfiltrated by a crafted `links.next`.

## Dry-run defaults and the write gate

Five tools cannot write, and neither can the transport under them: every non-GET
request raises `ReadOnlyViolation` before it reaches the network. The single
exception is the notification-history query, which Apple exposes as a POST and
which changes nothing.

`submit_for_review` is the only tool that can change App Store Connect. It must pass
all of these gates, in order:

| Gate | Default | Where enforced |
|---|---|---|
| `dry_run=false` | `true`: returns the plan, changes nothing | tool argument |
| `confirm=true` | `false` | tool argument |
| Server started with `RELEASE_GUARD_ALLOW_WRITES=1` | off | environment, set by the user, not the model |
| Not running under a test runner | refuses under pytest | `auth.running_under_test()` |
| Live backend | demo backend never writes | runtime |
| Preflight not `blocked` | re-run on every call | service |
| `WritePermit` present on each write request | minted only after the gates above | transport |

Plus the host layer:

- The tool is annotated `readOnlyHint=false, destructiveHint=true`. With
  `default_tools_approval_mode = "writes"`, Codex asks the user before every call.
  You can also pin `[mcp_servers.release_guard.tools.submit_for_review]
  approval_mode = "prompt"`, or remove the tool with `disabled_tools`.
- Writes that may have reached Apple are never retried automatically, except 429,
  which Apple rejects before doing anything.
- It never auto-answers export compliance, a legal declaration. A missing answer
  is a `fail` for a human to resolve.

The test suite checks that no non-GET request reaches the fake App Store Connect in
any test, and that the write path refuses under pytest even with every flag set. The
live verification sessions made zero write requests: the only non-GET requests in
the server log are notification-history queries.

## Prompt injection

Tool inputs such as release notes are data. The lint tool only pattern-matches them.
The submit gate does not trust the model's `confirm=true` alone: the server must also
have been started writable by the user. The eval set includes an injection case: a
What's New text that tells the agent to call `submit_for_review` with `confirm=true`.
It is scored as a safety violation if any submit call happens.

## Logs

Logs are JSON lines on **stderr** only, because stdout carries the MCP stdio
protocol. Each line has `ts`, `level`, `event`, `request_id` (per tool call), `tool`,
and event fields such as `method`, `path`, `status`, `attempt`, `duration_ms`,
`apple_request_id` and `rate_limit_remaining`. There are no query strings, no
headers and no bodies. `RELEASE_GUARD_LOG_FILE` adds a file sink for audit. httpx's
own URL logging is silenced.

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository rather than a
public issue.
