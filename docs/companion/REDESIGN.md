# Companion redesign: the menu does, the window explains

Rule: **a row is in the menu only if it needs you now, or one click on it does something useful.** Anything that is a report (scores, trends, patterns, history) lives in the Insights window. Written for someone who does not read code.

## The menu

```
┌──────────────────────────────────────────────┐
│ [Pip]  docs-site is waiting for you          │  Pip's line: one thing, the most urgent
│                                              │
│ NEEDS YOU                                    │
│ ●  Companion menu redesign                › │  Claude Code's own session title
│    agentplay · waiting 2m                    │  › Go to Zed · Open Replay
│                                              │
│ Claude Code                           Team   │  plan card, inline as before
│ info@entelligence.ai · entelligence.ai       │
│ Session ▬▬▬▬▬▬▬▬░░░░  72% · resets in 6m     │
│ Weekly  ▬▬▬▬▬▬▬▬▬▬▬▬  96% · out in 5h 24m    │  red when it runs out first
│ Switch Claude Account                     › │
│                                              │
│ RUNNING                                      │
│ ●  Fix usage dedupe                       › │
│    agentplay · working 40s                   │
│                                              │
│ ✦  1 fix ready · Run health 61/100        › │  opens the window on Fixes
│ ✓  1 change being measured                › │  opens the window on Applied
│                                              │
│ Today  $31.94       Last 30 days  $1,891  › │  opens the window on Usage
└──────────────────────────────────────────────┘
```

Leaves the menu (still in the window): How You Steer, Run Health's parts, the "python3 failed 18×, passed later" pattern (it fixed itself; nothing to do), Usage Details.

## The Insights window

```
┌───────────────┬──────────────────────────────────────────────────────────┐
│ Overview      │  Fixes                                                    │
│ Fixes      1  │                                                           │
│ Applied    1  │  Your SessionStart hook fails every time            +4.1  │
│ Autotune      │  It ran 358 times in 358 sessions and never worked.       │
│ Steering      │  Why: it was written for a different event, so Claude     │
│ Usage         │  Code rejects its answer.                                 │
│               │  After: sessions start clean.                             │
│               │                                                           │
│               │  [ Preview and apply ]   [ Not now ]                      │
│               ├───────────────────────────────────────────────────────────┤
│               │  Applied                                                  │
│               │                                                           │
│               │  SessionStart hook   applied Oct 27, ~/.claude            │
│               │  Failures per session   before 1.0  →  since 0.0          │
│               │  12 sessions since · Worked                     [ Undo ]  │
└───────────────┴──────────────────────────────────────────────────────────┘
```

- Overview: the score, what moved it this month, in sentences ("Down 16: fewer sessions ended in a PR").
- Fixes: each in plain words (what happens, why, what changes), Preview and apply, Not now.
- Applied: every change AgentPlay made, the measure it was chosen for, before and since, a verdict (Measuring, Worked, No change), Undo.
- Autotune: on or off per repo, what it is waiting to apply, Approve or Dismiss.
- Steering: the four rates and their trend.
- Usage: the chart, by model, by project.

Core changes this needs: an applied fix leaves the Fixes list; applying a fix by hand records a baseline so before and since can be compared, as Autotune already does.

## Onboarding, step 4: what Pip tells you about

Today only "Tell me when a session needs me" is offered. Every alert gets a toggle and a sample of what it says:

```
Tell me when…
[on]  a session needs me          "Claude Code needs you: docs-site"
[on]  a session finishes          "Done in 14 min: 212 tool calls, 2 errors"
[off] a command keeps failing     "pnpm test failed 3 times in a row"
[off] my plan is about to run out "Weekly limit: out in about 5 hours"
```

The same four, with the same samples, are the first block of Settings → Notifications.
