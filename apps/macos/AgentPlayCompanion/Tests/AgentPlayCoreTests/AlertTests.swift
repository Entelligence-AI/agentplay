import Foundation
import Testing
@testable import AgentPlayCore

/// A live-looking snapshot from the two-active fixture, with `sample` cleared and times rebased to `now`.
func liveSnapshot(now: Date) throws -> Snapshot {
    var s = SnapshotDecoder.rebased(try fixture("two-active"), to: now)
    s.sample = false
    return s
}

func failure(_ sig: String, pattern: String = "aaaaaaaaaaaaaaaa", session: String = "local:claude-code:sample-7f3a",
             at: Date, resolved: Int? = nil, rule: AlertRuleKind = .repeatedFailure) -> AlertCandidate {
    AlertCandidate(rule: rule, signature: sig, patternKey: pattern, sessionId: session, agentTool: "claude-code",
                   command: "pnpm test", errClass: "exit_code", agent: "main", count: 3, firstSeq: 1, firstAt: at, triggerSeq: 5,
                   triggerAt: at, resolvedSeq: resolved,
                   evidence: DataPoint(value: .int(3), kind: .recorded, source: .agentplayMap, explanation: "x"))
}

@Suite struct Alerts {
    let start = Date(timeIntervalSince1970: 1_800_000_000)
    var now: Date { start.addingTimeInterval(600) }
    let utc = TimeZone(identifier: "UTC")!

    func settings(_ presets: Set<AlertPreset> = [.repeatedFailure]) -> AlertSettings {
        var s = AlertSettings(); s.enabled = presets; return s
    }

    func run(_ s: Snapshot, _ st: AlertSettings, _ h: AlertHistory, at: Date? = nil, presenting: Bool = false)
        -> ([PendingAlert], AlertHistory) {
        AlertEngine.evaluate(s, settings: st, history: h, ctx: AlertContext(now: at ?? now, timeZone: utc, isPresenting: presenting))
    }

    @Test func offByDefault() throws {
        let s = try liveSnapshot(now: now)
        #expect(run(s, AlertSettings(), AlertHistory(watermark: start)).0.isEmpty)
    }

    @Test func repeatedFailureFiresOnceThenDedupes() throws {
        let s = try liveSnapshot(now: now)
        let (first, h1) = run(s, settings(), AlertHistory(watermark: start))
        #expect(first.count == 1 && first[0].preset == .repeatedFailure && first[0].seq != nil)
        // Duplicate scan, same evidence.
        let (second, _) = run(s, settings(), h1, at: now.addingTimeInterval(30))
        #expect(second.isEmpty)
    }

    @Test func historyFromBeforeInstallNeverAlerts() throws {
        let s = try liveSnapshot(now: now)
        // First launch happens after the failure was recorded.
        let (out, h) = run(s, settings(), AlertHistory(watermark: now))
        #expect(out.isEmpty)
        // And it stays quiet on later scans, too.
        #expect(run(s, settings(), h, at: now.addingTimeInterval(60)).0.isEmpty)
    }

    @Test func oldTriggerAfterWakeIsHistory() throws {
        var s = try liveSnapshot(now: now)
        s.alerts = [failure("1111111111111111", at: now.addingTimeInterval(-3 * 3600))]
        #expect(run(s, settings(), AlertHistory(watermark: start.addingTimeInterval(-86400))).0.isEmpty)
    }

    @Test func resolvedFailureDoesNotNotify() throws {
        var s = try liveSnapshot(now: now)
        s.alerts = [failure("1111111111111111", at: now.addingTimeInterval(-60), resolved: 9)]
        #expect(run(s, settings(), AlertHistory(watermark: start)).0.isEmpty)
    }

    @Test func mutedPatternSessionAndRepo() throws {
        let s = try liveSnapshot(now: now)
        let a = try #require(s.alerts.first)
        var st = settings(); st.mutedPatterns = [a.patternKey]
        #expect(run(s, st, AlertHistory(watermark: start)).0.isEmpty)
        st = settings(); st.mutedSessions = [a.sessionId]
        #expect(run(s, st, AlertHistory(watermark: start)).0.isEmpty)
        st = settings(); st.mutedRepos = ["payments-api"]
        #expect(run(s, st, AlertHistory(watermark: start)).0.isEmpty)
    }

