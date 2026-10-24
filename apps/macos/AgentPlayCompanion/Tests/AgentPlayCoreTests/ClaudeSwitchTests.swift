import Foundation
import Testing
@testable import AgentPlayCore

@Suite struct ClaudeSwitchTests {
    let now = Date(timeIntervalSince1970: 1_792_800_000)

    func login(_ token: String, refreshExpiresIn: TimeInterval, mcp: String) -> Data {
        let o: [String: Any] = ["claudeAiOauth": ["accessToken": token, "refreshToken": "r-\(token)",
                                                  "expiresAt": (now.timeIntervalSince1970 - 60) * 1000,
                                                  "refreshTokenExpiresAt": (now.timeIntervalSince1970 + refreshExpiresIn) * 1000],
                                "mcpOAuth": ["server": mcp]]
        return try! JSONSerialization.data(withJSONObject: o)
    }

    @Test func switchingKeepsThisMacsMCPLogins() throws {
        let target = login("B", refreshExpiresIn: 86400, mcp: "stale-copy")
        let live = login("A", refreshExpiresIn: 86400, mcp: "live")
        let out = try #require(ClaudeSwitch.merged(target: target, live: live))
        let obj = try #require(JSONSerialization.jsonObject(with: out) as? [String: Any])
        #expect((obj["claudeAiOauth"] as? [String: Any])?["accessToken"] as? String == "B")
        #expect((obj["mcpOAuth"] as? [String: Any])?["server"] as? String == "live")
    }

    @Test func onlyTheAccountBlockOfClaudeJSONChanges() throws {
        let cj = #"{"numStartups":7,"oauthAccount":{"emailAddress":"a@example.com"},"projects":{"/p":{"allowedTools":[]}}}"#
        let out = try #require(ClaudeSwitch.withAccount(Data(cj.utf8), oauthAccount: Data(#"{"emailAddress":"b@example.org","accountUuid":"u2"}"#.utf8)))
        let obj = try #require(JSONSerialization.jsonObject(with: out) as? [String: Any])
        #expect((obj["oauthAccount"] as? [String: Any])?["emailAddress"] as? String == "b@example.org")
        #expect(obj["numStartups"] as? Int == 7 && obj["projects"] != nil)
    }

    @Test func aDeadRefreshTokenIsNotUsable() {
        #expect(ClaudeSwitch.isUsable(login("A", refreshExpiresIn: 3600, mcp: ""), now: now))
        #expect(!ClaudeSwitch.isUsable(login("A", refreshExpiresIn: -1, mcp: ""), now: now))
        #expect(!ClaudeSwitch.isUsable(Data("{}".utf8), now: now))
    }

    @Test func securityLineQuotesJSON() {
        let line = ClaudeSwitch.securityLine(service: "S", account: "a", label: "me@example.com", value: #"{"k":"v \"q\" \\ x"}"#)
        #expect(line == #"add-generic-password -U -s "S" -a "a" -l "me@example.com" -w "{\"k\":\"v \\\"q\\\" \\\\ x\"}""# + "\n")
    }
}

/// End to end on throwaway Keychain items and a temp home: save A, sign in as B, save B, switch back to A.
@Suite(.serialized) struct ClaudeSwitchKeychainTests {
    @Test func saveAndSwitchRoundTrip() throws {
        let tag = UUID().uuidString.prefix(8)
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("cs-\(tag)")
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        let env = ClaudeSwitch.Env(home: home, claudeService: "AgentPlay-test-claude-\(tag)",
                                   storeService: "AgentPlay-test-store-\(tag)", user: "tester")
        defer {
            for args in [["-s", env.claudeService, "-a", "tester"], ["-s", env.storeService, "-a", "uA"], ["-s", env.storeService, "-a", "uB"]] {
                _ = ClaudeSwitch.runSecurity(["delete-generic-password"] + args)
            }
            try? FileManager.default.removeItem(at: home)
        }
        let now = Date()
        func login(_ t: String, mcp: String) -> String {
            String(decoding: try! JSONSerialization.data(withJSONObject: [
                "claudeAiOauth": ["accessToken": t, "refreshToken": "r\(t)", "refreshTokenExpiresAt": (now.timeIntervalSince1970 + 86400) * 1000],
                "mcpOAuth": ["srv": mcp]], options: [.sortedKeys]), as: UTF8.self)
        }
        func signIn(_ uuid: String, _ email: String, token: String, mcp: String) throws {
            _ = ClaudeSwitch.runSecurity(["-i"], stdin: ClaudeSwitch.securityLine(service: env.claudeService, account: "tester", label: nil, value: login(token, mcp: mcp)))
            try Data(#"{"numStartups":1,"oauthAccount":{"accountUuid":"\#(uuid)","emailAddress":"\#(email)"}}"#.utf8)
                .write(to: home.appendingPathComponent(".claude.json"))
        }
        try signIn("uA", "a@example.com", token: "tokA", mcp: "m1")
        let a = try ClaudeSwitch.saveCurrent(env).get()
        try signIn("uB", "b@example.com", token: "tokB", mcp: "m2")
        _ = try ClaudeSwitch.saveCurrent(env).get()

        #expect(ClaudeSwitch.switchTo(a, liveBusy: true, env: env, backups: home.appendingPathComponent("bk")) == .failure(.busy))
        _ = try ClaudeSwitch.switchTo(a, liveBusy: false, env: env, backups: home.appendingPathComponent("bk")).get()

        let live = try #require(ClaudeSwitch.liveLogin(env)?.data)
        let obj = try #require(JSONSerialization.jsonObject(with: live) as? [String: Any])
        #expect((obj["claudeAiOauth"] as? [String: Any])?["accessToken"] as? String == "tokA")
        #expect((obj["mcpOAuth"] as? [String: Any])?["srv"] as? String == "m2")  // this Mac's MCP logins kept
        #expect(ClaudeAccount.readIdentity(home: home)?.email == "a@example.com")
        // B's login was saved on the way out, and the old ~/.claude.json was backed up.
        #expect(ClaudeSwitch.savedPayload("uB", env) != nil)
        #expect((try? FileManager.default.contentsOfDirectory(atPath: home.appendingPathComponent("bk").path).count) == 1)
    }
}

@Suite struct ClaudeLoginScriptTests {
    @Test func loginScriptSignsInWithoutLoggingOut() {
        let s = ClaudeSwitch.loginScript(claude: "/Users/me/it's here/claude")
        #expect(s.contains(#"'/Users/me/it'\''s here/claude' auth login --claudeai"#))
        #expect(!s.contains("logout"))
    }
}

@Suite struct CompanionCommandTests {
    @Test func readsOkAndFailureReplies() {
        let ok = CompanionCommand.parse(Data(#"{"ok": true, "entryId": "abc", "file": "CLAUDE.md", "granted": false}"#.utf8))
        #expect(ok.ok && ok.fields["entryId"] == "abc" && ok.fields["granted"] == "false")
        let bad = CompanionCommand.parse(Data(#"{"ok": false, "code": "not_granted", "message": "AgentPlay may not write to x yet."}"#.utf8))
        #expect(!bad.ok && bad.message == "AgentPlay may not write to x yet.")
        #expect(!CompanionCommand.parse(Data("oops".utf8)).ok)
    }
}
