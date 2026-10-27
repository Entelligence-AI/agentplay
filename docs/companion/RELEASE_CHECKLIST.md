# Release checklist

## Status on 2026-10-23

| Gate | State |
|---|---|
| G0 Repo audit | Done: REPO_AUDIT.md, MARKET_NOTES.md |
| G1 Prototype review | Done with one gap: three working directions, light and dark screens, keyframe clips, scorecard. Live screen recordings not made (EXPERIMENTS.md §1) |
| G2 Core integration | Done: real store, process-backed activity, ordering, deep link to the exact `seq` verified against the running viewer, no invented percentages |
| G3 Alerts and privacy | Done: local notifications, presets off by default, dedupe, cooldown, budget, quiet hours, snooze, mutes, watermark; leak tests; no network unless Claude-account usage is on (offered at setup, one click to decline) |
| G4 Packaging | Dev and release `.app` and DMG build and pass `codesign --verify --deep --strict`. **Public release BLOCKED: no Developer ID identity, no notarization** |
| G5 Pilot-ready | Not yet: needs a notarized build and the five-developer test |

## Blocked: Developer ID signing and notarization

Nothing here is notarized. `security find-identity -v -p codesigning` reports 0 identities on the build machine, and `scripts/companion-dmg.sh` refuses to call a DMG notarized without the checks below passing.

To unblock (an Apple Developer Program account for Entelligence AI):

1. Create a **Developer ID Application** certificate and install it in the build machine's login keychain.
2. Create an app-specific password for the Apple ID, then:
   `xcrun notarytool store-credentials agentplay-notary --apple-id <apple-id> --team-id <TEAMID> --password <app-specific-password>`
3. Build, sign, notarize, staple, verify:
   ```sh
   CODESIGN_IDENTITY="Developer ID Application: Entelligence AI (<TEAMID>)" \
   NOTARY_PROFILE=agentplay-notary \
   sh scripts/companion-dmg.sh
   ```
   The script signs every Mach-O in the embedded runtime with the hardened runtime, submits with `notarytool --wait`, staples, then runs `stapler validate`, `spctl --assess` on the DMG and the app, and `codesign --verify --deep --strict` on the mounted app, and writes the `.sha256`.
4. If notarization reports issues, `xcrun notarytool log <submission-id> --keychain-profile agentplay-notary` lists each binary.

## Before a release

- [ ] `make companion-test` green (Python contract and leak tests, full Python suite, Swift tests, fixtures identical, `check_clean`, bundle self-test)
- [ ] `make test` and `make lint` green: the CLI and viewer are unchanged
- [ ] `scripts/companion-dmg.sh` with credentials ends in "Notarized and stapled"
- [ ] On a second Mac with no developer tools: download the DMG from the release page, install, open, see the welcome, find a run, open the replay at that event
- [ ] Uninstall with `scripts/companion-uninstall.sh` leaves no process and keeps `~/.agentplay`
- [ ] Version: `python/pyproject.toml` (the app uses it); build number is the commit count
- [ ] Release notes (below), DMG and `.sha256` attached to a public GitHub release
- [ ] Homebrew cask PR with the release URL and SHA256

## Universal build (Intel)

Not available yet. `ARCHS="arm64 x86_64" scripts/companion-build.sh release` fails with Command Line Tools: the x86_64 slice can't build the SDK's `Spatial` module. It is untested with Xcode. The runtime is a second problem: python-build-standalone ships one architecture per tree, so a universal app needs either two DMGs (simplest) or the arm64 and x86_64 runtimes merged with `lipo -create` file by file.

## Updates

## Updates (Sparkle)

Installed copies check once a day for a newer release and offer it; nothing installs without a click. The feed is `https://github.com/Entelligence-AI/agentplay/releases/latest/download/appcast.xml`, so publishing a release is publishing the update. Every update is checked against the EdDSA public key built into the app, so a tampered DMG is refused. Off in Settings → General; Check for Updates… on right-click. Dev builds and builds without a public key never check.

## One-time setup (about an hour, then every tag ships itself)

1. **Apple Developer Program** for Entelligence AI ($99 a year). Create a *Developer ID Application* certificate, export it with its key as a `.p12`.
2. **Sparkle keys**, once, on a trusted Mac:
   ```sh
   .build/companion-release/artifacts/sparkle/Sparkle/bin/generate_keys      # prints the public key, stores the private one in the Keychain
   .build/companion-release/artifacts/sparkle/Sparkle/bin/generate_keys -x sparkle-private.key
   ```
   Commit the public key to `apps/macos/AgentPlayCompanion/Resources/sparkle_public_key.txt`. Keep `sparkle-private.key` out of the repo. Losing it means no installed copy can be updated, so store it in the team password manager too.
3. **GitHub secrets** on the repo:

   | Secret | Value |
   |---|---|
   | `DEVELOPER_ID_P12` | `base64 -i cert.p12` |
   | `DEVELOPER_ID_P12_PASSWORD` | the `.p12` password |
   | `NOTARY_APPLE_ID` | the Apple ID email |
   | `NOTARY_TEAM_ID` | the 10-character team id |
   | `NOTARY_PASSWORD` | an app-specific password for that Apple ID |
   | `SPARKLE_PRIVATE_KEY` | the contents of `sparkle-private.key` |

## Shipping a version

Bump `python/pyproject.toml` and `agentplay/__init__.py`, tag `vX.Y.Z`, push the tag. The `release` workflow publishes the Python package and, in the `companion` job, builds, signs, notarizes and staples the DMG, writes `appcast.xml`, and attaches both to the GitHub release. Every installed copy offers the update within a day. Until the secrets exist the job skips with a notice, and the Python release is unaffected.

## Homebrew cask (draft, after a notarized release exists)

```ruby
cask "agentplay-companion" do
  version "<version>"
  sha256 "<sha256 of the DMG>"
  url "https://github.com/Entelligence-AI/agentplay/releases/download/v#{version}/AgentPlay-Companion-#{version}-arm64.dmg"
  name "AgentPlay Companion"
  desc "Menu bar companion for AgentPlay: follow coding-agent runs, jump to the moment"
  homepage "https://github.com/Entelligence-AI/agentplay"
  depends_on macos: ">= :sonoma"
  depends_on arch: :arm64
  app "AgentPlay Companion.app"
  zap trash: [
    "~/Library/Preferences/ai.entelligence.agentplay.companion.plist",
    "~/Library/Caches/ai.entelligence.agentplay.companion",
  ]
end
```

`zap` deliberately leaves `~/.agentplay`: it holds the user's maps, shared with `uvx agentplay`.

## Release notes (draft)

**AgentPlay Companion (preview).** Follow coding-agent runs without staring at logs.

- See which agents are running (from live processes, not guesses) and the last recorded step of each.
- Jump from a run, an error, or a tick on its timeline straight to that moment in the AgentPlay replay.
- Optional local alerts for a command failing three times in a row, and more, all off until you turn them on.
- Context and plan usage only where the agent records them: Codex reports both; Claude Code records tokens but no limit, so the app says that instead of guessing.
- One observed pattern across your recent runs, with its denominator.
- Local by default: no AgentPlay account, no analytics. The only network request is to Anthropic for your Claude usage; setup offers it switched on and you can decline there or turn it off in Settings. Your maps stay in `~/.agentplay`.

Known limitations: Apple silicon only; refreshes take seconds on very large live transcripts and are spaced out to bound CPU; repos under Documents, Desktop or Downloads need a one-time macOS folder permission; Claude Code plan usage is not available locally.
