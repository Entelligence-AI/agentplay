# Privacy

AgentPlay Companion is local only. It has no account, sends nothing anywhere, and keeps no analytics. This page says exactly what it reads, shows, stores and runs, and which test holds each line.

## Network

None by default. One opt-in exception, Settings → Live Limits → "Show Claude usage from my account", off until the user turns it on: it reads the login Claude Code saved in the Keychain item "Claude Code-credentials" through `/usr/bin/security` (macOS asks once), and sends one `GET https://api.anthropic.com/api/oauth/usage` with that token, every 5 minutes and when the menu opens, backing off when Anthropic asks. It keeps only the percentages and reset times it gets back; the token is never stored, logged or renewed by AgentPlay (renewing would rotate Claude Code's own login). This exists because Claude Code started from an editor's agent panel (Zed's claude-acp, other Agent SDK hosts) has no statusline and so never reports plan usage on the Mac; it is how CodexBar and the other usage menus read it. Tests: `ClaudeAccountTests`.

Switching Claude accounts (Settings → Account, and Switch Claude Account in the menu) makes no requests either. On the user's Save, it copies Claude Code's login from its Keychain item into AgentPlay's own item ("AgentPlay Claude accounts", one per account) and keeps the name, organization and last-seen usage in preferences. On Switch, it saves the outgoing login first, writes the chosen login into Claude Code's item (keeping this Mac's MCP server logins), and replaces only `oauthAccount` in `~/.claude.json`, after copying it to `~/.agentplay/backups`. All of it goes through `/usr/bin/security`, as Claude Code does, with the login on stdin, never in a process's arguments. Refused while a session is mid-step or when the saved login has expired. "Remember accounts I sign in to" is off by default. Tests: `ClaudeSwitchTests`, including an end-to-end round trip on throwaway Keychain items.

Otherwise the app makes no requests of its own: no update check, no telemetry SDK, no remote fonts, icons or scripts (all assets are drawn in code or bundled). Every child process gets `AGENTPLAY_NO_NETWORK=1` and `DO_NOT_TRACK=1`, whatever the parent environment says (`Redaction.childEnvironmentIsMinimal`), so the viewer it opens hides the newsletter card and can't send an email. The only things that open a web page are links the user clicks (provider usage pages, "See supported agents"), and they open in the browser.

`test_snapshot_makes_no_network_calls` builds a snapshot with `socket.connect` raising.

## What it reads

| Read | Why |
|---|---|
| `~/.agentplay/index.json` and `maps/` | Everything on Now and Learn |
| The agents' transcript folders (`~/.claude`, `~/.codex`, `~/.cursor`, `~/.copilot`, VS Code's user storage, OpenCode's data folder, `~/.kiro`, and folders the user adds) | The same incremental build `agentplay build` runs |
| Repo folders those sessions worked in | The builder checks paths and git settings to tell which repo a file belongs to. For repos under Documents, Desktop or Downloads, macOS asks once |
| Session facts and the Run health report (`facts.py`, `health.py`) | The Insights rows: a score, five parts, and fixes that name a command, tool, hook or MCP server, with counts and session ids. Computed locally from the maps, cached 15 minutes in `~/.agentplay/cache/companion-insights.json`; no titles, paths or arguments (`test_insights_report_is_cached_and_private`) |
| Claude Code transcripts, for steering | Your own messages, read only to classify: a correction (opens with "no", "wait", "stop"… or says "still not working"), frustration (swearing, or three or more ! or ?), an interruption, a refused tool call. Only `[time, kind, line id]` is kept, in the usage cache; no text (`test_steering_signals_count_people_not_programs`). Failing hooks: the error Claude Code recorded is read to pick a fixed phrase (wrong event name, not found, timed out, exit code) and dropped |
| Claude Code transcripts, for usage | Only lines that carry `usage`: message id, request id, model, timestamp, token counts, and the folder name of `cwd`. Cached in `~/.agentplay/cache/companion-claude-usage.json` with nothing else; message text is never kept (`test_usage_reads_claude_transcripts_once_per_request`) |
| The tail of Codex transcripts | Only the numeric `token_count` record: context window and plan windows |
| `lsof` output for recent transcripts | Which agent processes are live |
| Claude Code's live-process records, `~/.claude/sessions/<pid>.json` | Which sessions are working, idle or waiting on you. The app re-reads them the moment the folder changes, so "needs you" shows in about a second. Only `pid`, `sessionId`, `status`, the start and status times, and the folder name of `cwd` are kept; the full path, the session name and the socket path are never read in (`LiveRecordsTests.parsesAndKeepsOnlyTheFolderName`) |
| The parent processes of a live session's `pid` | To find the terminal app a click should bring forward. Process ids only, through `sysctl` |

Not requested: Full Disk Access, Accessibility, Screen Recording, Contacts, browser data. Keychain only with the opt-in above, for that one item. The app is not sandboxed, so it can read the agent folders the user already owns; it uses no entitlement beyond the hardened runtime.

## What it never shows, stores or sends

Prompt text, session titles and task labels (both are prompt text in the index), assistant or reasoning text, file contents, command arguments, tool inputs and outputs, repo remotes, PR URLs, transcript paths.

These are never read into the snapshot in the first place: the Python core does not pass them through (`companion.py`), and the Swift side has nothing to leak. Tests:

- `test_snapshot_never_leaks_prompt_args_paths_or_remotes`: a real-shaped Claude Code transcript seeded with secret prompt text, secret arguments, a secret environment value, secret tool output and a secret remote is built, then snapshotted; none of it appears, and the title that does sit in the index stays out.
- `test_synthetic_index_fields_never_reach_snapshot`: titles, task labels, remotes, PR URLs and transcript paths in index rows.
- `Redaction.summaryHasNoPathsOrIds`, `notificationsOmitRepoByDefault`: the copied summary and notifications.
- `test_codex_limits_fresh` asserts nothing from a Codex transcript's text reaches the snapshot.
- The existing map contract (`test_builder_never_leaks_free_text`) still passes unchanged.

What the popover does show is what the viewer shows from the same maps: agent, repo name, shell verbs (`pnpm test`, never its flags), tool names, repo-relative file paths in the map's path mode, model names, counts and times.

## Notifications

Off by default. macOS is asked for permission only when the user switches on an alert. A notification carries the agent, the command family and a count, for example "pnpm test failed 3 times in a row." "Session needs you" says "Claude Code needs you" and is skipped when that session's terminal is already the frontmost app. "Session finished" carries how long it worked, tool-call and error counts and the last step's tool or command name, from the map. Repo names, in any of them, only if the user turns that on (`needsYouNamesTheProjectOnlyWhenAllowed`, `finishedNoteNeedsRealWorkAndSaysHowItWent`). The payload holds the session id and event `seq` so a click can open the moment; nothing else. History records alert ids and times only; Settings → Notifications → Forget alert history clears it, and the watermark moves forward so nothing old fires afterwards.

## What it writes

- Its preferences, in the `ai.entelligence.agentplay.companion` domain: settings, mutes, pins, dismissed Learn cards, alert ids and times, custom folders.
- `~/Library/Caches/ai.entelligence.agentplay.companion/viewer.pid` while its viewer runs.
- The same maps and index in `~/.agentplay` that `agentplay build` writes.

Nothing is written inside the app bundle (Python bytecode is precompiled at build time and `PYTHONDONTWRITEBYTECODE=1` is set). Uninstalling leaves `~/.agentplay` alone unless asked (`scripts/companion-uninstall.sh --remove-maps`).

## The local viewer

The viewer the companion opens is the stock `agentplay view`, on 127.0.0.1 and a free port, with its existing guards: loopback peer, local `Host`, own `Origin`, `Sec-Fetch-Site`. The self-test checks two of them against the running bundle: a foreign `Host` (DNS rebinding) and a foreign `Origin` both get 403. The companion adds no HTTP endpoint, socket or IPC listener; it talks to the core only through a child process's stdout. A copied replay link is a 127.0.0.1 link and the app says it only works on this Mac.

## Process hygiene

The core runs as `python -I` (no `PYTHON*` variables, no user site-packages) with a minimal environment: only `HOME`, a fixed `PATH`, locale, and the user's `AGENTPLAY_*` settings; nothing else from the parent, such as cloud credentials, is passed. On quit or SIGTERM the app stops its snapshot child and its viewer. A viewer left by a crash is stopped on next launch only if its command is this app's own Python, so a viewer the user started with `uvx agentplay` is never touched (`ViewerOwnership` tests, self-test `leftoverReaped`).
