# UX states

Every state the brief lists, what the popover says, and where it is drawn. Screens are rendered from the real views over synthetic fixtures (`apps/macos/AgentPlayCompanion/Fixtures`); regenerate with `scripts/companion-build.sh dev` then `AgentPlayCompanion --render docs/companion/screens` (see INSTALL.md).

## Information architecture

Four destinations, no more: **Now** (default), **Limits**, **Learn**, and **Settings** (a separate window). The popover is 380 pt wide; Now fits one screen with at most two run cards before "View all". Search appears only behind View all (⌘K).

| Key | Does |
|---|---|
| ⌘1 / ⌘2 / ⌘3 | Now / Limits / Learn |
| ⌘K | View all, with the search field focused |
| ⌘O | Open full AgentPlay |
| ⌘, | Settings |
| Return | Open replay on the expanded card |
| Option-click the menu-bar mark | Open the full viewer (the footer button is the discoverable route) |
| Click the mark in the popover | Session constellation; click again to return |

## States

| State | Copy | Screen |
|---|---|---|
| Scanning | "Finding your sessions…", count indexed so far, Cancel | [scanning](screens/state-scanning-light.png) |
| Rescanning with data | Cards stay; one line "Rescanning · N sessions indexed" with Cancel | (inline, Now) |
| Scan stalled (folder prompt) | "Still scanning. Is macOS asking for folder access?" with the reason and a settings link | (after 45 s) |
| No agents found | "No local coding-agent sessions yet", Choose folder, Try a sample, See supported agents | [no agents](screens/state-no-agents-light.png) |
| Agents found, nothing built | "No sessions built yet", Scan now | (same view) |
| Detected, none active | "No active runs detected" plus "Last run 59m ago"; never "all agents idle" | [idle](screens/state-detected-idle-light.png) |
| Running | Filled lime dot. Only with process evidence: an agent process holds the transcript open, or a `claude` process runs in its project folder | [two running](screens/v1-now-light.png) |
| Recent activity only | Lime ring and "Activity recorded 2m ago. No running process seen." Shared-database agents (Cursor, OpenCode, Kiro, Copilot VS Code) are low confidence | [partial](screens/state-partial-light.png) |
| Map behind its transcript | "Replay is 18m behind the transcript. Catching up." No "running" claim from a stale map | (real data only) |
| Tool still running | "Running: pytest · 4m" only on a live, current map | (real data only) |
| Error reading a session | "Can't read 1 session. … Rebuild it from Settings (Diagnostics)." The source stays listed | [partial](screens/state-partial-light.png) |
| Source can't be read | Red row in Sources and onboarding with the path and the fix; never dropped silently | [settings, sources](screens/settings-sources-light.png) |
| Core too old / runtime missing | "This AgentPlay (agentplay 0.2.3) has no companion snapshot…", Try again, Try a sample | [error](screens/state-error-light.png) |
| Context without a denominator | "142k tokens" + Recorded + "No context limit is recorded, so there is no percentage." Never 0% | [limits](screens/v1-limits-light.png) |
| Context with a provider denominator | "71% · 184k of 258k" + "Reported by Codex" | [limits](screens/v1-limits-light.png) |
| Context unavailable | "Context usage unavailable" + why (Cursor records no tokens) | [partial limits](screens/state-partial-limits-light.png) |
| Plan usage unavailable | "Plan usage unavailable", the reason, Open provider usage | [limits](screens/v1-limits-light.png) |
| Stale plan snapshot | Struck-through grey value, "Reported by Codex · stale", "updated 6h ago", why it is stale | [stale](screens/state-stale-quota-light.png) |
| Conflicting plan reports | Low confidence, "they may be different accounts" | [conflicting](screens/state-conflicting-plans-light.png) |
| Worth a look (Direction B card) | "pnpm test failed 3 times · Claude Code", "Since 10:42 · payments-api · non-zero exit", See sequence, Mute this pattern | [now](screens/v1-now-light.png) |
| Resolved failure | Same card on grey, "since resolved · passed later in the run"; never a notification | (Now) |
| Learn card | Denominator first, observed count, "passed later in N of M", Review evidence, Dismiss | [learn](screens/v1-learn-light.png) |
| Nothing to learn | "A failure has to recur in at least two runs before it shows here." | (Learn) |
| Multiple sessions in one repo | Each card is agent + repo + its own time; pin from the context menu; View all lists each | [many](screens/state-many-view-all-light.png) |
| First-run sample | Every surface shows "Sample data"; sample data never notifies; Replay opens the viewer's own demo | [onboarding 3](screens/onboarding-3-light.png) |
| Permissions: notifications denied | "macOS has notifications off for AgentPlay." + Open System Settings | [notifications](screens/settings-notifications-light.png) |

## Onboarding (five steps)

[1 Welcome](screens/onboarding-1-light.png) · [2 Find sessions](screens/onboarding-2-light.png) · [3 First value](screens/onboarding-3-light.png) · [4 Optional choices](screens/onboarding-4-light.png) · [5 Stay local](screens/onboarding-5-light.png)

Notification permission is requested only when the user switches on an alert, in step 4 or Settings. "Connect Entelligence" is visible but disabled, with the reason.

## Run card interactions

- Click: expands, shows the five-tick micro timeline of real recorded steps, Open replay, Jump to last error, Full history.
- Drag across the ticks: reads each step (`#9 edit · Edit · src/charge.test.ts`); click a tick to open that exact `seq`.
- Secondary click: Open replay, Copy redacted run summary, Copy local replay link (with the 127.0.0.1 note), Pin to top, Mute alerts for this run.

## Motion

All transitions use cubic-bezier(.16, 1, .3, 1) at 220 ms or less, and none run under Reduce Motion.

- Map to moment: four dots settle onto the latest tick when a card opens.
- Heartbeat: the mark's dots spread for three slow beats when an active run gains events, then stop.
- Completion: four dots converge once on Open replay when a run you were watching stops.
- The constellation is drawn from the run's real event counts by kind, log scaled. It has no connections between spokes.
- Optional sound: Tink at 25% volume, only on actions the user takes.
