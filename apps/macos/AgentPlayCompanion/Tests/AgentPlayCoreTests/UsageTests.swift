import Foundation
import Testing
@testable import AgentPlayCore

@Suite struct PaceTests {
    let now = Date(timeIntervalSince1970: 1_800_000_000)

    @Test func onPaceIsNormalEvenWhenHigh() {
        // 70% used, 75% of the week gone: fine.
        let reset = now.addingTimeInterval(0.25 * 7 * 86400)
        let p = Pace.of(used: 70, windowMinutes: 10080, resetsAt: reset, now: now)
        #expect(p?.level == .normal && p?.runsOutAt == nil)
    }

    @Test func aheadAndOver() {
        // 50% used with 30% of the window gone: 1.67x pace.
        let reset = now.addingTimeInterval(0.7 * 300 * 60)
        let p = Pace.of(used: 50, windowMinutes: 300, resetsAt: reset, now: now)
        #expect(p?.level == .over)
        #expect(p?.runsOutAt != nil && p!.runsOutAt! < reset)
        let q = Pace.of(used: 35, windowMinutes: 300, resetsAt: reset, now: now)  // 1.17x
        #expect(q?.level == .normal)
        let r = Pace.of(used: 40, windowMinutes: 300, resetsAt: reset, now: now)  // 1.33x
        #expect(r?.level == .ahead)
        #expect(Pace.of(used: 91, windowMinutes: 300, resetsAt: now.addingTimeInterval(60), now: now)?.level == .over)
    }

    @Test func noPaceWithoutWindowOrAfterReset() {
        #expect(Pace.of(used: 50, windowMinutes: nil, resetsAt: now.addingTimeInterval(60), now: now) == nil)
        #expect(Pace.of(used: 50, windowMinutes: 300, resetsAt: now.addingTimeInterval(-1), now: now) == nil)
    }
}

@Suite struct ProviderTests {
    @Test func mostConstrainedFirstAndUnavailableLast() throws {
        let s = try fixture("two-active")
        let ps = ProviderSummary.build(s.limits)
        #expect(ps.map(\.provider) == ["claude-code", "codex"])  // 58% weekly beats Codex's 41%
        #expect(ps[0].windows.first?.window == "five_hour")      // nearest reset first
        let stale = ProviderSummary.build(try fixture("stale-quota").limits)
        #expect(stale.last?.windows.isEmpty == true && stale.last?.unavailable != nil)
    }
}

@Suite struct UsageDecoding {
    @Test func usageReportDecodesAndFlagsUnpriced() throws {
        let s = try fixture("two-active")
        let u = try #require(s.usage)
        #expect(u.today.requests > 0 && u.today.costUsd > 0)
        #expect(u.daily.count == 14)
        let slice = UsageSlice(key: "mystery", tokens: .init(input: 1, cacheWrite: 0, cacheRead: 0, output: 1, total: 2),
                               costUsd: 0, requests: 3, unpricedRequests: 3, sessions: 1)
        #expect(slice.isUnpriced)
    }

    @Test func liveSessionsDecode() throws {
        let s = try fixture("two-active")
        let live = try #require(s.live)
        #expect(live.contains { $0.status == .waiting && !$0.inStore })
        #expect(s.runs.first { $0.sessionId == "local:claude-code:sample-7f3a" }?.liveStatus == .busy)
    }

    @Test func moneyFormatting() {
        #expect(Format.money(0.1) == "$0.10")
        #expect(Format.money(79.56) == "$79.56")
        #expect(Format.money(921.4) == "$921")
        #expect(Format.money(1536.7) == "$1,537")
    }
}

