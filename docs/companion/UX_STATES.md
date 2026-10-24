# UX states

Every state the brief lists, what the menu says, and how to see it. Screens aren't committed: draw every state, light and dark, from the real views over synthetic fixtures (`apps/macos/AgentPlayCompanion/Fixtures`) with `scripts/companion-build.sh dev`, then `AgentPlayCompanion --render <dir>` (see INSTALL.md).

## Information architecture

A real NSMenu from the status item, like CodexBar, Tailscale and Apple's own menu bar extras. Hover, arrow keys, type-select, submenus, dismissal and key equivalents are the system's. Every clickable row is a native menu item. The only custom views are display-only: the provider cards, the usage summary and the usage chart.

Order follows attention:

1. **Needs You**, only when a session is waiting on input.
2. **Plan limits**, one card per provider this Mac uses, most constrained first.
3. **Sessions**, up to five, then Show All Sessions…
4. **Usage**: Today and Last 30 Days, then Usage Details › and Recent Errors ›.
5. Open AgentPlay ⌘O, Refresh ⌘R (its subtitle says when), Settings… ⌘,, Quit ⌘Q.

| Input | Does |
|---|---|
| Click a session | Brings its terminal app forward (the tooltip names it) (Terminal, iTerm, Ghostty, VS Code, Cursor), found by walking up from the agent's process. Opens the replay when there is no terminal app to find: an ended run, tmux, ssh |
| Option-click a session | Opens its replay instead |
| Usage Details › | Submenu: 14-day chart, 7 and 30 days, by model, by project |
| Recent Errors › | Submenu: each error or pattern; click opens the replay at that step |
| Turn On Live Limits… | Settings → Live Limits, which shows the exact change before applying it |

## Menu-bar icon: Pip

Pip is a small template character, so macOS tints it for any menu bar. Its body fills with the 5-hour session, it stands on a ground line that is the week, and its pose is the one thing worth knowing now, in this order:

| Pose | When |
|---|---|
| Waving | A session is waiting on you. The arm moves for 8 s, then holds |
| Hourglass | A plan window is spent until it resets |
| X eyes | A command just failed three times |
| Arms up | A session just finished |
| Sweat drop | Ahead of pace (or 90% used) on a plan window |
| Typing | An agent is working; the frame changes when an agent actually writes, never on a timer |
| Asleep | Nothing running |

When something happens, a short line appears beside Pip for 8 s: which session or command, since the pose says what. It stays under about 110 pt so it can't push other icons behind the notch.

| Input on the icon | Does |
|---|---|
| Click while the line shows | Acts on it: go to the waiting or finished session's terminal, open the replay at a failure, open the usage page for a spent window |
| Click otherwise | Opens the menu |
| Right-click or Control-click | Always opens the menu |
| Option-click | Opens AgentPlay |

Nothing moves under Reduce Motion or in Low Power Mode, and no timer runs while Pip is still. Settings → General turns Pip off (two plain bars instead) and the line off separately. The menu opens with Pip at twice the size and one sentence: "1 waiting on you: docs-site", "Going fast, 2h 10m left in this session", "All quiet", with session and week below.

Notifications for "Session needs you" carry Go to Terminal, Snooze 15 Minutes and Mute This Session. "Session finished" (opt-in) fires only after two minutes of work and says how it went, "payments-api is done: Worked 14m · 32 tool calls · 1 error · last: pnpm test", with Go to Terminal and Open Replay.

## States