    @Test func sameCommandInTwoReposRespectsPatternCooldown() throws {
        var s = try liveSnapshot(now: now)
        s.alerts = [failure("1111111111111111", session: "a", at: now.addingTimeInterval(-60)),
                    failure("2222222222222222", session: "b", at: now.addingTimeInterval(-30))]
        let (out, h) = run(s, settings(), AlertHistory(watermark: start))
        #expect(out.count == 1)  // second waits for the pattern cooldown, it isn't dropped
        // After the cooldown the other run's failure goes out: it is still under two hours old.
        let (later, h2) = run(s, settings(), h, at: now.addingTimeInterval(3700))
        #expect(later.count == 1 && later[0].sessionId == "b")
        #expect(run(s, settings(), h2, at: now.addingTimeInterval(3800)).0.isEmpty)
    }

    @Test func hourlyBudget() throws {
        var s = try liveSnapshot(now: now)
        s.alerts = (0..<5).map { i in failure(String(repeating: "\(i)", count: 16), pattern: String(repeating: "\(i)", count: 16),
                                               session: "s\(i)", at: now.addingTimeInterval(Double(-10 * i - 1))) }
        let (out, h) = run(s, settings(), AlertHistory(watermark: start))
        #expect(out.count == 2)
        #expect(run(s, settings(), h, at: now.addingTimeInterval(120)).0.isEmpty)
        // An hour later the budget is back and the rest are still current (under two hours old).
        #expect(run(s, settings(), h, at: now.addingTimeInterval(3601)).0.count == 2)
    }

    @Test func clockMovedBackStillCountsAgainstBudget() throws {
        var s = try liveSnapshot(now: now)
        s.alerts = (0..<3).map { i in failure(String(repeating: "\(i)", count: 16), pattern: String(repeating: "\(i)", count: 16),
                                               session: "s\(i)", at: now.addingTimeInterval(-5)) }
        var h = AlertHistory(watermark: start)
        h.recent = [now.addingTimeInterval(1800), now.addingTimeInterval(1800)]  // delivered "in the future"
        #expect(run(s, settings(), h).0.isEmpty)
    }

    @Test func quietHoursFollowTheTimeZone() throws {
        let s = try liveSnapshot(now: now)
        var st = settings()
        // 22:00 to 07:00, wrapping midnight.
        st.quietHours = QuietHours(start: 22 * 60, end: 7 * 60)
        let q = try #require(st.quietHours)
        var cal = Calendar(identifier: .gregorian); cal.timeZone = utc
        let late = cal.date(bySettingHour: 23, minute: 30, second: 0, of: now)!
        let noon = cal.date(bySettingHour: 12, minute: 0, second: 0, of: now)!
        #expect(q.contains(late, timeZone: utc))
        #expect(!q.contains(noon, timeZone: utc))
        // The user flew to Tokyo: 14:00 UTC is 23:00 there, inside quiet hours, and outside them in UTC.
        let tokyo = TimeZone(identifier: "Asia/Tokyo")!
        let tokyoLate = cal.date(bySettingHour: 14, minute: 0, second: 0, of: now)!  // 23:00 in Tokyo
        #expect(q.contains(tokyoLate, timeZone: tokyo))
        #expect(!q.contains(tokyoLate, timeZone: utc))
        let r = AlertEngine.evaluate(s, settings: st, history: AlertHistory(watermark: start),
                                     ctx: AlertContext(now: tokyoLate, timeZone: tokyo))
        #expect(r.deliver.isEmpty)
    }

    @Test func snoozeAndPresenting() throws {
        let s = try liveSnapshot(now: now)
        var st = settings(); st.snoozeUntil = now.addingTimeInterval(3600)
        #expect(run(s, st, AlertHistory(watermark: start)).0.isEmpty)
        #expect(run(s, settings(), AlertHistory(watermark: start), presenting: true).0.isEmpty)
        var noDND = settings(); noDND.silenceWhilePresenting = false
        #expect(run(s, noDND, AlertHistory(watermark: start), presenting: true).0.count == 1)
    }

    @Test func sampleDataNeverNotifies() throws {
        let s = SnapshotDecoder.rebased(try fixture("two-active"), to: now)
        #expect(s.isSample)
        #expect(run(s, settings(Set(AlertPreset.allCases)), AlertHistory(watermark: start)).0.isEmpty)
    }