@Suite struct BridgeTests {
    func temp() throws -> (ClaudeBridge, URL) {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir.appendingPathComponent(".claude"), withIntermediateDirectories: true)
        let b = ClaudeBridge(claudeSettings: dir.appendingPathComponent(".claude/settings.json"),
                             agentplayHome: dir.appendingPathComponent(".agentplay"))
        return (b, dir)
    }

    func settings(_ b: ClaudeBridge) throws -> [String: Any] {
        try JSONSerialization.jsonObject(with: Data(contentsOf: b.claudeSettings)) as! [String: Any]
    }

    @Test func installKeepsOtherSettingsAndRestoresOnRemoval() throws {
        let (b, _) = try temp()
        let original: [String: Any] = ["model": "opus", "permissions": ["allow": ["Bash(ls)"]],
                                       "statusLine": ["type": "command", "command": "my-line --fancy"]]
        let originalData = try JSONSerialization.data(withJSONObject: original)
        try originalData.write(to: b.claudeSettings)
        #expect(b.state() == .otherStatusLine("my-line --fancy"))
        try b.install(script: "#!/bin/sh\ndir=\"${AGENTPLAY_HOME:-$HOME/.agentplay}/live/claude\"\n")
        #expect(b.state() == .on)
        // Claude Code runs it without AGENTPLAY_HOME: the folder is fixed at install.
        let installed = try String(contentsOf: b.scriptURL, encoding: .utf8)
        #expect(installed.contains("dir='\(b.agentplayHome.path)/live/claude'") && !installed.contains("AGENTPLAY_HOME"))
        var s = try settings(b)
        #expect(s["model"] as? String == "opus")
        #expect((s["statusLine"] as? [String: Any])?["command"] as? String == "/bin/sh '\(b.scriptURL.path)'")
        // The previous command is kept so the bridge can keep running it.
        let prev = try String(contentsOf: b.agentplayHome.appendingPathComponent("live/claude-statusline.previous"), encoding: .utf8)
        #expect(prev == "my-line --fancy")
        // The backup is the original settings byte for byte, not just some file at that path.
        #expect(try Data(contentsOf: b.backupURL) == originalData)
        try b.uninstall()
        s = try settings(b)
        #expect((s["statusLine"] as? [String: Any])?["command"] as? String == "my-line --fancy")
        #expect(s["model"] as? String == "opus")
        #expect(!FileManager.default.fileExists(atPath: b.scriptURL.path))
    }

    @Test func symlinkedSettingsStayALink() throws {
        let (b, dir) = try temp()
        let target = dir.appendingPathComponent("dotfiles/claude-settings.json")
        try FileManager.default.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(#"{"model":"opus"}"#.utf8).write(to: target)
        try FileManager.default.createSymbolicLink(at: b.claudeSettings, withDestinationURL: target)
        func linked() -> Bool { (try? FileManager.default.destinationOfSymbolicLink(atPath: b.claudeSettings.path)) == target.path }
        func onDisk() throws -> [String: Any] { try JSONSerialization.jsonObject(with: Data(contentsOf: target)) as! [String: Any] }
        try b.install(script: "#!/bin/sh\n")
        var disk = try onDisk()
        #expect(linked() && b.state() == .on && disk["statusLine"] != nil && disk["model"] as? String == "opus")
        // The backup is a copy of the contents, not a second link to the file being changed.
        #expect(FileManager.default.fileExists(atPath: b.backupURL.path)
                && (try? FileManager.default.destinationOfSymbolicLink(atPath: b.backupURL.path)) == nil)
        try b.uninstall()
        disk = try onDisk()
        #expect(linked() && b.state() == .off && disk["statusLine"] == nil)
    }

    @Test func noPreviousStatusLineIsRemovedCleanly() throws {
        let (b, _) = try temp()
        try b.install(script: "#!/bin/sh\n")   // no settings file at all yet
        try b.uninstall()
        #expect(try settings(b)["statusLine"] == nil)
        #expect(b.state() == .off)
    }

    @Test func refusesToRemoveSomeoneElsesStatusLine() throws {
        let (b, _) = try temp()
        try b.install(script: "#!/bin/sh\n")
        var s = try settings(b)
        s["statusLine"] = ["type": "command", "command": "another-tool"]
        try JSONSerialization.data(withJSONObject: s).write(to: b.claudeSettings)
        #expect(throws: ClaudeBridge.BridgeError.notOurs) { try b.uninstall() }
        #expect((try settings(b)["statusLine"] as? [String: Any])?["command"] as? String == "another-tool")
    }

    @Test func invalidSettingsAreNeverOverwritten() throws {
        let (b, _) = try temp()
        try Data("{ not json".utf8).write(to: b.claudeSettings)
        #expect(throws: ClaudeBridge.BridgeError.self) { try b.install(script: "#!/bin/sh\n") }
        #expect(try String(contentsOf: b.claudeSettings, encoding: .utf8) == "{ not json")
    }
}
