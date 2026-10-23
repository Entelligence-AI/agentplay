# AgentPlay Companion: repository audit

Audited 2026-10-23 against `main` at `08a2021` (Merge PR #17). Branch for this work: `feat/companion-macos`.
Everything below was read from source on that commit unless it says otherwise.

## 1. Brief assumptions, rechecked

| Brief says | Verified? | Notes |
|---|---|---|
| `uvx agentplay` is a local viewer over saved transcripts for Claude Code, Codex, Cursor, Copilot CLI/VS Code, OpenCode, Kiro | Yes | `discover.AGENTS`, README. Open PRs #34 (Windsurf), #35 (Gemini CLI), #36 (Antigravity) add more, none merged. |
| Store under `~/.agentplay/`, viewer on 127.0.0.1, port 7317, incremental builds | Yes | `store.py:28` (`AGENTPLAY_HOME` overrides), `cli.py:352` (`--port 7317`), `server.py:509` falls back to a free port when busy. Incremental by source mtime (`store.is_fresh`). |
| `agents, build, list, view, export, import` exist | Yes | `cli.py:337-379`. No `companion` command yet. |
| Deep links use URL state including session/event scope | Yes | See section 4. |
| Cursor does not reliably provide billed tokens; costs are estimates | Yes | `totals.costUsd` is "estimated cost at list prices. Advisory only" (`docs/format.md`). |
| Titles can include first-prompt excerpts; exports omit prompt text | Yes | `sessionTitle`/`taskTitles` live in the index only (`pipeline.py:42-71`); `_public_row` strips `taskTitles`; `_with_task_titles` merges only for `_from_this_machine()`. |
| `account.py` is newsletter signup only | Yes | Writes `~/.agentplay/account.json`; network call only after explicit submit. Not authentication. |
| PR #19 is open, experimental | Yes | State OPEN, MERGEABLE, last updated 2026-10-23T15:42Z. See section 6. |
| Issues #13, #15, #29, #31 | All OPEN | #13 keyboard help overlay, #15 layout below 1100 px, #29 unpriced models show $0, #31 copy-link for current moment. Last updated 2026-10-22/23. |

Differences from the brief worth stating:

- **No Xcode on the build machine.** Only Command Line Tools with Swift 6.4 (`swift --version`). The app is built with SwiftPM and assembled into an `.app` by script. `xcodebuild`, asset catalogs and `notarytool` via Xcode are unavailable.
- **Zero code-signing identities** (`security find-identity -v -p codesigning` → 0). Developer ID signing and notarization are BLOCKED here. Builds are ad-hoc signed.
- **No `~/.agentplay` store exists** on this machine yet. Real-data validation used `AGENTPLAY_HOME=/tmp/...` built from real local transcripts so the audit did not create the user's store as a side effect.

## 2. Capability matrix (main)

| Capability | Owner (path) | Interface | Companion use |
|---|---|---|---|
| Find agent homes | `discover.py` `Homes.default()`, `present()` | Python | Provider readiness ("Claude Code: found at …") |
| Discover + build maps | `pipeline.build_all()` | Python, `agentplay build --json` | Background incremental scan |
| Map store, index | `store.Store` (`index`, `rows()`, `get()`, `reload_if_changed()`) | Python | Read-only snapshot source |
| Path safety on map reads | `store._KEY_RE`, `Store.get` resolve + `is_relative_to` | Python | Reused as is, no new file reads |
| Map format | `docs/format.md`, `schema/agentplay-v1.schema.json`, `builder.SCHEMA` | JSON | Events, errors, retries, context, compaction |
| Viewer server | `server.serve()`, `cli.cmd_view` | HTTP GET JSON, one POST | Opened on demand for replay |
| Server guards | `_from_this_machine`, `_allowed_origin`, `_local_origin`, `Sec-Fetch-Site`, CSP | HTTP | Unchanged. Companion adds no HTTP surface |
| Prices | `_core/pricing.py` | Python | Not shown in v1 popover (issue #29) |
| Newsletter signup | `account.py` | `GET/POST /api/account` | Optional newsletter row in Settings, clearly named |

## 3. Data the companion leans on (index row and map)

Index row (`pipeline._row`, `builder.summary`): `sessionId, startedAt, endedAt, durationMs, agentTool, models[], repos[] (names), calls, requests, errors, errorsBySource, retries, delays, maxContext, compactions, role, parentSessionId, outputs, prs[], contentHash, updatedAt, sourceMtime, sourcePath, builtWith, pathMode, sessionTitle (prompt text), taskTitles (prompt text)`.

Fields the companion must never emit: `sessionTitle`, `taskTitles`, `sourcePath`, `prs[].url` (can name private repos), repo remotes. `sourcePath` is only stat'ed inside Python to derive activity.

Map events used:

- `exec` / `verify` with `verb` (program plus subcommand only, per redaction rule 2), `err`, `errClass`, `exitCode`, `retryOf`, `attempt`.
- `edit` / `read` with `targets[]` (file ids into `files[]`, repo-relative or hashed per `pathMode`).
- `llm` with `in`, `cw`, `cr` per request. `in + cw + cr` of the latest main-lane request is the recorded prompt size of that request, the best local proxy for current context occupancy. No model context limit is recorded anywhere, so a percentage has no denominator.
- `compaction` with `trigger`, `preTokens`, `postTokens`.
- `error` events with `errClass` (`usage_limit`, `rate_limit`, `spend_limit`, …). A `usage_limit` error is the only plan-limit evidence in local data, and it says "a limit was hit", never "how much is left".
- `session.outcome` (`verified`, `unresolved_errors`, `interrupted`, …).
- `t` is ms since `session.startedAt`, so wall clock of an event is `startedAt + t`.

Data gaps:

- **Running vs recent.** No index field says a session is running. `sourceMtime` is build-time mtime. The companion stats `sourcePath` live and uses `lsof` on recent transcripts for process evidence. Database-backed agents (Cursor, OpenCode, Kiro) share one DB, so per-session "active" cannot be proven; they get "activity recorded recently" at most.
- **Plan allowance.** No parser reads provider rate-limit payloads (`grep token_count|rate_limits` finds only span token attrs). See `DATA_PROVENANCE.md`.
- **Context denominator.** Not recorded. Percent stays unavailable.
- **Prices.** Several models unpriced (#29). Companion does not show cost in v1.
- **Map lag.** A map reflects the last build. The companion triggers an incremental build (debounced) when watched transcript dirs change.

## 4. Viewer deep-link semantics (nuqs URL state)

| Param | Where | Meaning |
|---|---|---|
| `session` | `SessionExplorer.tsx:137` | Selected session id |
| `at` | `SessionExplorer.tsx:520` `useSeekParam` | Seek to event whose whole-session seq (`srcSeqOf`) equals `at`; else clamped index. Param is dropped after seeking |
| `task`, `model`, `sub`, `ss` | `core/sessionScope.ts:279` | Scope filters. `ss` owns the scope to one session |
| `stab` | sidebar tab (`errors`, …) | |
| `panel`, `view`, `win`, `sq`, `theme`, `demo`, `sample` | `App.tsx`, `PlatformBanner.tsx` | Misc view state |

The main lens returns the full document (`sessionMapModel.ts:1191`), so `/?session=<id>&at=<seq>` lands on subagent-lane events too; no `sub` needed. Session ids look like `local:claude-code:<uuid>` and are percent-encoded by the companion (`URLComponents`), tested in `DeepLinkTests`. Error jump: `&stab=errors` is added for error moments.

## 5. Security posture inherited

- Server binds 127.0.0.1. Every GET requires loopback peer and a local `Host`; `Origin`, when present, must be the server's own; `/api/*` refuses foreign `Sec-Fetch-Site`. The only POST is `/api/account`.
- The companion adds **no HTTP endpoint and no IPC listener**. It runs `python -I -m agentplay companion snapshot --json` as a child process and reads stdout. Nothing can call into it from a browser.
- The viewer it opens is the same `agentplay view --no-open` server with the same guards. The companion tracks the PID it spawned and only stops that one.

## 6. PR #19 (`claude/agentplay-insights-dashboard-search-ddo14k`)

Open, 36 files, +6267/−36, 10 commits. `git merge-tree` against main is clean; overlapping files are viewer only (`App.tsx`, `SessionExplorer.tsx`, `SessionList.tsx`).

| Module | What it is | Writes | Companion stance |
|---|---|---|---|
| `facts.py` (v4) | Per-session aggregates stamped on index rows: `cmd{label:{calls,fails,retries,byClass}}`, `errorsByClass`, compaction, waste estimates | `index.json` rows via pipeline | Compatible shape; companion computes the same `verb` family itself so it works on main |
| `search.py` (v2) | Per-event postings incl. `cmd:<label>` and `err:<class>` with seqs | `~/.agentplay/search.json` | Not used. Companion reads seqs straight from maps |
| `health.py` | Run Health 0-100: 5 components × 20, `MIN_SESSIONS = 10`, components with no denominator count as full marks | none | Not surfaced in v1. "Full marks with no denominator" would overstate quality in a menu bar |
| `fixes.py` | Preview/apply/undo into `CLAUDE.md`, `CLAUDE.local.md`, `.claude/settings*.json` behind repo grant, hash check, write lock, journal | repo files, `grants.json`, `applied.json` | Never called. Learn card links to the viewer only |
| `autotune.py` | Daily auto-apply of `claude_md` fixes; installs a SessionStart hook in `~/.claude/settings.json` | user config | Never called. Conflicts with "no auto-mutation of agent config" |
| server diff | `/api/stats`, `/api/health`, `/api/fix-preview`, `/api/autotune` GET; `/api/fix`, `/api/repos`, `/api/autotune` POST; no token | | Companion does not call these |

Issues found in PR #19 while auditing (reported, not fixed here): `cli fix apply` never previews and passes no `expect_hash`; `--variant` is not bounds-checked; `autotune.install_hook` writes `~/.claude/settings.json` without the write lock and without preview.

Dependency decision: the companion ships against main. When PR #19 lands, the Learn card can read `row.facts.cmd` instead of scanning maps (same `verb` labels, see `companion.py: command_family`). No PR #19 code is copied, merged or mutated.

## 7. Change footprint of this work

- New: `python/src/agentplay/companion.py` (read-only snapshot), `python/tests/test_companion.py`.
- Modified: `cli.py` (a handful of lines to register `companion snapshot`), chosen to stay small because PR #19 and #32 also touch `cli.py`.
- New: `apps/macos/AgentPlayCompanion/` (SwiftPM), `scripts/companion-*.sh`, `docs/companion/*`.
- Windows/Linux: the Python change is pure stdlib (`lsof` is optional and only attempted on macOS/Linux when present); CLI and viewer behavior unchanged.
