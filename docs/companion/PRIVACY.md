# Privacy

AgentPlay Companion has no AgentPlay account and keeps no analytics; the requests it makes are described under Network (plan limits, updates, and suggestions from Claude if you turn them on), and each can be switched off. This page says exactly what it reads, shows, stores and runs, and which test holds each line.

## Network

One request, for the plan limits, on by default and off with one switch: "Show Claude usage from my account" (the last onboarding step, where it says what it reads and whom it asks, and Settings → Live Limits). When on, it reads the login Claude Code saved in the Keychain item "Claude Code-credentials" through `/usr/bin/security` (macOS asks once), and sends one `GET https://api.anthropic.com/api/oauth/usage` with that token, every 5 minutes and when the menu opens, backing off when Anthropic asks. It keeps only the percentages and reset times it gets back; the token is never stored, logged or renewed by AgentPlay (renewing would rotate Claude Code's own login). This exists because Claude Code started from an editor's agent panel (Zed's claude-acp, other Agent SDK hosts) has no statusline and so never reports plan usage on the Mac; it is how CodexBar and the other usage menus read it. Tests: `ClaudeAccountTests`.

Switching Claude accounts (Settings → Account, and Switch Claude Account in the menu) makes no requests either. On the user's Save, it copies Claude Code's login from its Keychain item into AgentPlay's own item ("AgentPlay Claude accounts", one per account) and keeps the name, organization and last-seen usage in preferences. On Switch, it saves the outgoing login first, writes the chosen login into Claude Code's item (keeping this Mac's MCP server logins), and replaces only `oauthAccount` in `~/.claude.json`, after copying it to `~/.agentplay/backups`. All of it goes through `/usr/bin/security`, as Claude Code does, with the login on stdin, never in a process's arguments. Refused while a session is mid-step or when the saved login has expired. "Remember accounts I sign in to" is off by default. Tests: `ClaudeSwitchTests`, including an end-to-end round trip on throwaway Keychain items.

Suggestions from Claude are off by default ("Ask Claude for suggestions once a day", Settings → Privacy, and "Ask Claude for Suggestions" under Claude Suggests in the menu's Fixes tab). When on, at most once every 20 hours the app runs your own `claude` once: `claude -p --model haiku --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence`, from `~/.agentplay/coach`, so no tool can run, none of your hooks or MCP servers load, and no session is saved. It sends counts only: run health and its parts, steering rates, the titles of the fixes found, the name of a command that keeps failing, and your installed skills, subagents and MCP servers by name with their use counts the one-line `description` each skill or subagent declares, which hook script runs a skill and which of your subagents it drives (found locally by name in its SKILL.md; the text itself is never kept or sent), and the titles already on screen so Claude doesn't repeat them. Never a prompt, a message, a file path or code. It uses your Claude plan like any other Claude Code request, a few thousand Haiku tokens a day. The reply is checked before it's kept: at most three suggestions, short strings, and a skill name only if you have it installed. Tests: `test_coach_runs_isolated_once_a_day_and_sends_no_text`, `test_coach_keeps_only_checked_suggestions`.

Updates: once a day, when the release is signed with a real key, Sparkle fetches the appcast from GitHub Releases (no system profile is sent). Off with "Check for updates once a day" in Settings → General.

Otherwise the app makes no requests of its own: no telemetry SDK, no remote fonts, icons or scripts (all assets are drawn in code or bundled). Every child process gets `AGENTPLAY_NO_NETWORK=1` and `DO_NOT_TRACK=1`, whatever the parent environment says (`Redaction.childEnvironmentIsMinimal`), so the viewer it opens hides the newsletter card and can't send an email. The only things that open a web page are links the user clicks (provider usage pages, "See supported agents"), and they open in the browser.

`test_snapshot_makes_no_network_calls` builds a snapshot with `socket.connect` raising.

## What it reads

| Read | Why |
|---|---|
| `~/.agentplay/index.json` and `maps/` | Everything on Now and Learn |
| The agents' transcript folders (`~/.claude`, `~/.codex`, `~/.cursor`, `~/.copilot`, VS Code's user storage, OpenCode's data folder, `~/.kiro`, and folders the user adds) | The same incremental build `agentplay build` runs |
| Repo folders those sessions worked in | The builder checks paths and git settings to tell which repo a file belongs to. For repos under Documents, Desktop or Downloads, macOS asks once |
| Session facts and the Run health report (`facts.py`, `health.py`) | The Insights rows: a score, five parts, and fixes that name a command, tool, hook or MCP server, with counts and session ids. Computed locally from the maps, cached 15 minutes in `~/.agentplay/cache/companion-insights.json`; no titles, paths or arguments (`test_insights_report_is_cached_and_private`) |
| Claude Code transcripts, for steering | Your own messages, read only to classify: a correction (opens with "no", "wait", "stop"… or says "still not working"), frustration (swearing, or three or more ! or ?), an interruption, a refused tool call. Only `[time, kind, line id]` is kept, in the usage cache, where kind also says whether a correction said the result didn't hold and whether frustration was a word or punctuation; no text (`test_steering_signals_count_people_not_programs`). Failing hooks: the error Claude Code recorded is read to pick a fixed phrase (wrong event name, not found, timed out, exit code) and dropped |
| Claude Code transcripts, for usage | Only lines that carry `usage`: message id, request id, model, timestamp, token counts, the session id, and the folder name of `cwd`. Cached in `~/.agentplay/cache/companion-claude-usage.json` with nothing else, under a digest of the transcript's path rather than the path itself; message text is never kept (`test_usage_reads_claude_transcripts_once_per_request`) |
| The tail of Codex transcripts | Only the numeric `token_count` record: context window and plan windows |
| Claude Code's record of a failing hook (`attachment` on a system line) | The event it ran for, the one it answered as, and the command it ran, read to find the hook's entry in `~/.claude/settings.json`. The snapshot and the menu carry the command's name only; the full command stays in the process that applies the fix |
| `lsof` output for recent transcripts | Which agent processes are live |
| Claude Code's live-process records, `~/.claude/sessions/<pid>.json` | Which sessions are working, idle or waiting on you. The app re-reads them the moment the folder changes, so "needs you" shows in about a second. Only `pid`, `sessionId`, `status`, the start and status times, the folder name of `cwd`, and Claude Code's own title for the session (`name`, what `/resume` shows) are kept; the full path and the socket path are never read in (`LiveRecordsTests.parsesAndKeepsOnlyTheFolderName`). The title names the session's row in the menu, on this Mac; it is never put in a notification, the copied summary or the snapshot the core writes |
| The parent processes of a live session's `pid` | To find the terminal app a click should bring forward. Process ids only, through `sysctl` |

Not requested: Full Disk Access, Accessibility, Screen Recording, Contacts, browser data. Keychain only for the usage fetch above, for that one item, and not at all when it is off. The app is not sandboxed, so it can read the agent folders the user already owns; it uses no entitlement beyond the hardened runtime.

## What it never shows, stores or sends

Prompt text, session titles and task labels from the index (the one exception: the menu's live-session rows show Claude Code's own title for a running session, read from its live record, see above; never in notifications or anything sent), assistant or reasoning text, file contents, command arguments, tool inputs and outputs, repo remotes, PR URLs. Transcript paths are never shown or sent; they are stored only on this Mac (see "What it writes").

These are never read into the snapshot in the first place: the Python core does not pass them through (`companion.py`), and the Swift side has nothing to leak. Tests:

- `test_snapshot_never_leaks_prompt_args_paths_or_remotes`: a real-shaped Claude Code transcript seeded with secret prompt text, secret arguments, a secret environment value, secret tool output and a secret remote is built, then snapshotted; none of it appears, and the title that does sit in the index stays out.
- `test_synthetic_index_fields_never_reach_snapshot`: titles, task labels, remotes, PR URLs and transcript paths in index rows.
- `Redaction.summaryHasNoPathsOrIds`, `notificationsOmitRepoByDefault`: the copied summary and notifications.
- `test_codex_limits_fresh` asserts nothing from a Codex transcript's text reaches the snapshot.
- The existing map contract (`test_builder_never_leaks_free_text`) still passes unchanged.

What the popover does show is what the viewer shows from the same maps: agent, repo name, shell verbs (`pnpm test`, never its flags), tool names, repo-relative file paths in the map's path mode, model names, counts and times.

## Notifications

Alerts show as Pip on screen by default: AgentPlay's own banner, drawn by the app in the top-right corner, with the same title and body a notification would carry and nothing more. Nothing is sent anywhere and nothing is stored; macOS permission isn't needed. "macOS notifications" or "Both" in Settings → Notifications also posts them through Notification Center, which asks permission the first time. The optional walking Pip strip shows only how many agents are working and a session's name.


Off by default, except "Session needs you", which onboarding offers switched on once macOS has allowed notifications and applies only when the user presses Start with it still on. macOS is asked for permission from the onboarding button or when the user switches on an alert. A notification carries the agent, the command family and a count, for example "pnpm test failed 3 times in a row." "Session needs you" says "Claude Code needs you" and is skipped when that session's terminal is already the frontmost app. "Session finished" carries how long it worked, tool-call and error counts and the last step's tool or command name, from the map. Repo names, in any of them, only if the user turns that on (`needsYouNamesTheProjectOnlyWhenAllowed`, `finishedNoteNeedsRealWorkAndSaysHowItWent`). The payload holds the session id and event `seq` so a click can open the moment; nothing else. History records alert ids and times only; Settings → Notifications → Forget alert history clears it, and the watermark moves forward so nothing old fires afterwards.

## What it writes

Turning things off from Tools (only after you confirm, with the exact change shown): a skill gets `skillOverrides: {"name": "off"}` and an MCP server or claude.ai connector a `deniedMcpServers` entry, both in `~/.claude/settings.json` and both Claude Code's own switches. A subagent's file moves to `~/.agentplay/set-aside/agents`, and a hook's entry moves from `settings.json` to `~/.agentplay/set-aside/hooks.json`, because Claude Code has no switch for either. Every write takes AgentPlay's write lock and copies `settings.json` to `~/.agentplay/backups` first; Turn On puts back exactly what was there. Plugin skills are not switched (Claude Code switches plugins as a whole). Tests: `test_setup_switches_use_claude_codes_own_keys_and_undo_exactly`.

- Its preferences, in the `ai.entelligence.agentplay.companion` domain: settings, mutes, pins, dismissed Learn cards, alert ids and times, custom folders.
- `~/Library/Caches/ai.entelligence.agentplay.companion/viewer.pid` while its viewer runs.
- The same maps and index in `~/.agentplay` that `agentplay build` writes. The index records each session's transcript path (`sourcePath`), and the usage cache in `~/.agentplay/cache` is keyed by transcript path. Both stay on this Mac; neither is shown or sent.

Nothing is written inside the app bundle (Python bytecode is precompiled at build time and `PYTHONDONTWRITEBYTECODE=1` is set). Uninstalling leaves `~/.agentplay` alone unless asked (`scripts/companion-uninstall.sh --remove-maps`).

## The local viewer

The viewer the companion opens is the stock `agentplay view`, on 127.0.0.1 and a free port, with its existing guards: loopback peer, local `Host`, own `Origin`, `Sec-Fetch-Site`. The self-test checks two of them against the running bundle: a foreign `Host` (DNS rebinding) and a foreign `Origin` both get 403. The companion adds no HTTP endpoint, socket or IPC listener; it talks to the core only through a child process's stdout. A copied replay link is a 127.0.0.1 link and the app says it only works on this Mac.

## Process hygiene

The core runs as `python -I` (no `PYTHON*` variables, no user site-packages) with a minimal environment: only `HOME`, a fixed `PATH`, locale, and the user's `AGENTPLAY_*` settings; nothing else from the parent, such as cloud credentials, is passed. On quit or SIGTERM the app stops its snapshot child and its viewer. A viewer left by a crash is stopped on next launch only if its command is this app's own Python, so a viewer the user started with `uvx agentplay` is never touched (`ViewerOwnership` tests, self-test `leftoverReaped`).