| State | What the menu shows | Render |
|---|---|---|
| Scanning | "Finding Your Sessions…", count indexed so far | scanning |
| No agents found | "No Agents Found", "Start Claude Code or Codex, then reopen." | no agents |
| Agents found, nothing indexed | "No Sessions Indexed Yet", "Refresh to scan Claude Code, Codex." | (same view) |
| Needs you | "Needs You" section first, orange dot, "Claude Code · Waiting 40s", and a dot on the icon. Read straight from Claude Code's live records when they change, so it shows in about a second rather than at the next scan | menu |
| Needs you, notification | Opt-in ("Session needs you", offered in onboarding). "Claude Code needs you", with the project only if repo names are allowed. Not sent when the session's terminal is in front; cleared once the session moves on. Click brings the terminal forward; Mute This Session silences that session | (system banner) |
| Working | Accent dot, what it did last, context % when known | menu |
| Idle or ended | Grey dot (idle process) or hollow dot (no process) and how long ago | idle |
| Plan limits | Card per provider: name, plan, "Updated just now · on pace for the week", then each window as "34% · resets in 2h 18m" over a bar. Tint follows pace: accent on pace, orange ahead, red over | menu |
| Pace warning | Orange line in the card: "Session runs out in 1h 12m at this pace", only when that comes before the reset | (no fixture is over pace) |
| Claude limits off | Card says "Live limits are off", then "Turn On Live Limits…" | (Settings preview not in the docs: it shows your home path) |
| Claude from an editor panel | Every live Claude session started from an editor (entrypoint `sdk-ts`): the card says "Your sessions run in Zed's agent panel, which doesn't report plan usage", and the menu offers "Show Usage from My Claude Account…" | (real data only) |
| Claude account usage | Opt-in in Settings → Live Limits; the card fills from the account. Failures say what to do: no login, Keychain denied, login expired, Anthropic busy | (Settings → Live Limits; not in the docs, it shows your home path) |
| Claude account | The card shows who the numbers belong to, "info@… · organization", from `~/.claude.json`. Switch Claude Account › lists saved accounts with a check on the active one and each one's last-seen session and week; Save for Switching, Add Another Account…, Manage Accounts… | (real data only) |
| Claude limit reached | Claude Code was refused for a window: that window shows full with its reset time, read from the transcript, no network | (real data only) |
| Stale plan snapshot | Grey bar, "88% · stale, updates when Codex runs" | stale |
| Conflicting plan reports | "Recent sessions reported Plus and Pro. They may be different accounts." | conflicting |
| Usage | Today and Last 30 Days, cost then tokens. Costs are estimates at API list prices; unpriced models say "unpriced", never $0 | menu |
| Usage details | Submenu with the chart and breakdowns | usage details |
| Steering | "Steering · 12% corrections · 9% frustrated": its submenu shows Blume's signal rates from the messages you type in Claude Code (corrections, frustration, interruptions, refusals per tool call), each against the 30 days before with a 30-day trend. Prompts a program sends (an SDK pipeline, a test harness) don't count; no text is kept | (real data only) |
| Apply a fix | Apply… previews the exact lines and file; the first write to a repo asks once; it goes through the journaled writer from #19. Run Health lists what AgentPlay applied, each with Undo, and what Autotune is holding, with Approve and Dismiss. A failing hook's fix names the commands configured for it and why it fails, with Show settings.json in Finder | (real data only) |
| Insights | "Insights · Last 30 Days": Run Health "72 / 100", with "Down 4 · weakest: Verification"; its submenu has the five 20-point parts as bars and Open Run Health in AgentPlay. Then up to three fixes from the Run health report ("Fix the PreToolUse hook", "+3 points · Environment health"), each with what was seen, what fixing it changes and where, and Preview and Apply in AgentPlay, Open an Example Session, Copy as Text. Then up to three errors, each with its details: for a single run, the agent, project, count, reason, start and whether it passed later, and Open Replay at the Failure; for a pattern, its denominator, whether it passed later, and every run it failed in, each opening the replay at the failing step | (real data only) |
| Many sessions | Five rows, then "Show All Sessions…" | many |
| Core too old / runtime missing | "Can't Read Your Sessions" and the reason | error |
| Last refresh failed | The last good data stays, with "Last Refresh Failed" and the reason on top | (inline) |
| Sample data | "Sample Data, Not Yours" header, then "Show My Data" at the top of the menu; cards say "Sample data", not an update time; sample data never notifies and never draws the icon's bars | menu |
| Notifications denied | Settings → Notifications: "macOS has notifications off for AgentPlay." + Open System Settings | notifications |

## Settings

The standard Mac settings window: toolbar tabs (General, Live Limits, Sources, Notifications, Privacy, Account, Diagnostics), grouped forms, the window titled after the pane. General.

## Onboarding (four steps, told by Pip)

1. **Meet Pip**: Pip wakes (asleep, then blinks and waves): "Hi, I'm Pip."
2. **Reading your sessions**: Pip types while a live counter climbs as the first scan indexes; "Read on this Mac. Nothing leaves it." It moves on by itself when done.
3. **What I learnt**: three facts from this Mac's own sessions fade in one by one: where the work goes, which model does it, the biggest fix.
4. **Find me in the menu bar**: the pose legend (needs you, finished, keeps failing, going fast, limit reached) and three choices: needs-you alerts, Claude usage from your account, open at login.

Progress dots, no step counter. Every animation follows Reduce Motion.

## Pip, interactive

- **Point at Pip** in the menu bar: it looks up at you (tracking-area events, no timer).
- **In the menu** Pip blinks, types while agents work and waves while one waits, on a quarter-second beat that exists only while the menu is open.
- **Click Pip** in the menu: it hops and moves to its next suggestion, drawn from your data: what's waiting, a window running hot, the top fix, a jump in corrections, a pattern that's only retrying. The row under it does the suggestion: Go to It, Apply This Fix…, Open My Usage Page, Open Run Health…
