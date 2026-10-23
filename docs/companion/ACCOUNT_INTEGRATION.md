# Account integration

Status: **not implemented, on purpose.** The app ships a visible, disabled "Connect Entelligence" entry with the reason. It never shows a login form, never pretends to sign anyone in, and every feature works signed out and offline.

## What exists today

- `python/src/agentplay/account.py` is an **email newsletter opt-in**, not an account. It stores `~/.agentplay/account.json` (`status`, `email`, a random `installId`, `at`) and posts the address to the endpoint in `docs/signup-endpoint.md` only after the user submits it. There is no password, token, session or verified identity.
- It is off whenever `DO_NOT_TRACK` or `AGENTPLAY_NO_NETWORK` is set. The companion sets both for its children, so the viewer it opens doesn't offer the newsletter. The app says so in Settings → Account and in onboarding step 5. Users who want the newsletter get it from `uvx agentplay`.
- This repo has no Entelligence identity provider, OAuth client, token endpoint or user API. None was found in the code, docs or issues on 2026-10-23. Turning the newsletter email into a "login" would be deceptive, so it isn't done.

## Contract a real backend must offer before the app connects

Identity, per OAuth 2.1 for native apps (RFC 8252) with OIDC:

| Need | Requirement |
|---|---|
| Authorization endpoint | Browser-based (system browser via `ASWebAuthenticationSession`), never an embedded password field |
| PKCE | S256 `code_challenge`, verifier never leaves the device |
| `state`, `nonce` | Random per attempt; `state` checked on callback, `nonce` checked in the ID token |
| Redirect | Claimed universal link (`https://…/companion/callback` with an `apple-app-site-association` entry) or a custom scheme registered by the app; callback origin and expiry validated |
| Tokens | Short-lived access token, rotating refresh token; stored only in the macOS Keychain (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`), never in `account.json` or preferences |
| Revocation | `POST /oauth/revoke`; on Disconnect the app revokes, deletes the Keychain items, and only then shows "Disconnected" |
| Discovery | `/.well-known/openid-configuration` |

Data endpoints, each needing its own consent screen the first time:

| Purpose | Endpoint | Data class | Default |
|---|---|---|---|
| Sync preferences | `GET/PUT /v1/companion/settings` | presets, thresholds, quiet hours, pattern keys (hashes) | off |
| Team recipes | `GET /v1/teams/{id}/recipes` | read-only shared alert presets | off |
| Redacted run cards | `POST /v1/run-cards` | one user-chosen card: agent, command family, counts, times; no repo, path or id unless chosen | off, per card |
| Delete synced data | `DELETE /v1/companion/data` | everything above | user action |
| Export settings | `GET /v1/companion/settings/export` | settings | user action |

Never uploaded, under any setting: transcripts, prompts, commands with arguments, repo identifiers, maps, map exports.

Failure rules: a deletion is shown as done only after a 2xx and a follow-up read that confirms it; a failed request leaves local state unchanged and says so. Offline, the account section shows "Can't reach Entelligence" and everything else keeps working.

## Integration tests to write with the backend

- Sign in with PKCE against a test issuer; tampered `state` rejected; replayed code rejected; expired callback rejected.
- Token refresh after expiry; refresh failure signs out locally without touching local data.
- Disconnect revokes and clears Keychain; a revoke failure is reported and retried, not hidden.
- Delete synced data: 2xx then confirmed empty; 500 shown as not deleted.
- Every local feature with the account signed out, offline, and with the backend returning 5xx.
- No request carries a session id, path, repo name or prompt (a test proxy asserting on bodies).

## Where it plugs in

`AccountPane` (Settings) and onboarding step 5 already hold the entry points. The implementation would add an `AccountService` in `AgentPlayCore` behind a feature flag that stays off until the contract above exists and passes the tests.
