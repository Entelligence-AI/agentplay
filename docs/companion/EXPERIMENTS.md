# Experiments: prototypes, measurements, user test

Date: 2026-10-23. Machine: Apple silicon MacBook (arm64), macOS 27, Swift 6.4 Command Line Tools.

## 1. Three prototypes, one data model

All three directions run in the same app against the same `agentplay.companion/v1` snapshot. A dev build switches between them (`scripts/companion-dev.sh --fixture two-active --direction A|B|C`, or Settings → General → Prototype direction).

| Direction | Screens (light / dark) | Clip |
|---|---|---|
| A · Pulse | [light](screens/proto-A-pulse-light.png) · [dark](screens/proto-A-pulse-dark.png) | [A-pulse-light.mp4](media/A-pulse-light.mp4) · [dark](media/A-pulse-dark.mp4) |
| B · Breakpoint | [card](screens/proto-B-breakpoint-light.png) · [quiet](screens/proto-B-breakpoint-quiet-dark.png) | [B-breakpoint-light.mp4](media/B-breakpoint-light.mp4) · [dark](media/B-breakpoint-dark.mp4) |
| C · Harness Lab | [light](screens/proto-C-lab-light.png) · [dark](screens/proto-C-lab-dark.png) | [C-lab-light.mp4](media/C-lab-light.mp4) · [dark](media/C-lab-dark.mp4) |
| v1 (chosen) | [Now](screens/v1-now-light.png) · [Limits](screens/v1-limits-dark.png) · [Learn](screens/v1-learn-dark.png) | [v1-tour-light.mp4](media/v1-tour-light.mp4) · [proof](media/proof-light.mp4) |

How the clips were made: `scripts/companion-clips.sh` renders each beat from the real SwiftUI views offscreen (synthetic fixtures, "Sample data" badge visible), holds it, and cross-fades with ffmpeg. They are keyframe clips, not recordings of someone using the app. A live screen recording was not made: the display was in use for a video call during this work, and recording it would have captured that. `scripts/companion-capture.sh` records one when the screen is clear.

The proof clip ends on the real viewer, served from a synthetic store, opened at the exact link the companion builds for the "pnpm test failed 3 times" card (`/?session=local%3Aclaude-code%3Asample-7f3a&at=5&stab=errors`). The viewer lands on the Errors tab with "3× pnpm test in a row" selected and the playhead on step 6 of 13, the first failure.

### Scorecard

Scored 1 to 5 by the implementer, from the renders, from the popover drawn over this Mac's real store (`--render-live`, images kept out of the repo), and from the self-test. This is not a user panel; see section 4.

