import Foundation
import Testing
@testable import AgentPlayCore

@Suite struct PipTests {
    let now = Date(timeIntervalSince1970: 1_792_800_000)

    func live(_ id: String, _ status: LiveStatus, project: String = "docs-site") -> LiveSession {
        LiveSession(agentTool: "claude-code", sessionId: id, status: status, pid: 42, project: project,
                    startedAt: nil, statusSince: now, inStore: true)
    }

    func window(_ name: String, used: Double, minutes: Int, resetsIn: TimeInterval, provider: String = "claude-code",
                observedAgo: TimeInterval = 0) -> ProviderLimitWindow {
        ProviderLimitWindow(provider: provider, window: name, windowMinutes: minutes, plan: nil, limitId: nil,
                            resetsAt: now.addingTimeInterval(resetsIn), usageUrl: nil,
                            used: DataPoint(value: LimitValue(usedPercent: used, leftPercent: 100 - used),
                                            kind: .providerReported, source: .officialProviderAPI,
                                            observedAt: now.addingTimeInterval(-observedAgo),
                                            window: .fiveHour, explanation: "", freshness: .fresh))
    }

    @Test func limitTitleNamesTheWindowThePoseIsAbout() {
        // A session window spent an hour ago has reset since; the week is what is actually out.
        let lim = [window("five_hour", used: 100, minutes: 300, resetsIn: -60),
                   window("weekly", used: 100, minutes: 10080, resetsIn: 86400)]
        #expect(Pip.pose(live: [], limits: lim, recent: nil, now: now) == .limit)
        #expect(Pip.sentence(.limit, live: [], limits: lim, recent: nil, now: now).title.hasPrefix("Out of weekly usage until "))
        // Only the reset session: nothing is spent any more.
        #expect(Pip.pose(live: [], limits: [lim[0]], recent: nil, now: now) == .sleep)
    }

    @Test func oldReadingsDoNotOutrankCurrentOnes() {
        // Codex reported 95% two days ago and nothing since; Claude is at 40% right now.
        let lim = [window("weekly", used: 95, minutes: 10080, resetsIn: 3 * 86400, provider: "codex", observedAgo: 2 * 86400),
                   window("five_hour", used: 40, minutes: 300, resetsIn: 3600)]
        let ranked = ProviderSummary.build(lim, now: now)
        #expect(ranked.map(\.provider) == ["claude-code", "codex"])
        #expect(ranked[1].tightest(now: now) == nil)
        #expect(Pip.levels(lim, now: now).session == 0.4)
        // Seen two days ago, when both readings were current, Codex led.
        let then = now.addingTimeInterval(-2 * 86400)
        #expect(ProviderSummary.build(lim, now: then).first?.provider == "codex")
    }

    @Test func poseFollowsWhatMattersMost() {
        let calm = [window("five_hour", used: 20, minutes: 300, resetsIn: 4 * 3600)]
        #expect(Pip.pose(live: [], limits: calm, recent: nil, now: now) == .sleep)
        #expect(Pip.pose(live: [live("a", .busy)], limits: calm, recent: nil, now: now) == .work)
        #expect(Pip.pose(live: [live("a", .busy), live("b", .waiting)], limits: calm, recent: nil, now: now) == .wave)
        let spent = [window("five_hour", used: 100, minutes: 300, resetsIn: 3600)]
        #expect(Pip.pose(live: [live("a", .busy)], limits: spent, recent: nil, now: now) == .limit)
        // 60% used with only an hour of five gone: ahead of pace.
        let hot = [window("five_hour", used: 60, minutes: 300, resetsIn: 4 * 3600)]
        #expect(Pip.pose(live: [live("a", .busy)], limits: hot, recent: nil, now: now) == .sweat)
        let done = PipEvent(kind: .finished, text: "x is done", sessionId: "a", pid: nil, seq: nil, id: "d")
        #expect(Pip.pose(live: [], limits: calm, recent: done, now: now) == .cheer)
    }

    @Test func pipSaysWhatIsGoingOn() {
        let lim = [window("five_hour", used: 75, minutes: 300, resetsIn: 2 * 3600)]
        let s = Pip.sentence(.wave, live: [live("a", .waiting)], limits: lim, recent: nil, now: now)
        #expect(s.title == "1 waiting on you: docs-site" && s.detail == "Session 75%")
        #expect(Pip.sentence(.work, live: [live("a", .busy), live("b", .busy)], limits: [], recent: nil, now: now).title == "2 agents working")
        #expect(Pip.sentence(.sleep, live: [], limits: [], recent: nil, now: now) == ("All quiet", nil))
        // Sweating about the week, not the calm session: say so.
        let week = [window("five_hour", used: 4, minutes: 300, resetsIn: 4 * 3600),
                    window("weekly", used: 71, minutes: 10080, resetsIn: 5 * 86400)]
        #expect(Pip.sentence(.sweat, live: [], limits: week, recent: nil, now: now).title == "Going fast this week")
    }

    @Test func eventsAreTransitionsNotStates() {
        let before = [live("a", .busy), live("b", .busy, project: "payments-api")]
        let after = [live("a", .waiting), live("b", .idle, project: "payments-api")]
        let ev = Pip.events(liveBefore: before, liveAfter: after, alertsBefore: [], alerts: [], repos: [:])
        #expect(ev.map(\.kind) == [.needsYou, .finished])
        #expect(ev[0].text == "docs-site needs you" && ev[0].label == "docs-site" && ev[0].pid == 42)
        #expect(ev[1].text == "payments-api is done")
        #expect(Pip.events(liveBefore: after, liveAfter: after, alertsBefore: [], alerts: [], repos: [:]).isEmpty)
        #expect(Pip.events(liveBefore: nil, liveAfter: after, alertsBefore: nil, alerts: [], repos: [:]).isEmpty)
    }

    @Test func newRepeatedFailuresAreEvents() throws {
        let alerts = try fixture("two-active").alerts.filter { $0.rule == .repeatedFailure && !$0.isResolved }
        try #require(!alerts.isEmpty)
        let ev = Pip.events(liveBefore: [], liveAfter: [], alertsBefore: [], alerts: alerts, repos: [:])
        #expect(ev.first?.kind == .failed && ev.first?.seq != nil)
        let seen = Set(alerts.map(\.signature))
        #expect(Pip.events(liveBefore: [], liveAfter: [], alertsBefore: seen, alerts: alerts, repos: [:]).isEmpty)
    }

    @Test func tipsLeadWithWhatNeedsYou() throws {
        let s = try fixture("two-active")
        let tips = Pip.tips(s, now: s.generatedAt)
        #expect(tips.first?.text == "docs-site is waiting for you.")
        if case .goTo(let sid, _) = tips.first?.action { #expect(sid.hasSuffix("sample-9e2b")) } else { Issue.record("expected goTo") }
        #expect(!Pip.tips(try fixture("no-agents"), now: s.generatedAt).isEmpty)
    }

    @Test func learningsComeFromTheUsersOwnData() throws {
        let l = Pip.learnings(try fixture("two-active"))
        #expect(!l.isEmpty && l.count <= 3)
        #expect(l.contains { $0.text.contains("payments-api") || $0.text.contains("sessions from") })
    }
}
