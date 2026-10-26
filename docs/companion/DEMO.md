# AgentPlay Companion: stakeholder demo runbook

About 8 minutes. Start from first run: no prefs, no local store, so the onboarding plays for real and the first scan reads the Mac's sessions live. To get there: quit the app, then `defaults delete ai.entelligence.agentplay.companion` and `mv ~/.agentplay ~/.agentplay.pre-demo`.

## Before the room

1. Close anything private. The menu names your repos (it never shows prompts, file contents or commands).
2. Quit other menu-bar meters so Pip is the only agent icon in the bar.
3. Pause any menu-bar hiding utility (Ice, Bartender, Hidden Bar), or Pip lands behind its chevron.
4. Open a terminal in a real repo with `claude` ready to start, but not started.
5. Notifications: if macOS already has them off for the app, the onboarding shows "Open System Settings" instead of asking. Turn them on first (System Settings → Notifications → AgentPlay Companion) if you want the needs-you banner in the demo.
6. Do not open the app yet.

## 1. Meet Pip (1 min)

Open `/Applications/AgentPlay Companion.app`.

- One screen, one idea: Pip asleep, then it blinks and waves. "Hi, I'm Pip. I watch your coding agents from the menu bar, so you don't have to watch the terminal."
- Click **Let's Go**.

## 2. Reading your sessions (1 min)

- Pip types while the counter climbs: every session already on this Mac, from Claude Code, Codex, Cursor and whatever else it finds. "Read on this Mac; nothing leaves it."
- "May I notify you?" → **Allow**, then accept the macOS prompt. The one permission the app asks for. macOS may also ask about the Desktop or Documents folder (the builder checking which repo a session worked in): allow it, or repo names stay hidden.
- It moves on by itself when the scan is done (about a minute for a couple of thousand sessions; later launches read only what changed).

## 3. This is me, in your menu bar (30 s)

- The status item exactly as it draws right now, with Pip's sentence ("2 agents working", "All quiet"), then the five poses: needs you, finished, keeps failing, going fast, limit reached.
- Say: "Click me for the details; right-click for Settings and Quit." **Continue**.

## 4. Three choices (30 s)

All three start on: needs-you alerts, Claude usage from your account (live 5-hour and weekly limits, the same numbers as claude.ai), open at login. Leave them. **Start**. macOS asks once for the Keychain login Claude Code saved: **Always Allow**. Point at Pip in the bar: it looks up.

## 5. A day with Pip (3 min)

In the terminal, start `claude` with a task that will need a permission, for example "run the test suite and fix what fails".

- Pip's arm types: an agent is working, and only while it writes.
- When Claude asks for permission, Pip waves within about a second and the repo name appears beside it. "You look up, not at the terminal."
- Click Pip: the terminal comes forward. Approve the permission.
- Left-click Pip for the menu: **Needs You** on top when something waits, then the plan cards, live sessions, Insights, Usage. Point at the session row › for its detail (status, context bar, last steps, Go to Terminal, Open Replay).
- When Claude finishes: arms up. A command failing three times: X eyes.
- Right-click Pip: Open AgentPlay, All Sessions, Refresh, Settings, Quit.

## 6. What Pip knows (1 min)

Insights · Last 30 Days:

- **Run Health** › the five parts as bars and the change against the 30 days before.
- **How you steer** is the card at the top of Insights: corrections and frustration from your own messages, with a sentence saying what kind they mostly are; no text is kept. **Steering Details** › for the four rates.
- A **Fix** row › what happened, why, what the fix gets you; **Apply This Fix…** says in its subtitle exactly what it will write, previews the removed and added lines, and can be undone from Run Health. A hook that answers the wrong event is fixed in Claude Code's own settings.json. Show the preview; cancel or apply.
- **Usage Details** › the 14-day chart, by model, by project. Costs are estimates at list prices.

## 7. Close (30 s)

Settings (⌘,) → Privacy: local only, no account, one network call (Claude usage from your account), offered at setup and declined in one click. "Open source, Apache-2.0, part of agentplay."

## If something goes wrong

| Symptom | Do |
|---|---|
| Pip is not in the bar | A hiding utility folded it: click its chevron, or pause the utility |
| Pip does not wave when Claude waits | Needs-you is Claude Code only, from a terminal session (editor panels have no live record). Codex has no waiting record |
| No notification banner | macOS has it off: System Settings → Notifications → AgentPlay Companion |
| Old state wanted back | Quit, then `defaults import ai.entelligence.agentplay.companion <backup plist>` and `mv ~/.agentplay ~/.agentplay.demo-run && mv ~/.agentplay.pre-demo ~/.agentplay` |