| Criterion | A · Pulse | B · Breakpoint | C · Harness Lab |
|---|---|---|---|
| Glanceable in 2 s | 4: two run cards with a live dot read at once | 5: one card, or "Nothing needs you" | 2: a sentence to read |
| Useful action within 2 clicks | 5: card → Open replay | 5: See sequence opens the first failure | 4: Review evidence opens the first run |
| Background noise | 3: you have to open it to learn anything | 5: silent until a recorded condition | 5: read-only, no alerts |
| Privacy comprehension | 4: Local badge, provenance chips | 4: no repo in notifications by default | 4: "Observed, not a score" |
| Keyboard | 4: ⌘1-3, ⌘K, ⌘O, Return opens replay | 3: two buttons | 3: two buttons |
| Feasible on real transcripts | 3: Context/Plan block made it 800 pt tall (two screens on a 13" Mac); Claude plan usage is always unavailable, so half the block is "unavailable" | 4: found 1 repeated-failure run in 12 real runs; needed the pending-call fix below | 4: real card: "`python3` failed 12 times in 4 runs", and "passed later in 4 of those 4" |

Decision: **A's shell for Now, B as alert behaviour and the "Worth a look" card, C as the Learn tab.** Data-backed change from the brief's sketch: Context and Plan moved out of Now into a Limits tab, because A rendered at 800 pt with them inline ([frame](screens/proto-A-pulse-light.png)) and Now has to fit one screen. v1 Now renders at about 450 pt.

### What real data changed

Running against 1,855 real sessions (Claude Code, Codex, Cursor, OpenCode) found four things fixtures alone would not have:

1. **A live call looks like a failure.** In a running session the trailing tool call is recorded as `err: true, errClass: no_result` until its result arrives. Counting it would fire "repeated failure" on every long call. Now: on a run with process evidence, it is shown as running, never as an error, and never counted.
2. **A stale map looks like a running call.** A map built mid-call, read 18 minutes later, showed "Running: python · 18m" when the call had long finished. Now the core reports `mapBehindSeconds`, a call counts as running only on a live, current map, and the card says "Replay is 18m behind the transcript. Catching up."
3. **Learn needs recurrence, not volume.** Ranking by raw failure count picked `curl` failing 21 times in 2 runs. Ranking by number of runs first picked `python3`, 12 failures in 4 runs, which is the pattern worth seeing.
4. **Codex reports conflicting plans.** Two local Codex sessions carried different `limit_id`/`plan_type` (`codex`/`prolite` with windows, `premium`/`free` with none). The fixture `conflicting-plans` and the low-confidence path come from this.

## 2. Launch testing the real bundle

The `.app` was launched with `open` (not from Xcode) and observed through `pgrep`, `ps`, `/usr/bin/log stream` on the `ai.entelligence.agentplay.companion` subsystem, and `CGWindowListCopyWindowInfo`.

| Check | Result |
|---|---|
| First launch opens onboarding | Yes: one 440 × 450 window on screen, prefs domain created |
| Status item visible | Not confirmed visually: this Mac's menu bar is collapsed behind an overflow chevron, and on macOS 27 status items are drawn by the system, not in the app's own window list |
| Refresh under continuous transcript writes | **Bug found and fixed.** Before: 20 schedules in 40 s, 0 refreshes (each event restarted the debounce). After: about one refresh a minute, 3 schedules in 3 min |
| SIGTERM with a snapshot child running | **Bug found and fixed.** Before: the child outlived the app. After: no app, viewer or snapshot process left |
| Viewer left by a crash | Reaped on next launch through its PID file, only when the command is this app's own Python (self-test `leftoverReaped: true, leftoverGone: true`) |
| Scan stalled with 0% CPU, blocked in `open()` | macOS Files and Folders gate: the builder looks inside repos under `~/Documents`, and a new app's first access waits for the user's answer. Now: onboarding says so, and a stalled scan shows a hint with a link to the setting |

## 3. Measurements

All measured on this machine; none are targets.

| What | Measured |
|---|---|
| Full build of 60 days of sessions (`agentplay build --since 60d`) | 1,807 found, 1,798 built in 35.0 s |
| `companion snapshot --json` (repo venv, 490 root sessions, 12 runs) | 0.56 s wall, 385 ms inside |
| Same, bundled Python, scrubbed environment (`env -i`) | 1.63 s cold, 0.27 s warm |
| Same, through the app's `SnapshotService` (self-test) | 560 ms |
| Scoped refresh `--refresh --agents claude-code,codex`, quiet machine | 3.5 s |
| Same, machine under load, one 34 MB live transcript | 13 to 21 s: discovery walk 7.2 to 9.6 s, rebuilding that 9,123-event session 10.4 s, this 18.7 MB session 0.29 s |
| Viewer start (`agentplay view --no-open --no-build --port 0`) | 74 ms to the start line |
| App process, idle, popover closed | 18 to 53 MB RSS, 0.3% CPU average over 3 min (36 samples) |
| Snapshot child busy, before the adaptive gap | 11 of 36 samples (about 30%) while two agents were writing |
| App bundle | 44 MB (Python runtime 52 MB copied, 41 MB after pruning and precompiling); DMG 19 MB |

The refresh cost is the companion's biggest known cost, and it lives in the shared core: discovery reads the head of every transcript on each run, and a changed session is re-parsed from the start. In the app it is bounded: refreshes only follow file events, a queued refresh is never pushed back, the gap is the larger of 60 s (20 s while the popover is open) and 4× the last refresh, tripled in Low Power Mode, and the child runs at utility QoS. The duty cycle after this change was not re-measured on real data: the re-run hit the Files and Folders gate above, during a call, and I did not want a permission prompt on screen. Core follow-ups that would remove most of the cost: cache the discovery index between runs keyed by directory mtime, and parse Claude transcripts from the last offset.

## 4. User test (not run yet)

No developer sessions were run. The protocol below is ready; nothing in this repo claims results from it.

Participants: five or more Mac developers who use at least one supported agent, with consent to observe and take notes. No analytics are collected; notes are written by hand.

Setup: a fresh user account or `scripts/companion-uninstall.sh` first. Hand them the DMG. Say nothing about the app.

| # | Task | Success when | Watch for |
|---|---|---|---|
| 1 | Install without Terminal and find a previous run | App in the menu bar, a run card visible | Gatekeeper wording, the Files and Folders prompt |
| 2 | Find the latest recorded failed command in a real session | They name it, then open it with Jump to last error | Do they find the micro timeline? |
| 3 | Is context usage recorded for your agent? How is that different from your plan allowance? | They explain tokens without a limit vs plan unavailable | Do they read "142k tokens" as a percentage? |
| 4 | Turn on an alert, mute it, then turn notifications off entirely | Preset on, mute from the card, all presets off | When macOS asks for permission |
| 5 | Open the full viewer at the same event | Viewer shows the same command at the playhead | Do they notice it is 127.0.0.1? |

Record per task: completed (y/n), time, wrong turns, the exact labels that confused them. Five people can find confusing labels and broken flows; they cannot show market fit, and the write-up should not suggest it. Success question to ask on day five: did you open it again because it helped you debug or improve a run?
