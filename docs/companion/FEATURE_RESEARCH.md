# Feature research: what people install usage plugins for

Researched 2026-10-23. Companion to [MARKET_NOTES.md](MARKET_NOTES.md) (competitor mechanics) and [DATA_PROVENANCE.md](DATA_PROVENANCE.md) (our source rules). This file answers: which features do developers actually install tools for, where does each number come from, and which numbers can we show honestly.

Method note: reddit.com and old.reddit.com refused every fetch, and `site:reddit.com` searches returned nothing. Community signal below comes from Hacker News (Algolia API), GitHub stars and READMEs, and vendor pages. Treat "Reddit says" claims elsewhere as unverified.

## 1. The tools and the demand signal

| Tool | Form | Stars / HN | What it is known for |
|---|---|---|---|
| [CodexBar](https://github.com/steipete/CodexBar) | macOS menu bar, many providers | 22.8k stars | Session, weekly, monthly meters per provider, reset countdowns, credits, local cost history, quota burndown with an even-use guide, status-page incidents, WidgetKit, quota notifications |
| [ccusage](https://github.com/ryoppippi/ccusage) | CLI | 19.1k stars; [HN 75 pts](https://news.ycombinator.com/item?id=44610925) | `daily`, `weekly`, `monthly`, `session`, `blocks` reports, per-model `--breakdown`, `--project`/`--instances`, `--json`, `statusline`. Prices from LiteLLM plus models.dev, `--mode auto|calculate|display` ([docs](https://ccusage.com/guide/cost-modes)). 18 local sources incl. Codex, OpenCode, Copilot CLI, Gemini CLI. `blocks --live` removed in v18, replaced by `statusline` ([docs](https://ccusage.com/guide/blocks-reports)) |
| [ccstatusline](https://github.com/sirmalloc/ccstatusline) | Claude Code statusline | 13.4k stars | Context %, session and weekly usage, per-model weekly (Sonnet/Opus/Fable), block and weekly reset timers, session cost and cost rate, cache hit rate, compaction counter, git and PR widgets |
| [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) | Terminal TUI | 8.8k stars; [Show HN 245 pts, 135 comments](https://news.ycombinator.com/item?id=44317012) | Live burn rate, "will you hit the cap before reset", plan presets, P90 custom limits, confidence labels (`official`, `local_estimate`) |
| [CodeBurn](https://github.com/AgentSeal/codeburn) | TUI + SwiftBar | [Show HN 112 pts](https://news.ycombinator.com/item?id=47759035) | Spend by task type (13 categories from tool patterns, no LLM). Commenter: "Activity classification is the actually useful feature" |
| [Usage4Claude](https://github.com/f-is-h/usage4claude), [ClaUse Bar](https://apps.apple.com/app/id6759294136), [Usagebar](https://usagebar.com/), [Claude Meter](https://github.com/puq-ai/claude-meter), [cli-usage-bar](https://github.com/svenedik/cli-usage-bar), [Clabar](https://github.com/Magir/clabar), [AgentPace](https://festudio.net/agentpace/) | macOS menu bar | small (Usage4Claude 225 stars) | All converge on the same core: 5h % and 7d % in the bar, reset countdown, threshold notifications (defaults 70/90, 75/90, 80/95) |

What HN commenters asked for (Monitor thread 44317012):
- "I wish it was just a simple command that outputs how much I have used out of my plan already." (ed_mercer)
- "Tool also says I used 198% of my max5 plan" (anotheryou): hardcoded plan limits are wrong.
- "Token usage doesn't reset after the time window unless you hit 100%." (_august): local 5h block logic diverges from the real window.
- Support for more agents (Gemini, Cursor). CodeBurn thread: per-session cost when running parallel sessions (zzy824).

Built-in alternatives users already have: Claude Code `/usage` (alias `/cost`, `/stats`) shows plan bars, session cost, and since recent versions an attribution breakdown with fix recommendations ([costs doc](https://code.claude.com/docs/en/costs)). `/context` shows what fills the window. Codex `/status` shows 5h and weekly % with reset times ([pricing doc](https://learn.chatgpt.com/docs/pricing)). These are interactive only; the gap tools fill is "always visible, across agents, with alerts".

## 2. Ranked features

Rank = how many tools ship it, weighted by stars and HN engagement.

| # | Feature | Tier | Evidence |
|---|---|---|---|
| 1 | 5-hour window % used + reset countdown | Must | Every menu bar app above; ccstatusline; Monitor; HN "how much I have used" |
| 2 | Weekly (7d) % used + reset time | Must | Same set; weekly limits are the main complaint driver ([TechCrunch](https://techcrunch.com/2025/07/28/anthropic-unveils-new-rate-limits-to-curb-claude-code-power-users/)) |
| 3 | Threshold notifications (user-set, default 75/90) and "limit hit, resets at X" | Must | Usage4Claude, ClaUse Bar, Usagebar, Claude Meter, cli-usage-bar, CodexBar |
| 4 | Context window % for the active session | Must | ccstatusline's headline widget; statusline docs lead with it |
| 5 | Active sessions list (which agent, project, running or idle) | Must | Clabar, Glint; our existing run list already does this |
| 6 | Tokens today / week / month with cost labeled "API-equivalent estimate" | Should | ccusage's core; CodexBar "local estimate, not billing data"; HN "$100 plan, $600-800 usage" |
| 7 | Per-project and per-model breakdown | Should | ccusage `--project`, `--breakdown`; CodeBurn |
| 8 | Pace: are you ahead or behind an even burn to reset | Should | CodexBar burndown, Monitor pace labels, AgentPace, Claumon |
| 9 | Per-model weekly (Opus, Sonnet) | Should | Usage4Claude, ccstatusline; "You've hit your Opus limit" is a real separate ceiling ([costs doc](https://code.claude.com/docs/en/costs)) |
| 10 | Usage credits / extra usage spend vs monthly limit | Nice | ccstatusline Extra Usage, CodexBar credits; Codex `credits` object |
| 11 | History charts (daily bars, 5h windows over a week) | Nice | Clabar, CodexBar inline charts, ccusage tables |
| 12 | Spend by task type, cache hit rate, compaction count | Nice | CodeBurn, ccstatusline |
| 13 | Multiple accounts | Nice | CodexBar token accounts; low demand signal |
| 14 | Provider status incidents | Nice | CodexBar, ccstatusline Claude Status |

## 3. Per feature: source, documented, accuracy, our honest path

Only three sources give a real "% of plan": the Claude Code statusline `rate_limits` (Pro/Max only), Codex `token_count.rate_limits` (passive, open-source schema), and Codex app-server `account/rateLimits/read` (documented). Everything else that prints a plan % from local tokens is a guess.

| Feature | Best source | Documented? | Accuracy | How we get it locally and honestly |
|---|---|---|---|---|
| Claude 5h / 7d % + reset | Statusline stdin `rate_limits.five_hour.*`, `rate_limits.seven_day.*` | Yes ([statusline](https://code.claude.com/docs/en/statusline)) | Provider-reported, exact. Absent for API-key users and before the first response; each window dropped once `resets_at` passes | Opt-in statusline bridge that wraps the user's existing command and appends the JSON to `~/.agentplay/claude-statusline.jsonl`. Without it: show nothing |
| Claude limit hit + reset time | Transcript row with `error: "rate_limit"` | No (observed locally, 138 rows on this machine) | Exact for the moment it happened | Passive, no shim. Shape confirmed from an SDK session; CLI rows assumed identical. Show "5h limit hit, resets 14:00" until `resetsAt` passes |
| Claude 5h / 7d via OAuth | `GET api.anthropic.com/api/oauth/usage` + Keychain token, `anthropic-beta: oauth-2025-04-20`; returns `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet` with `utilization`, `resets_at` | No. UsageNow calls it undocumented | Matches `/usage` but can break and needs the user's token | Do not use (DATA_PROVENANCE "Never") |
| Claude per-model weekly | OAuth endpoint only | No | n/a | Not available on the documented path. Show only the limit-hit row when "Opus limit" is hit |
| Codex 5h / weekly % + reset | `token_count.rate_limits.primary/secondary` in session JSONL | Partly (open-source `codex-rs/protocol`) | Provider-reported at that turn; stale between turns | Passive read, already shipped. `window_minutes` 300 = 5h, 10080 = weekly |
| Codex live limits | `codex app-server` `account/rateLimits/read`, notification `account/rateLimits/updated` | Yes ([app-server](https://learn.chatgpt.com/docs/app-server)) | Exact, live | Optional opt-in probe, only while Codex is in use |
| Codex via web | `chatgpt.com/backend-api/wham/usage` (CodexBar) | No | n/a | Do not use |
| Context % (Claude) | Statusline `context_window.used_percentage`, `context_window_size` | Yes | Exact; input-only formula; `null` early and after `/compact` | Bridge. Without it, show tokens only (see section 5) |
| Context % (Codex) | `token_count.info.last_token_usage.input_tokens / info.model_context_window` | Partly | Close to Codex's own indicator | Passive, already shipped |
| Tokens and cost per day / project / model | Claude: `~/.claude/projects/**/*.jsonl` `message.usage`. Codex: deltas of `info.total_token_usage` | No (file formats undocumented) | Tokens exact after dedup. Cost is API list price, not what a subscriber pays | Local scan, label every dollar "API-equivalent at list price". Use the pricing table below, not LiteLLM, so the source is citable |
| Session cost (Claude live) | Statusline `cost.total_cost_usd` | Yes | Client-side list-price estimate, resets on `/clear` | Bridge |
| 5h "blocks" from local logs | ccusage: first message starts a 5h block, UTC, new block after expiry | n/a | Heuristic. Will not align with Anthropic's window (HN `_august`) | Do not show as a limit. At most "active since 09:12" |
| Plan token limits (Pro 19k, Max5 88k, Max20 220k) | Monitor presets | No. Monitor's own README contradicts itself (Pro 19k vs "~44k") and now prefers statusline `rate_limits` | Wrong in practice ("198% of my max5 plan") | Never ship token-limit presets |
| Burn rate / projection | Derived from successive provider % readings | Derived | Good when based on provider % deltas; poor when based on token counts against guessed limits | Slope of the last N `used_percentage` readings to `resets_at`; label "projection" |
| Active sessions | `lsof` + transcript mtime | Derived | High | Already shipped |
| Team / API spend | OpenTelemetry `claude_code.cost.usage`, `claude_code.token.usage`, event `claude_code.api_request` (`cost_usd`, tokens, `request_id`) | Yes ([monitoring](https://code.claude.com/docs/en/monitoring-usage)) | List-price estimate; no rate-limit metric | Possible later: companion as a localhost OTLP receiver. Needs env vars set by the user |

## 4. Exact fields and file locations

### Claude Code

Transcripts: `~/.claude/projects/<cwd with / replaced by ->/<sessionId>.jsonl` (also `$CLAUDE_CONFIG_DIR/projects`). Verified on this machine with Claude Code 2.1.x:

```
assistant row: type="assistant", timestamp, sessionId, cwd, gitBranch, version, isSidechain, requestId,
  message.id, message.model,
  message.usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens,
                 cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens},
                 output_tokens_details.thinking_tokens, server_tool_use.{web_search_requests, web_fetch_requests},
                 service_tier, speed, inference_geo}
limit-hit row: error="rate_limit", isApiErrorMessage=true, apiErrorStatus=429, apiError="usage_limit_reached",
  quotaLimits.{status:"rejected", resetsAt:<epoch s>, rateLimitType:"five_hour"|..., overageStatus, isUsingOverage},
  message.model="<synthetic>"  (exclude from token totals)
```

Dedup is mandatory. Claude Code writes one row per content block with the same usage repeated. Newest transcript here: 522 assistant rows with usage, 197 unique `(message.id, requestId)`, up to 6 copies of one message. Naive summing overcounts about 2.6x. Keep one row per key. `costUSD` appears only in old test transcripts here, so current versions do not write it; ccusage `auto` mode falls back to calculating.

Statusline stdin JSON (documented): `session_id`, `transcript_path`, `cwd`, `model.id`, `model.display_name`, `workspace.{current_dir, project_dir}`, `version`, `cost.{total_cost_usd, total_duration_ms, total_api_duration_ms, total_lines_added, total_lines_removed}`, `context_window.{total_input_tokens, total_output_tokens, context_window_size, used_percentage, remaining_percentage, current_usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}}`, `rate_limits.five_hour.{used_percentage, resets_at}`, `rate_limits.seven_day.{used_percentage, resets_at}`, gateway only `rate_limits.spend_limit.{used_percentage, resets_at, used_usd, limit_usd, period}`, `prompt_cache.{warm, hit_ratio, expires_at, ...}`, `effort.level`. `resets_at` is Unix epoch seconds; percentages 0 to 100. Runs on each assistant message, debounced 300 ms; `refreshInterval` (seconds, min 1) adds a timer.

### Codex

Sessions: `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl`. Verified with Codex 0.125:

```
session_meta.payload.{id, cwd, originator, cli_version, source, model_provider}
turn_context.payload.{turn_id, cwd, model, effort, ...}
event_msg payload.type="token_count":
  info.total_token_usage.{input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, total_tokens}  (cumulative per session)
  info.last_token_usage.{same}                                                                                         (last turn)
  info.model_context_window                                                                                            (e.g. 258400)
  rate_limits.{limit_id, limit_name, plan_type, rate_limit_reached_type,
               primary|secondary.{used_percent, window_minutes, resets_at},
               credits.{has_credits, unlimited, balance}}
```

`info` can be `null` and `primary`/`secondary` can be `null`. Daily tokens = sum of positive deltas of `total_token_usage` per session, not a sum of totals. App-server: `codex app-server` (stdio JSON-RPC), `initialize` then `account/rateLimits/read` returns `rateLimits.{limitId, limitName, primary.{usedPercent, windowDurationMins, resetsAt}, secondary, rateLimitReachedType}`, `rateLimitResetCredits`; `account/read` returns `account.planType`.

### Others
Cursor, Copilot, OpenCode: no documented local quota source (see MARKET_NOTES). ccusage reads their local token logs; OpenCode keeps `~/.local/share/opencode/opencode.db`. Show tokens and activity only.

## 5. Context window and pricing denominators

Claude: current models (Fable 5.1, Opus 5.5, Sonnet 5.5, Haiku 5.5) have 1M context and 128K max output per the [models overview](https://platform.claude.com/docs/en/about-claude/models/overview), but the statusline doc says `context_window_size` is "200000 by default, or 1000000 for models with extended context". The session's real limit depends on plan and settings, so inferring it from the model ID is wrong. Rule: show `%` only with `context_window.context_window_size` from the statusline. Otherwise show "84k tokens in context" with no percentage.

Codex: the [Codex models page](https://learn.chatgpt.com/docs/models) publishes no context sizes. Use `info.model_context_window` from the same session's `token_count` record (258,400 seen live), which is Codex's own number.

Claude list prices per MTok ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)):

| Model | Input | 5m cache write | 1h cache write | Cache read | Output |
|---|---|---|---|---|---|
| Fable 5.1 | $10 | $12.50 | $20 | $0.25 | $50 |
| Opus 5.5 | $4 | $5 | $8 | $0.20 | $20 |
| Sonnet 5.5 | $2 | $2.50 | $4 | $0.10 | $10 |
| Haiku 5.5 (prompt up to 100k) | $0.10 | $0.125 | $0.20 | $0.01 | $0.50 |
| Haiku 5.5 (prompt over 100k) | $0.50 | $0.625 | $1 | $0.05 | $2.50 |
| Opus 4.5 to Opus 5 | $5 | $6.25 | $10 | $0.50 | $25 |
| Sonnet 4.5 / 4.6 | $3 | $3.75 | $6 | $0.30 | $15 |
| Haiku 4.5 | $1 | $1.25 | $2 | $0.10 | $5 |

Notes: 4.6 and later (except Haiku 5.5) bill the full 1M window at standard rates. US-only `inference_geo` is 1.1x. Fast mode is premium. The JSONL records `cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` separately, so cache writes can be priced exactly. Unknown model: show tokens, no cost.

Codex: subscription usage is not dollar-metered. Credits per 1M tokens (input / cached / output): GPT-6.1 Sol 50 / 2.5 / 250, GPT-6 Sol 50 / 5 / 250, GPT-6 Luna 2.5 / 0.25 / 12.5, GPT-6 Astra 250 / 25 / 1,250 ([pricing](https://learn.chatgpt.com/docs/pricing)). USD API prices for these models are not on an official page we could read; third-party aggregators list GPT-6.1 Sol at $2 / $10, unverified. Show Codex in tokens and plan %, not dollars. Pro has no 5h limit currently, so expect only a weekly window there.

## 6. What a top-tier menu shows first

```
 [bar icon]  C 42% · X 18%            two numbers, the tighter window per provider; red over the user threshold

 ┌ Claude Code ───────────────── provider-reported, 2 min ago ┐
 │ 5h   ████████░░░░░░  42%   resets 15:00 (in 1h 48m)        │
 │ Week ███░░░░░░░░░░░  21%   resets Mon 09:00                │
 │ Pace: on track to 71% at reset                             │
 ├ Codex ─────────────────────── from session log, 6 min ago ┤
 │ Week ██░░░░░░░░░░░░  18%   resets Thu 11:30                │
 ├ Running now ──────────────────────────────────────────────┤
 │ ● agentplay  Claude  context 61% of 200k   3m              │
 │ ● frontend   Codex   context 34% of 258k   idle 2m         │
 ├ Today ────────────────────────────────────────────────────┤
 │ 4.1M tokens · ~$18.40 API-equivalent · top: agentplay      │
 └ Open history · Settings ──────────────────────────────────┘
```

Order: (1) the window closest to its limit with its reset time, (2) the other window, (3) running sessions with context %, (4) today's tokens and estimated cost, (5) history behind a click. Every number carries its source and age. A missing source shows "Enable Claude limits" (the bridge), never a guessed bar.
