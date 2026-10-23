# AgentPlay Companion: market and platform notes

Checked: 2026-10-23. Every claim below links to the primary page it came from. Anything marked "Not verified" could not be confirmed from a primary source on that date.

## Summary

- The menu-bar quota meter is crowded. CodexBar (MIT, 109 providers, 22.8k stars), UsageNow (MIT, about 12 providers) and Bloom (free, open source, meters since 0.4.0 in Aug 2026) all show 5-hour and weekly windows with reset countdowns. Matching them on quota earns AgentPlay nothing.
- None of them link an alert to the moment in the transcript that caused it. They show a number, not a moment. That gap is the Companion thesis.
- Both vendors document a structured, read-only way to read plan usage. Claude Code has statusline `rate_limits` (documented) and the Agent SDK `usage_report` (documented, marked experimental). Codex has `account/rateLimits/read` over `codex app-server` (documented) and `rate_limits` on `token_count` events in session JSONL (in the open source code, not on the docs site).
- Competitors also use routes nobody documents: OAuth usage endpoints (`api.anthropic.com`, `chatgpt.com/backend-api/wham/usage`), browser cookies, Keychain reads. We should not.

## Comparative matrix

| Product | What it shows | What we should not copy | Unique AgentPlay action | Data feasibility for us |
| - | - | - | - | - |
| Bloom | A parallel agent workspace (worktrees, diffs, PRs). Menu bar shows Claude Code and Codex allowance meters "coloured by pace", session and weekly limits, Codex credits and resets, waiting and finished counts | Becoming a workspace or agent runner. Polling CLIs every minute | Jump from "limit hit" to the turn that burned the window | Same CLIs it polls are open to us. Its method is not documented |
| CodexBar | Session, weekly and monthly windows, credits, spend estimates from local logs, provider status badges, widgets, a CLI | Cookie import, Full Disk Access for Safari, Keychain decryption, 109-provider sprawl | Open the replay at the exact event behind a spike or a status incident | Its documented paths (Codex app-server, local JSONL) are usable. Its cookie and OAuth paths are not, for us |
| UsageNow | Remaining per window, reset time, today's tokens on this Mac, widgets | The undocumented `api.anthropic.com` usage endpoint via a Keychain token (it is off by default even there) | Alert to the recorded moment, not a meter | Its "limits from session transcripts" idea fits us directly |

## Per-product notes

### Bloom (Spatie)
- Site: https://runbloom.app/ . Free and open source, no paid tier planned. Runs Claude Code and Codex through "the CLI and login already on your Mac". Cursor and OpenCode are listed as "Not yet". Needs macOS 26 or later. Download page lists 1.39.5.
- Changelog: https://runbloom.app/changelog
  - 0.4.0 (23 Aug 2026): "The menu bar now shows each agent's allowance" (window nearest expiry, usage so far, when it lifts).
  - 0.5.0 (24 Aug 2026): "The menu bar asks both CLIs what is left of your allowance".
  - 0.7.0 (24 Aug 2026): ships as a disk image.
  - 1.13.0 (12 Sep 2026): meters "coloured by pace rather than by level", up to two figures per provider in the bar, Codex Spark limits and credits. "Bloom asks both CLIs once a minute, and neither ask costs a turn or a penny."
  - 1.16.0 (14 Sep 2026): waiting and finished counts can be switched off separately.
  - 1.36.0 (24 Sep 2026): when a chat hits its usage limit, Bloom offers to continue it after the reset.
