import Foundation
import Testing
@testable import AgentPlayCore

@Suite struct ClaudeAccountTests {
    @Test func readsClaudeCodesCredentialFormat() throws {
        let json = #"{"claudeAiOauth":{"accessToken":"test-access-token","refreshToken":"r","expiresAt":1792800000000,"subscriptionType":"max"}}"#
        let c = try #require(ClaudeAccount.parseCredentials(Data(json.utf8)))
        #expect(c.accessToken == "test-access-token" && c.plan == "max")
        #expect(c.expiresAt == Date(timeIntervalSince1970: 1_792_800_000))
        #expect(ClaudeAccount.parseCredentials(Data("{}".utf8)) == nil)
    }

    @Test func picksTheUsersLiveLoginOverAStaleOne() {
        let old = Date(timeIntervalSince1970: 1_789_000_000), new = Date(timeIntervalSince1970: 1_792_800_000)
        let items = [
            ClaudeAccount.KeychainItem(service: "Claude Code-credentials", account: "unknown", modified: old),
            ClaudeAccount.KeychainItem(service: "Claude Safe Storage", account: "me", modified: new),
            ClaudeAccount.KeychainItem(service: "Claude Code-credentials-1a2b", account: "other", modified: new),
            ClaudeAccount.KeychainItem(service: "Claude Code-credentials", account: "me", modified: old),
        ]
        let ordered = ClaudeAccount.order(items, user: "me")
        #expect(ordered.map(\.account) == ["me", "other", "unknown"])
    }

    @Test func readsWhoClaudeCodeIsSignedInAs() throws {
        let json = #"{"numStartups":3,"oauthAccount":{"accountUuid":"e2a","emailAddress":"me@example.com","organizationName":"example","seatTier":"team","displayName":"Me"},"projects":{}}"#
        let id = try #require(ClaudeAccount.parseIdentity(Data(json.utf8)))
        #expect(id.line == "me@example.com · example" && id.accountUuid == "e2a" && id.seatTier == "team")
        #expect(ClaudeAccount.parseIdentity(Data(#"{"projects":{}}"#.utf8)) == nil)
    }

    @Test func parsesUsageWindows() throws {
        let json = #"{"five_hour":{"utilization":67.0,"resets_at":"2026-10-24T15:00:00.123456+00:00"},"seven_day":{"utilization":23,"resets_at":"2026-10-28T09:00:00Z"},"seven_day_opus":null}"#
        let now = Date(timeIntervalSince1970: 1_792_800_000)
        let rows = ClaudeAccount.parseUsage(Data(json.utf8), plan: "max", now: now)
        #expect(rows.map(\.window) == ["five_hour", "weekly"])
        #expect(rows[0].used.value?.usedPercent == 67 && rows[0].used.source == .officialProviderAPI)
        #expect(rows[0].resetsAt != nil && rows[1].resetsAt != nil)
        #expect(rows[0].plan == "max" && rows[0].used.observedAt == now)
    }

    @Test func requestCarriesTheBetaHeaderAndNoBody() {
        let r = ClaudeAccount.request(token: "t", claudeVersion: "2.1.309")
        #expect(r.url?.absoluteString == "https://api.anthropic.com/api/oauth/usage")
        #expect(r.value(forHTTPHeaderField: "anthropic-beta") == "oauth-2025-04-20")
        #expect(r.value(forHTTPHeaderField: "Authorization") == "Bearer t")
        #expect(r.httpMethod == "GET" && r.httpBody == nil)
    }

    @Test func accountRowsReplaceClaudesOthersStay() throws {
        let s = try fixture("two-active")
        let account = ClaudeAccount.parseUsage(Data(#"{"five_hour":{"utilization":5,"resets_at":"2026-10-24T15:00:00Z"}}"#.utf8),
                                               plan: nil, now: Date())
        let merged = ClaudeAccount.merge(s.limits, account: account)
        #expect(merged.filter { $0.provider == "claude-code" } == account)
        #expect(merged.filter { $0.provider == "codex" } == s.limits.filter { $0.provider == "codex" })
        #expect(ClaudeAccount.merge(s.limits, account: []) == s.limits)
    }
}
