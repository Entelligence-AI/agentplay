# Data provenance

Every number and state the companion shows is a `DataPoint` (schema: `schema/agentplay-companion-v1.schema.json`, Python: `companion.point`, Swift: `AgentPlayCore/Snapshot.swift`):

```
value        T | null                    null whenever kind is unavailable
kind         recorded | derived | estimated | provider_reported | unavailable
source       agentplay_map | local_index | approved_provider_cli | official_provider_api | none
observedAt   ISO time | null
window       context | five_hour | weekly | session | project | none
confidence   high | medium | low
explanation  plain sentence, always present
freshness    fresh | stale | unknown
```

Readers degrade, never upgrade: an unknown `kind` decodes as `unavailable`, an unknown `confidence` as `low`, and a context `percent` without a `limit` is dropped (`ContextValue.init`). Tests: `test_every_datapoint_is_honest`, `SnapshotDecoding.unknownEnumValuesDegradeToLeastAuthoritative`, `percentNeverWithoutLimit`.

## Source by source

| What | Source | Kind | Notes |
|---|---|---|---|
| Runs, order | `~/.agentplay/index.json` rows, root sessions with calls (same filter as `/api/sessions`) | recorded | Ordered by latest of end time, start time and live transcript mtime. Pinned runs first |
| Running | `lsof`: a process holds the transcript open, or a `claude` process's cwd encodes to the transcript's project folder | derived, high | Never from mtime alone |
| Recent activity | live `stat` of the transcript, under 10 minutes | derived, medium (low for shared-database agents) | "Activity recorded", not "running" |
| Last events, micro timeline | map `events`: kind, shell verb, tool, repo-relative target, `err`, `errClass`, `retryOf` | recorded | Ordered by `seq`; wall clock is `startedAt + t` |
| Map lag | transcript mtime minus the row's `updatedAt` | derived | `mapBehindSeconds`; above 10 s a trailing no-result call is not "running" |
| Tool still running | last call has `errClass: no_result`, run is active, map is current | derived | Elapsed = now − call start |
| Repeated failure | 3 failures of one command family and error class in one lane, no success in between, within 30 min | recorded evidence | `companion.repeated_failures`; resolved when the family later passes |
| Run ended with errors | `session.outcome == unresolved_errors` and the run is idle | recorded | |
| Learn card | failing `exec`/`verify` events by verb across the newest 40 root sessions of 14 days; at least 4 failures in at least 2 runs; ranked by runs, then failures | derived, `project` window | States the denominator and how many runs passed later |
| Context (Claude Code and others) | latest main-lane `llm` event: `in + cw + cr`; after a later compaction, its `postTokens` | recorded, medium | Tokens only: no limit is recorded anywhere, so no percentage. Subagent lanes ignored |
| Context (Codex) | last `token_count` record in the Codex transcript: `info.last_token_usage.input_tokens` over `info.model_context_window` | provider_reported | Codex's own indicator may differ slightly |
| Plan (Codex) | same record: `rate_limits.primary` / `secondary` (`used_percent`, `window_minutes` 300 → five_hour, 10080 → weekly, `resets_at`), `plan_type`, `limit_id` | provider_reported | Stale once `resets_at` has passed; low confidence when the two newest reports name different plans |
| Plan (Claude Code) | none | unavailable | See below |
| Plan (Cursor, Copilot, OpenCode, Kiro) | none | not shown | No documented local source |
| Usage (tokens, cost) | Claude Code: its raw transcripts, main and subagent files, one request per message id and request id, the finished line's token counts and the first line's time. Other agents: `llm` events in maps. A map request that repeats a counted one (same model and tokens within 5 s) is dropped | estimated | Costs at list prices; unpriced models are counted as unpriced, never $0. Raw transcripts, not maps, for Claude: a map can lag a live transcript, and the build skips sessions over 512 MB, which froze the biggest session's totals. Checked against an independent count of the transcripts: within 0.6% for today, 7 and 30 days (`python/src/agentplay/usage.py`) |

## Provider adapters

Each provider row has the same shape (`ProviderLimitWindow`): provider, window, plan, reset, usage URL, and the `used` DataPoint. Capabilities in v1:

| Provider | fetchContext | fetchPlanUsage | Strategy | Privacy requirement |
|---|---|---|---|---|
| Codex | yes | yes | Read the numeric `token_count` record Codex writes into its own session file | none beyond the transcript AgentPlay already reads |
| Claude Code | tokens only | no | none in v1 | would need a user-installed statusline hook |
| Others | per agent, tokens only where recorded | no | none | |

### Why these choices

- **Codex `token_count`**: the fields are defined in the open-source `codex-rs/protocol/src/protocol.rs`, not on the docs site, so the format can change. It is read passively from a file the user's own Codex wrote, with no new permission, no network and no paid turn. When the format changes, the parser returns unavailable (`test_codex_limits_missing_file_and_garbage`).
- **Codex app-server** (`codex app-server`, JSON-RPC `account/rateLimits/read`, documented): the right live source for a future `approved_provider_cli` adapter. Not in v1 because it means spawning the user's Codex binary with their auth on a timer. It should be opt-in per provider, with a refresh policy and the last error shown.
- **Claude Code statusline `rate_limits`** (`five_hour.used_percentage`, `five_hour.resets_at`, `seven_day.*`; documented at https://code.claude.com/docs/en/statusline; Pro and Max only, after the first response): documented, but reaching it means adding a statusline command to the user's Claude settings. The brief rules out installing hooks automatically. A future opt-in "Install statusline bridge" would show the exact diff, write one JSON line per update to `~/.agentplay/claude-statusline.jsonl`, and come with an uninstall.
- **Never**: undocumented usage endpoints, browser cookies, Keychain OAuth tokens, screen scraping, or estimating allowance from local tokens. See MARKET_NOTES.md for which competitors use which.

## Fixtures

Seven synthetic snapshots, all produced by the real `companion.snapshot` over synthetic maps (`scripts/companion_fixtures.py`, deterministic; `scripts/companion-test.sh` and the `companion` CI job fail if they drift):

| Fixture | Covers |
|---|---|
| `two-active` | two running runs, repeated failure, Codex fresh 5-hour and weekly windows, Codex context with a window, Claude tokens without one, a Learn card |
| `no-agents` | empty store, every source not found |
| `detected-idle` | one verified run from yesterday, nothing active |
| `stale-quota` | Codex 5-hour window reset since its last report |
| `conflicting-plans` | two Codex reports naming different plans |
| `partial-unavailable` | Cursor with no tokens, an imported map with no transcript, an unreadable map, a source that can't be read |
| `many-sessions` | 14 runs across agents in two repos |

## Provider adapter test matrix

| Case | Where |
|---|---|
| Wrong user / account mismatch | `test_codex_limits_conflicting_plans`, `conflictingPlansAreLowConfidence`, `allowanceNeedsFreshProviderData` |
| Expired snapshot | `test_codex_limits_stale_after_reset`, `staleQuotaIsStale`, `allowanceNeedsFreshProviderData` |
| No network | `test_snapshot_makes_no_network_calls` (socket connect raises) |
| Contradictory figures | `conflicting-plans` fixture; newest report wins, confidence low |
| File access revoked | `test_codex_limits_missing_file_and_garbage`; `partial-unavailable` source error |
| Reset time zone changes | times are UTC on the wire and formatted in the current zone at render; `quietHoursFollowTheTimeZone` |
| Plan with no windows | `test_codex_limits_without_windows_are_unavailable` (seen on this Mac's real data) |
