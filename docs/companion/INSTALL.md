# Install, build and uninstall

## For users (once a notarized release exists)

1. Download `AgentPlay-Companion-<version>-arm64.dmg` from the GitHub release, and check it against the `.sha256` next to it: `shasum -a 256 -c AgentPlay-Companion-<version>-arm64.dmg.sha256`.
2. Open it and drag **AgentPlay Companion** to **Applications**.
3. Open it from Applications. macOS confirms once that it came from the internet. The AgentPlay mark appears in the menu bar and a short welcome opens.

No Terminal, Python, uv or account is needed: the app carries its own Python runtime and the AgentPlay core.

Requirements: macOS 14 or later, Apple silicon. An Intel (x86_64) build needs a second runtime; see RELEASE_CHECKLIST.md.

Homebrew (after a release): `brew install --cask agentplay-companion` once the cask in RELEASE_CHECKLIST.md is published.

If macOS asks "AgentPlay Companion would like to access files in your Documents folder": that's because your repos live there. It looks there only to tell which repo a session worked in. Declining keeps it out; you can change it later in System Settings → Privacy & Security → Files and Folders. Please don't disable Gatekeeper to install any build of this app.

**Today, no notarized build exists** (no Apple Developer ID was available while building this). Until one does, use the team preview below.

## Team preview (before notarization)

For people you trust, on Apple silicon Macs. One person builds the DMG; everyone else runs one line.

Whoever builds (needs the developer setup below):

```sh
sh scripts/companion-dmg.sh      # ends with "BLOCKED: not notarized"; the DMG in dist/companion is still good for the team
```

Share `dist/companion/AgentPlay-Companion-<version>-arm64.dmg` (Slack, Drive, or a GitHub draft release).

Everyone else, in Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/Entelligence-AI/agentplay/main/scripts/companion-install.sh | sh -s -- ~/Downloads/AgentPlay-Companion-<version>-arm64.dmg
```

It copies the app into Applications, clears the "downloaded from the internet" flag that stops an un-notarized app from opening, and opens it. The welcome window does the rest. Updating is the same line with the new DMG.

## For developers

```sh
make setup                     # Python venv and viewer deps (once)
make companion-dev             # build the dev app and open it on the synthetic sample
sh scripts/companion-dev.sh    # the dev app on your real sessions (first run shows onboarding)
make companion-test            # Python contract + leak tests, Swift tests, fixtures, guards, bundle self-test
make companion-app             # release .app in dist/companion (ad-hoc signed)
make companion-dmg             # DMG; signs and notarizes only with credentials, see RELEASE_CHECKLIST.md
```

Build requirements: Swift 6 (Xcode, or Command Line Tools), `uv` (fetches python-build-standalone 3.12), `pnpm` (builds the viewer), `rsync`. With Command Line Tools only, the build script uses the macOS 26.x SDK it finds, because the 27 SDK's SwiftUI macros aren't in CLT. With Xcode, any current SDK works.

Dev-build flags (`--args` after `open`, ignored by release builds):

| Flag | Does |
|---|---|
| `--fixture <name>` | run on a synthetic snapshot, labelled Sample data |
| `--direction v1\|A\|B\|C` | switch prototype |
| `--skip-onboarding` | mark onboarding done |
| `--selftest` | end-to-end check of the bundle, prints JSON, exits |
| `--render <dir>` | draw every state, light and dark, from the bundled fixtures (or `AGENTPLAY_COMPANION_FIXTURES`) |
| `--render-live <dir>` | the popover over your real snapshot; contains your repo and file names, never commit |
| `--film <take>\|all --film-out <dir>` | records scripted takes for a launch video: the real menu bar item, menu, sheets and windows over `Fixtures/film.json` on a clean backdrop, the pointer driven by synthesized events, each take saved by `screencapture`. Every read of this Mac is off while filming. Needs a visible menu bar (no full-screen app on the filming display) and the pointer for about three minutes. `--film-slot N` pins the item N pt from the bar's right end, `--film-screen N` picks a display, `--no-record` rehearses |

Point it at another store with `AGENTPLAY_HOME`, for example `open -n --env AGENTPLAY_HOME=/tmp/ap "dist/companion/AgentPlay Companion.app"`.

### What the build does

`scripts/companion-build.sh`:

1. Builds the viewer into the Python package if it isn't there (`make viewer`).
2. `swift build -c release` for `AgentPlayCompanion`.
3. Assembles `AgentPlay Companion.app`: the binary, `Info.plist` (`LSUIElement`, bundle id `ai.entelligence.agentplay.companion`, version from `python/pyproject.toml`), icon, privacy text, the sample fixture (all fixtures in dev).
4. Copies python-build-standalone 3.12 into `Contents/Resources/python`, removes what transcripts and the viewer don't need (tests, tkinter, idle, ensurepip, static libs: 52 MB to 41 MB), installs `agentplay` into its `site-packages`, and precompiles bytecode so nothing is written into the signed bundle.
5. Signs every Mach-O inside out, then the app. With `CODESIGN_IDENTITY` set: hardened runtime, timestamp, `Resources/Companion.entitlements` (empty: no exceptions).

Result: 44 MB app, 19 MB DMG, about 1.6 s for the first snapshot after launch and 0.3 s after that.

## Uninstall

From a terminal: `sh scripts/companion-uninstall.sh`. By hand:

1. Quit it (right-click Pip → Quit AgentPlay). That stops the viewer it started.
2. Delete `/Applications/AgentPlay Companion.app`. Its login item goes with it.
3. Optional, to remove its settings: `defaults delete ai.entelligence.agentplay.companion` and `rm -rf ~/Library/Caches/ai.entelligence.agentplay.companion`.
4. Notification permission: System Settings → Notifications.

Your session maps in `~/.agentplay` stay, because `uvx agentplay` uses the same store. To remove them too: `sh scripts/companion-uninstall.sh --remove-maps`, or delete `~/.agentplay`.

## Windows and Linux

Unchanged. The companion is macOS only; the CLI and viewer keep working as before, and `agentplay companion snapshot --json` is plain standard-library Python that also runs there (without the `lsof` activity check).
