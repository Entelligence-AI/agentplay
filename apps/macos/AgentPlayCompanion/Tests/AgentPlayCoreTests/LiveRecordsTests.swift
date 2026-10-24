import Foundation
import Testing
@testable import AgentPlayCore

@Suite struct LiveRecordsTests {
    func record(_ fields: [String: Any]) -> Data {
        var base: [String: Any] = ["pid": 4242, "sessionId": "abc", "status": "waiting",
                                   "cwd": "/Users/me/SECRET-DIR/payments-api", "name": "SECRET-NAME",
                                   "messagingSocketPath": "/tmp/SECRET-SOCK",
                                   "startedAt": 1_792_000_000_000.0, "statusUpdatedAt": 1_792_000_100_000.0]
        base.merge(fields) { _, new in new }
        return try! JSONSerialization.data(withJSONObject: base)
    }

    @Test func parsesAndKeepsOnlyTheFolderName() throws {
        let s = try #require(LiveRecords.parse(record([:])))
        #expect(s.sessionId == "local:claude-code:abc" && s.status == .waiting && s.pid == 4242)
        #expect(s.project == "payments-api")
        #expect(!s.isSDK)
        #expect(try #require(LiveRecords.parse(record(["entrypoint": "sdk-ts"]))).isSDK)
        #expect(s.statusSince == Date(timeIntervalSince1970: 1_792_000_100))
        let raw = String(decoding: try JSONEncoder().encode(s), as: UTF8.self)
        for secret in ["SECRET-DIR", "SECRET-NAME", "SECRET-SOCK"] { #expect(!raw.contains(secret)) }
    }

    @Test func rejectsWhatIsNotALiveRecord() {
        #expect(LiveRecords.parse(record(["status": "dancing"])) == nil)
        #expect(LiveRecords.parse(record(["pid": 1])) == nil)
        #expect(LiveRecords.parse(record(["sessionId": ""])) == nil)
        #expect(LiveRecords.parse(Data("{nope".utf8)) == nil)
    }

    @Test func readsAliveRecordsOnlyWaitingFirst() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("live-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        try record(["pid": 10, "sessionId": "busy", "status": "busy"]).write(to: dir.appendingPathComponent("10.json"))
        try record(["pid": 11, "sessionId": "wait"]).write(to: dir.appendingPathComponent("11.json"))
        try record(["pid": 12, "sessionId": "dead"]).write(to: dir.appendingPathComponent("12.json"))
        try Data("x".utf8).write(to: dir.appendingPathComponent("notes.txt"))
        let live = LiveRecords.read(dir: dir) { $0 != 12 }
        #expect(live.map(\.sessionId) == ["local:claude-code:wait", "local:claude-code:busy"])
    }

    @Test func mergeTakesStatusFromRecordsAndInStoreFromSnapshot() throws {
        var old = try #require(LiveRecords.parse(record(["status": "busy"])))
        old.inStore = true
        let fresh = try #require(LiveRecords.parse(record([:])))
        let merged = LiveRecords.merge(snapshot: [old], records: [fresh])
        #expect(merged.count == 1 && merged[0].status == .waiting && merged[0].inStore)
        #expect(LiveRecords.merge(snapshot: [old], records: []).isEmpty)  // the process ended
    }

    @Test func needsYouNamesTheProjectOnlyWhenAllowed() throws {
        let l = try #require(LiveRecords.parse(record([:])))
        #expect(Redacted.needsYou(l, includeRepo: false).title == "Claude Code needs you")
        #expect(!Redacted.needsYou(l, includeRepo: false).body.contains("payments-api"))
        #expect(Redacted.needsYou(l, includeRepo: true).title == "payments-api needs you")
    }

    @Test func finishedNoteNeedsRealWorkAndSaysHowItWent() throws {
        let l = try #require(LiveRecords.parse(record(["status": "idle"])))
        let now = Date(timeIntervalSince1970: 1_792_800_000)
        #expect(Redacted.finished(l, run: nil, busySince: now.addingTimeInterval(-30), includeRepo: true, now: now) == nil)
        let run = try fixture("two-active").runs.first { $0.counts.calls ?? 0 > 0 }
        let note = try #require(Redacted.finished(l, run: run, busySince: now.addingTimeInterval(-14 * 60), includeRepo: false, now: now))
        #expect(note.title == "Claude Code is done" && note.body.hasPrefix("Worked 14m"))
        #expect(note.body.contains("tool call") && !note.body.contains("payments-api"))
    }

    @Test func onlyTransitionsIntoWaitingAreNews() throws {
        let busy = try #require(LiveRecords.parse(record(["status": "busy"])))
        let waiting = try #require(LiveRecords.parse(record([:])))
        #expect(LiveRecords.newlyWaiting(before: [busy], after: [waiting]).count == 1)
        #expect(LiveRecords.newlyWaiting(before: [waiting], after: [waiting]).isEmpty)
        #expect(LiveRecords.newlyWaiting(before: nil, after: [waiting]).isEmpty)  // already waiting at launch
        #expect(LiveRecords.newlyWaiting(before: [], after: [waiting]).count == 1)  // a new session asking at once
    }
}
