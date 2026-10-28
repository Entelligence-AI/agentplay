# UX states

Every state the brief lists, what the menu says, and how to see it. Screens aren't committed: draw every state, light and dark, from the real views over synthetic fixtures (`apps/macos/AgentPlayCompanion/Fixtures`) with `scripts/companion-build.sh dev`, then `AgentPlayCompanion --render <dir>` (see INSTALL.md).

## Information architecture

A real NSMenu from the status item, like CodexBar, Tailscale and Apple's own menu bar extras. Hover, arrow keys, type-select, submenus, dismissal and key equivalents are the system's. Every clickable row is a native menu item. The only custom views are display-only: the provider cards, the usage summary and the usage chart.

Order follows attention:

1. **Needs You**, only when a session is waiting on input.
2. **Plan limits**, one card per provider this Mac uses, most constrained first.
3. **Live Sessions**: only sessions whose process is alive and is still the one that wrote the record (a pid reused after a reboot does not count): waiting, working, and up to two idle for under six hours, in grey. Finished runs are not rows here; when no agent process is open, one line says so and names the last run ("Last: Claude Code in agentplay, 2h ago").
4. **Insights · Last 30 Days**: How you steer (a card), Run Health, up to two fixes, one error or pattern.
5. **Usage**: Today and Last 30 Days, then Usage Details ›.

The left-click menu ends with its content. AgentPlay's own commands are on **right-click**: Open AgentPlay ⌘O, All Sessions…, Refresh ⌘R (its subtitle says when), Settings… ⌘,, Quit ⌘Q. The shortcuts also work while the left-click menu is open.

| Input | Does |
|---|---|
| Point at a session › | Its detail: status and how long, agent and model, when it started, context as a bar, tool calls and errors, its last four steps. On top: Go to Terminal (the app is named: Terminal, iTerm, Ghostty, VS Code, Cursor, found by walking up from the agent's process) and Open Replay |
| Steering Details › | The four rates in full, each against the 30 days before, with what kind they were (corrections that said it still isn't working; swearing versus piles of punctuation), and Open Insights in AgentPlay… |
| Usage Details › | Submenu: 14-day chart, 7 and 30 days, by model, by project |
| An error or pattern › | Submenu: its details; click opens the replay at that step |
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
| Right-click or Control-click | Opens the app's own commands: Open AgentPlay, All Sessions…, Refresh, Settings…, Quit. While the line beside Pip shows (a left click acts on it), right-click opens the menu instead |
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
| How you steer | The first card under Insights: "7% corrected" and "5% frustrated" as two large numbers, each with a trend arrow (up is orange, both are better lower) and a 30-day sparkline, then one sentence: "1 in 14 messages corrects the agent. Frustration is down from 9%." When most corrections say the result didn't hold, the sentence says so and points at verification. The card is display-only and sits on the provider cards' left edge; the Steering Details row under it opens the four rates. Counted from the messages you type in Claude Code; prompts a program sends (an SDK pipeline, a test harness) don't count; no text is kept | menu-insights, card-steering, submenu-steering |
| Run Health | "Run Health 73 / 100" with the lever first: "82 within reach from 3 fixes · up 4.5". Its submenu says what the fixes would take it to, then the five 20-point parts as bars, each with one line on what it counts (Shipping: sessions that edited code and ended in a PR; Verification: sessions that ran a check after the last edit; Efficiency: errors and retries per call, and cache hits; Context hygiene: compactions and files re-read after one; Environment health: denied, blocked or missing tools per call), what AgentPlay applied (each with Undo), what Autotune is holding (Approve, Dismiss), and Open Run Health in AgentPlay… | submenu-run-health |
| A fix | A row per fix, up to two ("Fix the SessionStart:startup hook", "+4.4 points · Environment health · payments-api"). Its submenu is three short labelled lines, What happened / Why (orange, for a hook that answers the wrong event) / After the fix, then the actions: Apply This Fix… when AgentPlay can write the change itself, its subtitle saying exactly what it will do (a hook answering the wrong event is fixed in Claude Code's own settings.json: its entry leaves the event it fails at, and joins the one it answers as unless it is already there; the first write there asks once, like a repo; the preview shows the removed and added lines; journaled and undoable), Copy a Prompt for Claude Code (a paste-ready prompt carrying the problem, cause, commands, file and intended effect), Open settings.json (Option: show it in Finder), Open a Session Where It Happened, See It in AgentPlay… | submenu-fix |
| An error or pattern | One row: for a single run, the agent, project, count, reason, start and whether it passed later, and Open Replay at the Failure; for a pattern, its denominator, whether it passed later, and every run it failed in, each opening the replay at the failing step | (real data only) |
| Nothing running | "No agent is running", "Last: Claude Code in agentplay, 2h ago. Right-click Pip for all sessions." | detected-idle |
| Core too old / runtime missing | "Can't Read Your Sessions" and the reason | error |
| Last refresh failed | The last good data stays, with "Last Refresh Failed" and the reason on top | (inline) |
| Sample data | "Sample Data, Not Yours" header, then "Show My Data" at the top of the menu; cards say "Sample data", not an update time; sample data never notifies and never draws the icon's bars | menu |
| Notifications | Settings → Notifications opens with where macOS stands: not asked yet (Allow…), off (Open System Settings), or allowed, with Send a Test Notification so one can be seen before any preset is on | notifications |

## Settings

The standard Mac settings window: toolbar tabs (General, Live Limits, Sources, Notifications, Privacy, Account, Diagnostics), grouped forms, the window titled after the pane. General.

## Onboarding (four steps, told by Pip)

One idea per screen, big type, room around it, one button; 600 by 560.

1. **Meet Pip**: Pip wakes (asleep, then blinks and waves). "Hi, I'm Pip. I watch your coding agents from the menu bar, so you don't have to watch the terminal." Let's Go. A quiet link offers sample data instead.
2. **Reading your sessions**: Pip types while a live counter climbs as the first scan indexes; which agents it read from, and "Read on this Mac; nothing leaves it." Under it, the one permission worth asking for while there's time to answer: "May I notify you?" with Allow (the system prompt), or "Notifications are allowed", or, when macOS has them off, Open System Settings. A line says macOS may ask about the Desktop or Documents folder and why. It moves on by itself when done.
3. **This is me, in your menu bar**: the status item exactly as it draws right now (pose, fill, and Pip's sentence, "1 waiting on you: docs-site" or "All quiet"), then the five poses with room between them (needs you, finished, keeps failing, going fast, limit reached). "Click me for the details; right-click for Settings and Quit."
4. **Three choices**, all switched on as the suggestion: needs-you alerts (disabled, with Open System Settings in its own row, when macOS has notifications off), Claude usage from your account (the row says it reads the Keychain login and that macOS asks once), open at login. Nothing is applied until Start; Back and forth changes nothing. "Switch any off here, or change them in Settings later."

Progress dots, no step counter. Screens fade and rise in; every animation follows Reduce Motion.

## Pip, interactive

- **Point at Pip** in the menu bar: it looks up at you (tracking-area events, no timer).
- **In the menu** Pip blinks, types while agents work and waves while one waits, on a quarter-second beat that exists only while the menu is open.
- **Click Pip** in the menu: it hops and moves to its next suggestion, drawn from your data: what's waiting, a window running hot, the top fix, which model did most of the month's work and what it cost, a jump in corrections, a pattern that's only retrying. The row under it does the suggestion: Go to It, Apply This Fix…, Open My Usage Page, See the Breakdown in AgentPlay, Open Run Health…
