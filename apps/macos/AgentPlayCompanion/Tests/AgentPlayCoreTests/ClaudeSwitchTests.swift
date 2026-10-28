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

        let bk = home.appendingPathComponent("bk")
        let beforeSwitch = try Data(contentsOf: home.appendingPathComponent(".claude.json"))
        #expect(ClaudeSwitch.switchTo(a, liveBusy: true, env: env, backups: bk, now: now) == .failure(.busy))
        // The backup is written before anything else: if it can't be, nothing is touched.
        let noDir = home.appendingPathComponent(".claude.json/bk")
        #expect(ClaudeSwitch.switchTo(a, liveBusy: false, env: env, backups: noDir, now: now) == .failure(.backup(noDir.path)))
        #expect(ClaudeAccount.readIdentity(home: home)?.email == "b@example.com")
        _ = try ClaudeSwitch.switchTo(a, liveBusy: false, env: env, backups: bk, now: now).get()

        let live = try #require(ClaudeSwitch.liveLogin(env)?.data)
        let obj = try #require(JSONSerialization.jsonObject(with: live) as? [String: Any])
        #expect((obj["claudeAiOauth"] as? [String: Any])?["accessToken"] as? String == "tokA")
        #expect((obj["mcpOAuth"] as? [String: Any])?["srv"] as? String == "m2")  // this Mac's MCP logins kept
        #expect(ClaudeAccount.readIdentity(home: home)?.email == "a@example.com")
        // B's login was saved on the way out, and the old ~/.claude.json was backed up as it was.
        #expect(ClaudeSwitch.savedPayload("uB", env) != nil)
        let backups = try FileManager.default.contentsOfDirectory(atPath: bk.path)
        #expect(backups.count == 1)
        #expect(try Data(contentsOf: bk.appendingPathComponent(backups[0])) == beforeSwitch)

        // Switching back to B when ~/.claude.json can't be written puts A's login back, so the two stores agree.
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: home.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: home.path) }
        let b = ClaudeSwitch.Saved(accountUuid: "uB", email: "b@example.com", organization: nil, seatTier: nil, savedAt: now)
        #expect(ClaudeSwitch.switchTo(b, liveBusy: false, env: env, backups: bk, now: now.addingTimeInterval(1)) == .failure(.claudeJSON))
        let after = try #require(ClaudeSwitch.liveLogin(env)?.data)
        #expect(((try? JSONSerialization.jsonObject(with: after) as? [String: Any])?["claudeAiOauth"] as? [String: Any])?["accessToken"] as? String == "tokA")
        #expect(ClaudeAccount.readIdentity(home: home)?.email == "a@example.com")
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: home.path)

        // Forgetting removes the saved login; forgetting again is still fine, the login is gone either way.
        try ClaudeSwitch.forget("uB", env).get()
        #expect(ClaudeSwitch.savedPayload("uB", env) == nil)
        try ClaudeSwitch.forget("uB", env).get()
    }

    /// A ~/.claude.json that can't be written leaves the login alone; a symlinked one stays a symlink; forget reports.
    @Test func failedOrLinkedClaudeJSONAndForget() throws {
        let tag = UUID().uuidString.prefix(8)
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("cs-\(tag)")
        let dots = FileManager.default.temporaryDirectory.appendingPathComponent("cs-dots-\(tag)")
        for d in [home, dots] { try FileManager.default.createDirectory(at: d, withIntermediateDirectories: true) }
        let env = ClaudeSwitch.Env(home: home, claudeService: "AgentPlay-test-claude-\(tag)",
                                   storeService: "AgentPlay-test-store-\(tag)", user: "tester")
        defer {
            try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: home.path)
            for args in [["-s", env.claudeService, "-a", "tester"], ["-s", env.storeService, "-a", "uA"], ["-s", env.storeService, "-a", "uB"]] {
                _ = ClaudeSwitch.runSecurity(["delete-generic-password"] + args)
            }
            for d in [home, dots] { try? FileManager.default.removeItem(at: d) }
        }
        let now = Date()
        func signIn(_ uuid: String, _ email: String, token: String) throws {
            let login = String(decoding: try JSONSerialization.data(withJSONObject: [
                "claudeAiOauth": ["accessToken": token, "refreshToken": "r\(token)", "refreshTokenExpiresAt": (now.timeIntervalSince1970 + 86400) * 1000]],
                options: [.sortedKeys]), as: UTF8.self)
            _ = ClaudeSwitch.runSecurity(["-i"], stdin: ClaudeSwitch.securityLine(service: env.claudeService, account: "tester", label: nil, value: login))
            try Data(#"{"oauthAccount":{"accountUuid":"\#(uuid)","emailAddress":"\#(email)"}}"#.utf8)
                .write(to: home.appendingPathComponent(".claude.json").resolvingSymlinksInPath())
        }
        func liveToken() -> String? {
            let o = ClaudeSwitch.liveLogin(env).flatMap { try? JSONSerialization.jsonObject(with: $0.data) as? [String: Any] }
            return (o?["claudeAiOauth"] as? [String: Any])?["accessToken"] as? String
        }
        try signIn("uA", "a@example.com", token: "tokA")
        let a = try ClaudeSwitch.saveCurrent(env).get()
        try signIn("uB", "b@example.com", token: "tokB")
        _ = try ClaudeSwitch.saveCurrent(env).get()

        // A home that can't be written: the switch fails and Claude Code's login is still B's.
        try FileManager.default.setAttributes([.posixPermissions: 0o555], ofItemAtPath: home.path)
        #expect(ClaudeSwitch.switchTo(a, liveBusy: false, env: env, backups: dots.appendingPathComponent("bk")) == .failure(.claudeJSON))
        #expect(liveToken() == "tokB")
        #expect(ClaudeAccount.readIdentity(home: home)?.email == "b@example.com")
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: home.path)

        // A dotfiles-style symlink: the switch writes its target and the link survives.
        let link = home.appendingPathComponent(".claude.json"), target = dots.appendingPathComponent("claude.json")
        try FileManager.default.moveItem(at: link, to: target)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: target)
        _ = try ClaudeSwitch.switchTo(a, liveBusy: false, env: env, backups: dots.appendingPathComponent("bk")).get()
        #expect(liveToken() == "tokA")
        #expect((try? FileManager.default.destinationOfSymbolicLink(atPath: link.path)) == target.path)
        #expect(ClaudeAccount.readIdentity(home: home)?.email == "a@example.com")

        // Forget succeeds once the saved login is gone; one that is already gone counts as gone.
        try ClaudeSwitch.forget("uA", env).get()
        #expect(ClaudeSwitch.savedPayload("uA", env) == nil && ClaudeSwitch.savedPayload("uB", env) != nil)
        try ClaudeSwitch.forget("uA", env).get()
    }
}

@Suite struct ClaudeLoginScriptTests {
    @Test func loginScriptSignsInWithoutLoggingOut() throws {
        let s = ClaudeSwitch.loginScript(claude: "/Users/me/it's here/claude")
        #expect(s.contains(#"if '/Users/me/it'\''s here/claude' auth login --claudeai; then"#))
        #expect(!s.contains("logout"))
        // "Done" belongs to the success branch only; a failed sign-in says so and exits nonzero.
        let done = try #require(s.range(of: "Done.")), then = try #require(s.range(of: "; then")), other = try #require(s.range(of: "else"))
        #expect(then.lowerBound < done.lowerBound && done.lowerBound < other.lowerBound)
        #expect(s.contains("exit 1"))
        // Only a sign-in that worked closes the window, and only the window on this script's own terminal.
        let close = try #require(s.range(of: "close window id wid"))
        #expect(then.lowerBound < close.lowerBound && close.lowerBound < other.lowerBound)
        #expect(s.contains(#"contains \"$TTY\""#) && s.contains("TTY=$(tty)") && s.contains("nohup"))
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
