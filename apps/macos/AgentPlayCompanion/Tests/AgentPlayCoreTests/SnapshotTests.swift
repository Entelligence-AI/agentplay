import Foundation
import Testing
@testable import AgentPlayCore

let fixturesDir = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("Fixtures")

func fixture(_ name: String) throws -> Snapshot {
    try SnapshotDecoder.decode(Data(contentsOf: fixturesDir.appendingPathComponent("\(name).json")))
}

@Suite struct SnapshotDecoding {
    @Test(arguments: ["two-active", "no-agents", "detected-idle", "stale-quota", "conflicting-plans",
                      "partial-unavailable", "many-sessions"])
    func everyFixtureDecodes(name: String) throws {
        let s = try fixture(name)
        #expect(s.isSample)
        #expect(s.schemaVersion == 1)
    }

    @Test func twoActiveRuns() throws {
        let s = try fixture("two-active")
        #expect(s.activeRuns.count == 2)
        let claude = try #require(s.runs.first { $0.agentTool == "claude-code" })
        #expect(claude.context.kind == .recorded)
        #expect(claude.context.value?.percent == nil)  // tokens, no denominator
        let codex = try #require(s.runs.first { $0.agentTool == "codex" })
        #expect(codex.context.kind == .providerReported)
        #expect(codex.context.value?.limit == 258400)
        #expect(s.alerts.contains { $0.rule == .repeatedFailure && $0.command == "pnpm test" })
    }

    @Test func staleQuotaIsStale() throws {
        let s = try fixture("stale-quota")
        let five = try #require(s.limits.first { $0.window == "five_hour" })
        #expect(five.used.freshness == .stale)
        #expect(five.used.confidence == .low)
    }

    @Test func conflictingPlansAreLowConfidence() throws {
        let s = try fixture("conflicting-plans")
        let five = try #require(s.limits.first { $0.window == "five_hour" })
        #expect(five.used.confidence == .low)
        #expect(five.used.explanation.contains("different accounts"))
    }

    @Test func partialDataStaysUnavailable() throws {
        let s = try fixture("partial-unavailable")
        let cursor = try #require(s.runs.first { $0.agentTool == "cursor" })
        #expect(cursor.context.kind == .unavailable && cursor.context.value == nil)
        #expect(s.unreadable?.isEmpty == false)
        #expect(s.sources.first { $0.agent == "claude-code" }?.status == .error)
    }

    @Test func claudePlanUsageIsNeverAvailable() throws {
        for name in ["two-active", "stale-quota", "many-sessions"] {
            let s = try fixture(name)
            let c = try #require(s.limits.first { $0.provider == "claude-code" })
            #expect(c.used.kind == .unavailable && c.used.value == nil)
        }
    }

    @Test func rejectsOtherSchemas() {
        let other = #"{"schema":"something/v9","schemaVersion":1}"#.data(using: .utf8)!
        #expect(throws: SnapshotError.incompatibleSchema("something/v9")) { try SnapshotDecoder.decode(other) }
        let newer = #"{"schema":"agentplay.companion/v1","schemaVersion":2}"#.data(using: .utf8)!
        #expect(throws: SnapshotError.newerVersion(found: 2, supported: 1)) { try SnapshotDecoder.decode(newer) }
        #expect(throws: SnapshotError.malformed("not a JSON object")) { try SnapshotDecoder.decode(Data("nope".utf8)) }
    }

    @Test func unknownEnumValuesDegradeToLeastAuthoritative() throws {
        let json = #"{"value": 5, "kind": "telepathy", "source": "crystal_ball", "observedAt": null, "window": "decade", "confidence": "certain", "explanation": "x"}"#
        let p = try SnapshotDecoder.decoder().decode(DataPoint<Int>.self, from: Data(json.utf8))
        #expect(p.kind == .unavailable && p.value == nil && p.source == DataSource.none && p.confidence == .low)
    }

    @Test func percentNeverWithoutLimit() throws {
        let json = #"{"tokens": 100, "limit": null, "percent": 42}"#
        let v = try JSONDecoder().decode(ContextValue.self, from: Data(json.utf8))
        #expect(v.percent == nil)
    }