    @Test func contextAlertOnlyWithADenominator() throws {
        var s = try liveSnapshot(now: now)
        var st = settings([.contextNearFull]); st.contextThreshold = 70
        let (out, _) = run(s, st, AlertHistory(watermark: start))
        // Codex reports 184k of 258k (71%); Claude records tokens without a limit and never alerts.
        #expect(out.count == 1 && out[0].sessionId == "local:codex:sample-0c1d")
        // A stale reading does not alert.
        s.runs = s.runs.map { var r = $0; r.context.observedAt = now.addingTimeInterval(-3600); return r }
        #expect(run(s, st, AlertHistory(watermark: start)).0.isEmpty)
    }

    @Test func allowanceNeedsFreshProviderData() throws {
        var s = try liveSnapshot(now: now)
        var st = settings([.allowanceLow]); st.allowanceThresholdLeft = 60
        let fired = run(s, st, AlertHistory(watermark: start)).0
        #expect(fired.count == 1 && fired[0].patternKey == "plan|codex|weekly")  // 59% left weekly; 68% of 5-hour is above
        s.limits = s.limits.map { var l = $0; l.used.freshness = .stale; return l }
        #expect(run(s, st, AlertHistory(watermark: start)).0.isEmpty)
        var c = try liveSnapshot(now: now)
        c.limits = c.limits.map { var l = $0; l.used.confidence = .low; return l }  // conflicting accounts
        #expect(run(c, st, AlertHistory(watermark: start)).0.isEmpty)
        var r = try liveSnapshot(now: now)
        r.limits = r.limits.map { var l = $0; l.resetsAt = now.addingTimeInterval(-1); return l }  // already reset
        #expect(run(r, st, AlertHistory(watermark: start)).0.isEmpty)
    }

    @Test func unresolvedEndsAreGrouped() throws {
        var s = try liveSnapshot(now: now)
        s.alerts = [failure("1111111111111111", pattern: "p1", session: "a", at: now.addingTimeInterval(-60), rule: .unresolvedEnd),
                    failure("2222222222222222", pattern: "p1", session: "b", at: now.addingTimeInterval(-50), rule: .unresolvedEnd),
                    failure("3333333333333333", pattern: "p1", session: "c", at: now.addingTimeInterval(-40), rule: .unresolvedEnd)]
        let (out, h) = run(s, settings([.unresolvedEnd]), AlertHistory(watermark: start))
        #expect(out.count == 1 && out[0].note.body.hasPrefix("3 runs"))
        #expect(run(s, settings([.unresolvedEnd]), h, at: now.addingTimeInterval(7200 - 61)).0.isEmpty)
    }

    @Test func purgeKeepsWatermarkMovingForward() {
        var h = AlertHistory(watermark: start)
        h.delivered["x"] = now
        h.purge(now: now)
        #expect(h.delivered.isEmpty && h.watermark == now)
    }
}

@Suite struct PrefsTests {
    @Test func firstLaunchSetsWatermark() {
        let d = UserDefaults(suiteName: "test.\(UUID().uuidString)")!
        let store = PrefsStore(defaults: d)
        let t = Date(timeIntervalSince1970: 1_800_000_000)
        let p = store.load(now: t)
        #expect(p.history.watermark == t && !p.onboardingDone && p.alerts.enabled.isEmpty)
        var q = p; q.onboardingDone = true; store.save(q)
        #expect(store.load(now: t.addingTimeInterval(99)).history.watermark == t)
    }

    @Test func customFoldersUseTheCoreVariables() {
        var p = CompanionPrefs()
        p.customFolders = ["claude-code": ["/Volumes/work/.claude"], "opencode": ["/x/oc"]]
        let env = p.folderEnvironment
        #expect(env["AGENTPLAY_CLAUDE_HOMES"] == "~/.claude,/Volumes/work/.claude")
        #expect(env["AGENTPLAY_OPENCODE_HOMES"] == "/x/oc")
    }

    @Test func learnDismissalExpires() {
        var p = CompanionPrefs()
        let t = Date(timeIntervalSince1970: 1_800_000_000)
        p.dismissedLearn["k"] = t
        #expect(p.isLearnDismissed("k", now: t.addingTimeInterval(86400)))
        #expect(!p.isLearnDismissed("k", now: t.addingTimeInterval(15 * 86400)))
    }
}
