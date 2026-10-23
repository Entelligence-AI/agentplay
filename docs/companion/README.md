# AgentPlay Companion

A native macOS menu-bar companion for AgentPlay: follow coding-agent runs without staring at logs, and jump from a small alert to the exact recorded moment in the replay. Local only, no account.

![Now, light](screens/v1-now-light.png)

| Doc | What's in it |
|---|---|
| [REPO_AUDIT.md](REPO_AUDIT.md) | What main, PR #19 and the referenced issues provide, and what implementing revealed |
| [MARKET_NOTES.md](MARKET_NOTES.md) | Bloom, CodexBar, UsageNow, Apple platform rules, provider quota sources |
| [EXPERIMENTS.md](EXPERIMENTS.md) | Three prototypes, scorecard and decision, launch testing, measurements, user-test protocol |
| [UX_STATES.md](UX_STATES.md) | Every state with its copy and screen, keys, motion |
| [DATA_PROVENANCE.md](DATA_PROVENANCE.md) | The DataPoint contract, each source, provider adapters, fixtures |
| [PRIVACY.md](PRIVACY.md) | What it reads, never shows, writes and runs, and the tests behind each |
| [ACCOUNT_INTEGRATION.md](ACCOUNT_INTEGRATION.md) | Why there's no sign-in yet, and the backend contract one would need |
| [INSTALL.md](INSTALL.md) | Install, build, dev flags, uninstall |
| [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) | Gate status, the signing and notarization steps (blocked today), cask and release notes |

Code: `apps/macos/AgentPlayCompanion/` (SwiftUI app and `AgentPlayCore`), `python/src/agentplay/companion.py` (the read-only snapshot), `schema/agentplay-companion-v1.schema.json`, `scripts/companion-*`.
