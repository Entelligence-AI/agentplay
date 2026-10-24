# UX states

Every state the brief lists, what the menu says, and where it is drawn. Screens are rendered from the real views over synthetic fixtures (`apps/macos/AgentPlayCompanion/Fixtures`); regenerate with `scripts/companion-build.sh dev` then `AgentPlayCompanion --render docs/companion/screens` (see INSTALL.md).

## Information architecture

A real NSMenu from the status item, like CodexBar, Tailscale and Apple's own menu bar extras. Hover, arrow keys, type-select, submenus, dismissal and key equivalents are the system's. Every clickable row is a native menu item. The only custom views are display-only: the provider cards, the usage summary and the usage chart.

Order follows attention:

1. **Needs You**, only when a session is waiting on input.
2. **Plan limits**, one card per provider this Mac uses, most constrained first.
3. **Sessions**, up to five, then Show All Sessions…
4. **Usage**: Today and Last 30 Days, then Usage Details › and Recent Errors ›.
5. Open AgentPlay ⌘O, Refresh ⌘R (its subtitle says when), Settings… ⌘,, Quit ⌘Q.

![menu, light](screens/menu-light.png)

| Input | Does |
|---|---|
| Click a session | Brings its terminal app forward (the tooltip names it) (Terminal, iTerm, Ghostty, VS Code, Cursor), found by walking up from the agent's process. Opens the replay when there is no terminal app to find: an ended run, tmux, ssh |
| Option-click a session | Opens its replay instead |
| Usage Details › | Submenu: 14-day chart, 7 and 30 days, by model, by project |
| Recent Errors › | Submenu: each error or pattern; click opens the replay at that step |
| Turn On Live Limits… | Settings → Live Limits, which shows the exact change before applying it |

## Menu-bar icon: Pip

Pip is a small template character, so macOS tints it for any menu bar. Its body fills with the 5-hour session, it stands on a ground line that is the week, and its pose is the one thing worth knowing now, in this order:

| Pose | When | |
|---|---|---|
| Waving | A session is waiting on you. The arm moves for 8 s, then holds | ![](screens/pip-wave-light.png) |
| Hourglass | A plan window is spent until it resets | ![](screens/pip-limit-light.png) |
| X eyes | A command just failed three times | ![](screens/pip-oops-light.png) |
| Arms up | A session just finished | ![](screens/pip-cheer-light.png) |
| Sweat drop | Ahead of pace (or 90% used) on a plan window | ![](screens/pip-sweat-light.png) |
| Typing | An agent is working; the frame changes when an agent actually writes, never on a timer | ![](screens/pip-work-light.png) |
| Asleep | Nothing running | ![](screens/pip-sleep-light.png) |

When something happens, a short line appears beside Pip for 8 s: which session or command, since the pose says what ([needs you](screens/statusitem-island-light.png)). It stays under about 110 pt so it can't push other icons behind the notch.

| Input on the icon | Does |
|---|---|
| Click while the line shows | Acts on it: go to the waiting or finished session's terminal, open the replay at a failure, open the usage page for a spent window |
| Click otherwise | Opens the menu |
| Right-click or Control-click | Always opens the menu |
| Option-click | Opens AgentPlay |

Nothing moves under Reduce Motion or in Low Power Mode, and no timer runs while Pip is still. Settings → General turns Pip off (two plain bars instead) and the line off separately. The menu opens with Pip at twice the size and one sentence: "1 waiting on you: docs-site", "Going fast, 2h 10m left in this session", "All quiet", with session and week below.

Notifications for "Session needs you" carry Go to Terminal, Snooze 15 Minutes and Mute This Session. "Session finished" (opt-in) fires only after two minutes of work and says how it went, "payments-api is done: Worked 14m · 32 tool calls · 1 error · last: pnpm test", with Go to Terminal and Open Replay.

## States