    @Test func extraFieldsAreIgnored() throws {
        var obj = try JSONSerialization.jsonObject(with: Data(contentsOf: fixturesDir.appendingPathComponent("no-agents.json"))) as! [String: Any]
        obj["futureField"] = ["anything": 1]
        let s = try SnapshotDecoder.decode(JSONSerialization.data(withJSONObject: obj))
        #expect(s.runs.isEmpty)
    }

    @Test func rebaseKeepsRelativeTimes() throws {
        let s = try fixture("two-active")
        let now = Date(timeIntervalSince1970: 2_000_000_000)
        let r = SnapshotDecoder.rebased(s, to: now)
        let before = s.generatedAt.timeIntervalSince(try #require(s.runs[0].lastEventAt))
        let after = r.generatedAt.timeIntervalSince(try #require(r.runs[0].lastEventAt))
        #expect(abs(before - after) < 0.001)
        #expect(r.generatedAt == now)
    }

    @Test func coreTooOldIsRecognised() {
        let r = ProcessResult(status: 2, stdout: Data(),
                              stderr: "agentplay: error: argument cmd: invalid choice: 'companion' (choose from 'view', 'build')")
        guard case .failure(.coreTooOld) = SnapshotService.interpret(r) else { Issue.record("expected coreTooOld"); return }
    }

    @Test func processFailureKeepsLastLine() {
        let r = ProcessResult(status: 1, stdout: Data(), stderr: "Traceback\nValueError: bad")
        guard case .failure(.processFailed(let code, let msg)) = SnapshotService.interpret(r) else {
            Issue.record("expected processFailed"); return
        }
        #expect(code == 1 && msg == "ValueError: bad")
    }
}

@Suite struct Links {
    let base = URL(string: "http://127.0.0.1:7317/")!

    @Test func sessionAndSeq() {
        let u = ViewerLink.url(base: base, sessionId: "local:claude-code:abc", seq: 42)
        #expect(u.absoluteString == "http://127.0.0.1:7317/?session=local%3Aclaude-code%3Aabc&at=42")
    }

    @Test(arguments: ["a b", "a+b", "a&at=1", "a#b", "a/b/c", "ünï", "A:B"])
    func idsRoundTripThroughURLSearchParamsRules(id: String) {
        let u = ViewerLink.url(base: base, sessionId: id, seq: 3)
        let q = u.query ?? ""
        // What the viewer does: split on & and =, then decode (and + would be a space).
        let parts = Dictionary(uniqueKeysWithValues: q.split(separator: "&").map {
            let kv = $0.split(separator: "=", maxSplits: 1).map(String.init)
            return (kv[0], kv[1].replacingOccurrences(of: "+", with: " ").removingPercentEncoding ?? "")
        })
        #expect(parts["session"] == id)
        #expect(parts["at"] == "3")
        #expect(!q.contains("+"))
    }

    @Test func errorsTabAndNoSeq() {
        let u = ViewerLink.url(base: base, sessionId: "s", seq: nil, errorsTab: true)
        #expect(u.query == "session=s&stab=errors")
    }

    @Test func startLineParsing() {
        #expect(ViewerLink.parseStartLine("Viewer → \u{1B}[1mhttp://127.0.0.1:53211/\u{1B}[0m   (Ctrl+C to stop)")?.port == 53211)
        #expect(ViewerLink.parseStartLine("Viewer → http://127.0.0.1:7317/   (Ctrl+C to stop)")?.port == 7317)
        #expect(ViewerLink.parseStartLine("Viewer → http://evil.example:7317/") == nil)
        #expect(ViewerLink.parseStartLine("Port 7317 is busy; using 7318 instead.") == nil)
        #expect(ViewerLink.parseStartLine("http://127.0.0.1:99999/") == nil)
    }

    @Test func onlyLoopbackIsAViewer() {
        #expect(ViewerLink.isLocalViewer(URL(string: "http://127.0.0.1:1/")!))
        #expect(!ViewerLink.isLocalViewer(URL(string: "https://127.0.0.1:1/")!))
        #expect(!ViewerLink.isLocalViewer(URL(string: "http://127.0.0.1.evil.com:1/")!))
        #expect(!ViewerLink.isLocalViewer(URL(string: "http://127.0.0.1/")!))
    }
}

@Suite struct Formatting {
    let now = Date(timeIntervalSince1970: 1_800_000_000)

    @Test func relativeTimes() {
        #expect(Format.relative(now.addingTimeInterval(-10), now: now) == "just now")
        #expect(Format.relative(now.addingTimeInterval(-240), now: now) == "4m ago")
        #expect(Format.relative(now.addingTimeInterval(-7200), now: now) == "2h ago")
        #expect(Format.relative(now.addingTimeInterval(3600), now: now).contains("clock"))
        #expect(Format.relative(nil, now: now) == "time unknown")
    }

    @Test func tokensAndDurations() {
        #expect(Format.tokens(142_000) == "142k")
        #expect(Format.tokens(1_250) == "1.2k")
        #expect(Format.tokens(2_400_000) == "2.4M")
        #expect(Format.duration(4 * 60) == "4m")
        #expect(Format.duration(3600 + 12 * 60) == "1h 12m")
    }

    @Test func truncation() {
        #expect(Format.truncate("payments-api") == "payments-api")
        let t = Format.truncate("a-very-long-repository-name-indeed", max: 12)
        #expect(t.count == 12 && t.contains("…"))
    }
}

@Suite struct Redaction {
    @Test func summaryHasNoPathsOrIds() throws {
        let s = try fixture("two-active")
        for r in s.runs {
            let text = Redacted.runSummary(r, now: s.generatedAt)
            #expect(!text.contains(r.sessionId))
            for e in r.recentEvents { if let t = e.target { #expect(!text.contains(t)) } }
        }
    }

    @Test func notificationsOmitRepoByDefault() throws {
        let s = try fixture("two-active")
        let a = try #require(s.alerts.first)
        let note = Redacted.notification(for: a, repo: "payments-api", includeRepo: false)
        #expect(!note.body.contains("payments-api") && !note.title.contains("payments-api"))
        #expect(!note.body.contains(a.sessionId))
        #expect(note.body.contains("pnpm test"))
        #expect(Redacted.notification(for: a, repo: "payments-api", includeRepo: true).body.contains("payments-api"))
    }

    @Test func childEnvironmentIsMinimal() {
        let env = childEnvironment(parent: ["HOME": "/Users/x", "AWS_SECRET_ACCESS_KEY": "s", "PYTHONPATH": "/evil",
                                            "AGENTPLAY_HOME": "/tmp/ap", "AGENTPLAY_NO_NETWORK": "0",
                                            "AGENTPLAY_COMPANION_PYTHON": "/x"])
        #expect(env["AWS_SECRET_ACCESS_KEY"] == nil && env["PYTHONPATH"] == nil)
        #expect(env["AGENTPLAY_HOME"] == "/tmp/ap")
        #expect(env["AGENTPLAY_NO_NETWORK"] == "1" && env["DO_NOT_TRACK"] == "1")
        #expect(env["AGENTPLAY_COMPANION_PYTHON"] == nil)
    }

    @Test func pythonRunsIsolated() {
        for mode in [SnapshotService.Mode.read, .refresh, .fullScan] {
            #expect(SnapshotService.arguments(for: mode).first == "-I")
        }
    }
}

@Suite struct ViewerOwnership {
    let py = "/Applications/AgentPlay Companion.app/Contents/Resources/python/bin/python3"

    @Test func onlyOurOwnViewerMatches() {
        #expect(ViewerManager.isOurViewer("\(py) -I -m agentplay view --no-open --no-build --port 0", python: py))
        // The user's own viewers never match, whatever they run.
        #expect(!ViewerManager.isOurViewer("/Users/me/.local/bin/uvx agentplay view", python: py))
        #expect(!ViewerManager.isOurViewer("/opt/homebrew/bin/python3 -m agentplay view --port 7317", python: py))
        #expect(!ViewerManager.isOurViewer("\(py) -I -m agentplay companion snapshot --json", python: py))
    }

    @Test func reapIgnoresForeignPidAndClearsFile() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let pidFile = dir.appendingPathComponent("viewer.pid")
        try "4242\n".write(to: pidFile, atomically: true, encoding: .utf8)
        let v = ViewerManager(runtime: Runtime(python: URL(fileURLWithPath: py), bundled: true), pidFile: pidFile)
        // PID reused by an unrelated process: left alone.
        #expect(v.reapLeftover(commandOf: { _ in "/usr/bin/vim notes.txt" }) == false)
        #expect(!FileManager.default.fileExists(atPath: pidFile.path))
    }
}