- Correction to the brief: the menu-bar allowance meters first shipped in August 2026 (0.4.0). The September releases refined them.
- Not verified: which CLI calls Bloom makes (likely Codex app-server and Claude's `/usage`, but I did not read its source). Homebrew or App Store availability is not mentioned anywhere.
- Screenshots (reference only): https://runbloom.app/screenshots/overview-1240.webp , https://runbloom.app/screenshots/review-diff-1268.webp , https://runbloom.app/screenshots/panes-stacked-1240.webp

### CodexBar (Peter Steinberger)
- Site: https://codexbar.app/ . Repo: https://github.com/steipete/CodexBar . License: MIT, "Copyright (c) 2026 Peter Steinberger" (https://github.com/steipete/CodexBar/blob/main/LICENSE).
- Install: GitHub Releases (Sparkle updates) or `brew install --cask steipete/tap/codexbar`. macOS 14+. Ships a `codexbar` CLI and a Linux Qt app. No Dock icon.
- Permissions it may ask for: Full Disk Access (Safari cookies), Keychain (cookie decryption, OAuth), Files and Folders.
- Provider docs: https://github.com/steipete/CodexBar/blob/main/docs/providers.md . 109 provider IDs, five mechanisms: local files, CLI/RPC, OAuth, browser cookies or web sessions, API keys.
  - Codex: OAuth first (`GET https://chatgpt.com/backend-api/wham/usage`), then `codex -s read-only -a never app-server` JSON-RPC `account/rateLimits/read`, then optional hidden WebView on chatgpt.com. Cost history from `~/.codex` session JSONL. (docs/codex.md)
  - Claude: OAuth API first, browser cookies, then a `claude` CLI PTY fallback. Cost history from local project JSONL. (docs/claude.md)
  - Cursor: web API via cursor.com cookies. Copilot: GitHub device flow then the `copilot_internal` API. Kiro: `kiro-cli chat --no-interactive "/usage"` with ANSI parsing. OpenCode: web dashboard cookies, OpenCode Go reads `~/.local/share/opencode/opencode.db`.
- Provenance architecture: each provider has an ordered strategy chain ("Auto" tries OAuth, then CLI, then web). Each reading carries a source label (`oauth`, `cli`, `local`, `web`, `api`, `openai-web`). Network errors keep the last good reading for the same account; auth failures do not. Spend is labeled "a local estimate built from scanned logs and list prices, not billing data". Config in `~/.config/codexbar/config.json`.
- Worth borrowing: the per-reading source label and the "estimate, not billing" honesty. Not worth borrowing: everything cookie and Keychain shaped.
- Screenshots (reference only): https://codexbar.app/features/feature-resets.webp , https://codexbar.app/features/feature-credits.webp , https://codexbar.app/features/feature-status.webp

### UsageNow
- Docs: https://docs.usagenow.com/ . Repo: https://github.com/usagenow/usagenow (MIT per docs). Native macOS 15+ menu bar app plus widgets.
- Providers: Codex, Claude Code, Gemini CLI, Antigravity, Kiro, Warp, OpenCode, Qoder, Qwen Code, Cline, Grok Build, plus API accounts (and more pages under /providers).
- Install (https://docs.usagenow.com/installation): DMG from usagenow.com or GitHub Releases, Developer ID signed and notarized. Homebrew planned. Build from source in Xcode 26+.
- Claude Code data (https://docs.usagenow.com/providers/claude-code): local activity from `~/.claude/projects` (only timestamps, ids, model names, token counts). Plan from `~/.claude.json`. Optional, off by default, "experimental" limits fetch reads Claude Code's Keychain item via `/usr/bin/security` and calls "an undocumented Anthropic endpoint that may stop working without notice". Also reads limit hits and reset times that Claude Code records in session transcripts.
- Not verified: pricing (not stated) and the Codex provider mechanism (page not read).

## Apple platform constraints

- MenuBarExtra (https://developer.apple.com/documentation/swiftui/menubarextra): macOS 13.0+. "An app that only shows in the menu bar will be automatically terminated if the user removes the extra from the menu bar." Menu-bar-only apps usually hide from the Dock and app switcher with the `LSUIElement` Info.plist flag. Use the `.window` style (`menuBarExtraStyle`) for data-rich popovers. The `isInserted:` binding lets users hide the icon.
- SMAppService (https://developer.apple.com/documentation/servicemanagement/smappservice): macOS 13.0+. Registers LoginItems, LaunchAgents and LaunchDaemons inside the app bundle. Replaces `SMLoginItemSetEnabled` and hand-installed plists in `~/Library/LaunchAgents`. Use `SMAppService.mainApp` for "Open at login", off by default, user toggled.
- UNUserNotificationCenter.requestAuthorization (https://developer.apple.com/documentation/usernotifications/unusernotificationcenter/requestauthorization(options:completionhandler:)): request only the options you use. The system prompts once, and later calls don't prompt again. Ask "in a context that helps people understand why". So ask on the first alert-worthy event or in onboarding, not at launch.
- Notarization (https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution): Developer ID certificate, Hardened Runtime, secure timestamp, no `get-task-allow`, `notarytool` (altool not accepted since 1 Nov 2023), staple the ticket. Disk images (UDIF) can be notarized directly. Not needed for Mac App Store builds.
- Implication: target macOS 14+ (same as CodexBar). That gets MenuBarExtra and SMAppService. Ship a notarized DMG plus a Homebrew cask. No Full Disk Access: reading `~/.claude` and `~/.codex` doesn't need it for a non-sandboxed Developer ID app. Not verified: whether App Sandbox would block those paths without user-granted bookmarks. Assume it would, and skip the App Store for v1.

## Provider-quota source findings

| Provider | Sanctioned source | Fields | Documented? | Our decision |
| - | - | - | - | - |
| Claude Code (context) | Statusline stdin JSON (https://code.claude.com/docs/en/statusline) | `context_window.used_percentage`, `context_window.remaining_percentage`, `context_window.context_window_size`, `context_window.total_input_tokens`, `context_window.total_output_tokens`, `context_window.current_usage`, `exceeds_200k_tokens`, `transcript_path`, `session_id` | Yes. Percentages can be `null` early in a session. `current_usage` is `null` before the first call and after `/compact`. `used_percentage` counts input tokens only | Use, through an opt-in statusline shim that tees the JSON to a local file, keyed by `session_id` and `transcript_path` |
| Claude Code (plan) | Same statusline JSON | `rate_limits.five_hour.used_percentage`, `rate_limits.five_hour.resets_at`, `rate_limits.seven_day.used_percentage`, `rate_limits.seven_day.resets_at` (Unix epoch seconds, 0 to 100). Gateway only: `rate_limits.spend_limit.{used_percentage,resets_at,used_usd,limit_usd,period}` | Yes. Present "only for claude.ai Pro and Max subscribers" (or behind a spend-limit gateway), only after the first API response. Each window can be absent, and a window is dropped once its `resets_at` passes | Use as the primary Claude plan signal. Show nothing when it's absent, never a guess |
| Claude Code (plan, alt) | Agent SDK `usage_report` on the `/usage` reply (https://code.claude.com/docs/en/agent-sdk/typescript) | `rate_limits.limits[].{kind,group,percent,resets_at,severity,is_active,scope}`, `rate_limits.extra_usage.{is_enabled,monthly_limit,used_credits,utilization}` | Yes, but "experimental: its shape may change". Needs SDK v0.3.273+, a claude.ai credential, and the `user:profile` scope | Defer. Running a headless session to poll is heavier than we want |
| Claude Code (event) | Agent SDK `SDKRateLimitEvent` | `type: "rate_limit_event"`, `rate_limit_info.{status: allowed / allowed_warning / rejected, resetsAt, utilization, errorCode}` | Yes | Only works inside SDK sessions. Not a Companion source |
| Claude Code (CLI) | `/usage` (`/cost` and `/stats` are aliases), interactive only (https://code.claude.com/docs/en/commands) | Text UI | No JSON CLI output is documented | Don't scrape it |
| Codex (plan) | `codex app-server` JSON-RPC `account/rateLimits/read` and notification `account/rateLimits/updated` (https://learn.chatgpt.com/docs/app-server) | `rateLimits.{limitId,limitName,primary,secondary,rateLimitReachedType}`, windows `{usedPercent,windowDurationMins,resetsAt}`, `rateLimitsByLimitId`, `rateLimitResetCredits` | Yes, with no experimental label | Optional active probe, at most every few minutes and only while Codex is in use |
| Codex (plan, passive) | `token_count` events in `~/.codex/sessions/YYYY/MM/DD/*.jsonl` (`event_msg` with payload type `token_count`) | `rate_limits.primary` and `rate_limits.secondary` with `{used_percent, window_minutes, resets_at}`, plus `credits.{has_credits,unlimited,balance}`, `plan_type`, `rate_limit_reached_type`, and `info.model_context_window`, `info.total_token_usage`, `info.last_token_usage` | Partly. Defined in open source `codex-rs/protocol/src/protocol.rs` (https://github.com/openai/codex), not on the docs site. The on-disk format can change | Primary Codex source. It's passive, sits in files AgentPlay already reads, and ties each reading to a moment in the transcript |
| Codex (human) | Usage dashboard chatgpt.com/codex/settings/usage and `/status` in the CLI (https://learn.chatgpt.com/docs/pricing) | Human UI | Not structured | Link to it, don't read it |
| Codex (OAuth web) | `chatgpt.com/backend-api/wham/usage` (used by CodexBar) | Unknown | No | Don't use |
| Claude (OAuth web) | `api.anthropic.com` usage endpoint via Keychain token (UsageNow, CodexBar) | Unknown | No. UsageNow calls it "undocumented" | Don't use |
| Cursor, Copilot, OpenCode, Kiro | CodexBar uses cookies, `copilot_internal`, `kiro-cli ... "/usage"` ANSI parsing, and an OpenCode web dashboard | Various | No sanctioned structured source found | No quota in v1. Show transcript events only |

Not verified: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan returned HTTP 403. The Codex limit details above come from learn.chatgpt.com/docs/pricing (the developers.openai.com docs now redirect there). https://support.claude.com/en/articles/14552983-models-usage-and-limits-in-claude-code covers `/cost` for API users and the Enterprise pool ("limit reached, resets at time"). It doesn't mention 5-hour or weekly windows or any structured field.

## Claude Code plugins (future `/agentplay`)

- https://claude.com/blog/claude-code-plugins (9 Oct 2025): plugins bundle slash commands, subagents, MCP servers and hooks. Install with `/plugin`. Marketplaces are git repos with `.claude-plugin/marketplace.json`, added via `/plugin marketplace add owner/repo`.
- Fit: one plugin could ship the `/agentplay` command (open this session in a replay), a hook that drops bookmarks, and the statusline tee. Users would opt in once instead of editing settings.json by hand.

## Implications for Companion v1

1. Don't build a meter. Show a quiet icon with one state, and alert only on events: a limit reached, a window passing a user-set threshold, context nearing full, a session that ended with an error. Every alert opens the replay at that event.
2. Codex: read `rate_limits` from the `token_count` events already in session JSONL. Each reading has a timestamp and a position in the transcript, so "you crossed 80% weekly" can jump to the turn that crossed it. Nobody else does this.
3. Claude Code: offer an opt-in statusline shim that appends `{session_id, transcript_path, context_window.*, rate_limits.*}` with a timestamp to a local log. It wraps the user's existing statusline command and doesn't replace it. Without the shim, show no Claude quota.
4. Label every number with its source and age (borrowed from CodexBar's provenance labels), for example "from Codex session log, 3 min ago". Show nothing when a reading is missing or older than its `resets_at`.
5. Hard nos: no browser cookies, no Keychain reads, no undocumented endpoints, no Full Disk Access, no network calls by default. The only active probe is the optional `codex app-server` call.
6. Platform: macOS 14+, MenuBarExtra `.window` style, `LSUIElement`, `SMAppService.mainApp` off by default, ask for notifications on the first alert, notarized DMG plus Homebrew cask.
7. Other providers (Cursor, Copilot, OpenCode, Kiro): no quota. Show transcript events only until a vendor documents a source.