| State | What the menu shows | Screen |
|---|---|---|
| Scanning | "Finding Your Sessions…", count indexed so far | [scanning](screens/state-scanning-light.png) |
| No agents found | "No Agents Found", "Start Claude Code or Codex, then reopen." | [no agents](screens/state-no-agents-light.png) |
| Agents found, nothing indexed | "No Sessions Indexed Yet", "Refresh to scan Claude Code, Codex." | (same view) |
| Needs you | "Needs You" section first, orange dot, "Claude Code · Waiting 40s", and a dot on the icon. Read straight from Claude Code's live records when they change, so it shows in about a second rather than at the next scan | [menu](screens/menu-light.png) |
| Needs you, notification | Opt-in ("Session needs you", offered in onboarding). "Claude Code needs you", with the project only if repo names are allowed. Not sent when the session's terminal is in front; cleared once the session moves on. Click brings the terminal forward; Mute This Session silences that session | (system banner) |
| Working | Accent dot, what it did last, context % when known | [menu](screens/menu-light.png) |
| Idle or ended | Grey dot (idle process) or hollow dot (no process) and how long ago | [idle](screens/state-detected-idle-light.png) |
| Plan limits | Card per provider: name, plan, "Updated just now · on pace for the week", then each window as "34% · resets in 2h 18m" over a bar. Tint follows pace: accent on pace, orange ahead, red over | [menu](screens/menu-light.png) |
| Pace warning | Orange line in the card: "Session runs out in 1h 12m at this pace", only when that comes before the reset | (no fixture is over pace) |
| Claude limits off | Card says "Live limits are off", then "Turn On Live Limits…" | (Settings preview not in the docs: it shows your home path) |
| Claude from an editor panel | Every live Claude session started from an editor (entrypoint `sdk-ts`): the card says "Your sessions run in Zed's agent panel, which doesn't report plan usage", and the menu offers "Show Usage from My Claude Account…" | (real data only) |
| Claude account usage | Opt-in in Settings → Live Limits; the card fills from the account. Failures say what to do: no login, Keychain denied, login expired, Anthropic busy | (Settings → Live Limits; not in the docs, it shows your home path) |
| Claude account | The card shows who the numbers belong to, "info@… · organization", from `~/.claude.json`. Switch Claude Account › lists saved accounts with a check on the active one and each one's last-seen session and week; Save for Switching, Add Another Account…, Manage Accounts… | (real data only) |
| Claude limit reached | Claude Code was refused for a window: that window shows full with its reset time, read from the transcript, no network | (real data only) |
| Stale plan snapshot | Grey bar, "88% · stale, updates when Codex runs" | [stale](screens/state-stale-quota-light.png) |
| Conflicting plan reports | "Recent sessions reported Plus and Pro. They may be different accounts." | [conflicting](screens/state-conflicting-plans-light.png) |
| Usage | Today and Last 30 Days, cost then tokens. Costs are estimates at API list prices; unpriced models say "unpriced", never $0 | [menu](screens/menu-light.png) |
| Usage details | Submenu with the chart and breakdowns | [usage details](screens/submenu-usage-details-light.png) |
| Recent errors | Submenu row with a count; each error names the command, agent, project, reason and time | [partial](screens/state-partial-light.png) |
| Many sessions | Five rows, then "Show All Sessions…" | [many](screens/state-many-light.png) |
| Core too old / runtime missing | "Can't Read Your Sessions" and the reason | [error](screens/state-error-light.png) |
| Last refresh failed | The last good data stays, with "Last Refresh Failed" and the reason on top | (inline) |
| Sample data | "Sample Data, Not Yours" header, then "Show My Data" at the top of the menu; cards say "Sample data", not an update time; sample data never notifies and never draws the icon's bars | [menu](screens/menu-light.png) |
| Notifications denied | Settings → Notifications: "macOS has notifications off for AgentPlay." + Open System Settings | [notifications](screens/settings-notifications-light.png) |

## Settings

The standard Mac settings window: toolbar tabs (General, Live Limits, Sources, Notifications, Privacy, Account, Diagnostics), grouped forms, the window titled after the pane. [General](screens/settings-general-light.png).

## Onboarding (five steps)

[1 Welcome](screens/onboarding-1-light.png) · [2 Find sessions](screens/onboarding-2-light.png) · [3 First value](screens/onboarding-3-light.png) · [4 Optional choices](screens/onboarding-4-light.png) · [5 Stay local](screens/onboarding-5-light.png)

Notification permission is requested only when the user switches on an alert, in step 4 or Settings. "Connect Entelligence" is visible but disabled, with the reason.
