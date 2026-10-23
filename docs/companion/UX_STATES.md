# UX states

Every state the brief lists, what the menu says, and where it is drawn. Screens are rendered from the real views over synthetic fixtures (`apps/macos/AgentPlayCompanion/Fixtures`); regenerate with `scripts/companion-build.sh dev` then `AgentPlayCompanion --render docs/companion/screens` (see INSTALL.md).

## Information architecture

One native column, 320 pt wide, on the system menu material. It uses system label colours and the user's accent, and contains no hex colours. Top to bottom: header, provider limits, sessions, recent errors, usage, then the fixed actions. Usage comes last, so opening its detail never pushes sessions out of view. The middle scrolls only when the menu is taller than the screen. Settings is a separate window.

![menu, light](screens/menu-light.png)

| Key | Does |
|---|---|
| ↑ / ↓ | Move the highlight, like a native menu |
| → / ← | Open or close the highlighted provider |
| Return | Activate the highlighted row |
| ⌘R | Refresh |
| ⌘O | Open full AgentPlay |
| ⌘, | Settings |
| ⌘Q | Quit |
| Right-click | Secondary actions: on a session, Open Replay, Jump to Last Error, Copy Redacted Summary, Copy Local Replay Link, Mute Alerts for This Session; on a meter, Open the provider's usage page |

## Menu-bar icon

Two template bars: the 5-hour window on top and the weekly window below. A dot shows when a session needs you, and the bars dim when the numbers are stale. With no limits, it shows the AgentPlay mark. Rendered in [bars](screens/statusitem-bars-light.png), [waiting](screens/statusitem-waiting-light.png), [stale](screens/statusitem-stale-light.png) and [mark](screens/statusitem-mark-light.png).

## States

| State | Copy | Screen |
|---|---|---|
| Scanning | "Finding your sessions…", count indexed so far | [scanning](screens/state-scanning-light.png) |
| No agents found | "No agents found", "Start Claude Code or Codex, then reopen." | [no agents](screens/state-no-agents-light.png) |
| Agents found, nothing built | "No sessions built yet", "Refresh to scan Claude Code, Codex." | (same view) |
| Detected, none active | Sessions listed with hollow dots and their age; never "all agents idle" | [idle](screens/state-detected-idle-light.png) |
| Working | Accent dot, what it did last, context % when known. Only from a live process record or process evidence | [menu](screens/menu-light.png) |
| Needs you | Orange dot, "Waiting for you · 40s", and a dot on the menu-bar icon | [menu](screens/menu-light.png) |
| Live, map not built yet | The live status ("Working", "Idle") until the next refresh builds its map | (real data only) |
| Plan limits | Per window: bar, "34% used", reset countdown or day and time. Tint follows pace, not level: accent on pace, orange ahead, red over | [both providers](screens/menu-both-providers-light.png) |
| Pace warning | "At this pace, runs out in 1h 12m", only when that comes before the reset | (no fixture is over pace) |
| Claude limits off | "Turn On Live Limits…", "Show 5-hour and weekly usage", opens Settings → Live Limits | (not in the docs: the preview shows your real home path) |
| Plan usage unavailable | "Limits unavailable" plus the short reason | (provider row) |
| Stale plan snapshot | Grey bar, "Stale · 6h ago", "Updates when Codex runs again" | [stale](screens/state-stale-quota-light.png) |
| Conflicting plan reports | "Plans disagree", "Recent sessions reported Plus and Pro. They may be different accounts." | [conflicting](screens/state-conflicting-plans-light.png) |
| Recent error | Red triangle, "pnpm test failed 3×", agent · repo · reason, time | [partial](screens/state-partial-light.png) |
| Cross-run pattern | "pnpm test failed 9× in 4 runs", "Pattern across 5 recent sessions · passed later in some" | [menu](screens/menu-light.png) |
| Usage | "estimated": current 5-hour block and today, in tokens and list-price cost | [menu](screens/menu-light.png) |
| Usage detail | 14-day chart, last 7 and 30 days, by model and by project. Unpriced models say "unpriced", never $0 | [expanded](screens/menu-usage-expanded-light.png) |
| Many sessions | Four rows, then "Show All Sessions…" | [many](screens/state-many-light.png) |
| Core too old / runtime missing | "Can't read your sessions" and the reason | [error](screens/state-error-light.png) |
| Last refresh failed | The last good data stays, with "Last refresh failed" and the reason on top | (inline) |
| First-run sample | The header says "Sample data"; sample data never notifies | [onboarding 3](screens/onboarding-3-light.png) |
| Permissions: notifications denied | "macOS has notifications off for AgentPlay." + Open System Settings | [notifications](screens/settings-notifications-light.png) |

## Onboarding (five steps)

[1 Welcome](screens/onboarding-1-light.png) · [2 Find sessions](screens/onboarding-2-light.png) · [3 First value](screens/onboarding-3-light.png) · [4 Optional choices](screens/onboarding-4-light.png) · [5 Stay local](screens/onboarding-5-light.png)

Notification permission is requested only when the user switches on an alert, in step 4 or Settings. "Connect Entelligence" is visible but disabled, with the reason.

## Motion

Provider and usage disclosure use a short system animation, and none under Reduce Motion. When an active run records new events, the menu-bar icon pulses for two frames. Rows highlight with no animation, as native menus do.
