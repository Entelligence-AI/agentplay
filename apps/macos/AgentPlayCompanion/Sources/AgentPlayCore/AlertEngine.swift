import Foundation

/// Alert presets. All off by default; the user turns each on in Settings.
public enum AlertPreset: String, Codable, Sendable, CaseIterable, Identifiable {
    case needsYou, repeatedFailure, contextNearFull, allowanceLow, longRunningTool, unresolvedEnd, recurringPattern
    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .needsYou: "Session needs you"
        case .repeatedFailure: "Repeated failure"
        case .contextNearFull: "Context near full"
        case .allowanceLow: "Allowance running low"
        case .longRunningTool: "Long-running tool"
        case .unresolvedEnd: "Run finished with unresolved errors"
        case .recurringPattern: "New recurring pattern"
        }
    }

    public var detail: String {
        switch self {
        case .needsYou: "A Claude Code session is waiting for your input or permission. Not sent when its terminal is already in front."
        case .repeatedFailure: "The same command failed three times in a row in one run, with no pass in between."
        case .contextNearFull: "Only where the agent reports its context window (Codex today). Claude Code records tokens but no limit."
        case .allowanceLow: "Only where the provider itself reported plan usage with a reset time (Codex session files). Never estimated from tokens."
        case .longRunningTool: "A tool call with no result yet, on a run a live agent process is writing."
        case .unresolvedEnd: "A run that stopped with two or more errors in its last steps."
        case .recurringPattern: "At most one digest a day when a failure recurs across several runs."
        }
    }
}

public struct QuietHours: Codable, Sendable, Equatable {
    /// Minutes after local midnight. `start > end` wraps past midnight (22:00 to 07:00).
    public var start: Int
    public var end: Int

    public init(start: Int, end: Int) { self.start = start; self.end = end }

    public func contains(_ date: Date, timeZone: TimeZone) -> Bool {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timeZone
        let c = cal.dateComponents([.hour, .minute], from: date)
        let m = (c.hour ?? 0) * 60 + (c.minute ?? 0)
        if start == end { return false }
        return start < end ? (m >= start && m < end) : (m >= start || m < end)
    }
}

public struct AlertSettings: Codable, Sendable, Equatable {
    public var enabled: Set<AlertPreset> = []
    public var contextThreshold: Double = 90
    public var allowanceThresholdLeft: Double = 10
    public var longToolMinutes: Int = 15
    public var maxPerHour: Int = 2
    /// Same pattern in another run waits this long.
    public var patternCooldown: TimeInterval = 3600
    public var quietHours: QuietHours? = nil
    public var snoozeUntil: Date? = nil
    public var mutedPatterns: Set<String> = []
    public var mutedSessions: Set<String> = []
    public var mutedRepos: Set<String> = []
    public var includeRepoInNotifications = false
    public var silenceWhilePresenting = true

    public init() {}
}

public struct AlertHistory: Codable, Sendable, Equatable {
    /// Nothing that happened before this moment can alert: the first scan of old transcripts
    /// must not fire for last month's failures. Set on first launch, never moved back.
    public var watermark: Date
    public var delivered: [String: Date] = [:]
    public var patternLast: [String: Date] = [:]
    public var recent: [Date] = []

    public init(watermark: Date) { self.watermark = watermark }

    public mutating func purge(now: Date) {
        delivered = [:]; patternLast = [:]; recent = []
        watermark = max(watermark, now)
    }
}

public struct PendingAlert: Equatable, Sendable, Identifiable {
    public var id: String
    public var preset: AlertPreset
    public var patternKey: String
    public var sessionId: String?
    public var seq: Int?
    public var note: Redacted.Note
}

public struct AlertContext: Sendable {
    public var now: Date
    public var timeZone: TimeZone
    public var isPresenting: Bool
    public init(now: Date, timeZone: TimeZone = .current, isPresenting: Bool = false) {
        self.now = now; self.timeZone = timeZone; self.isPresenting = isPresenting
    }
}

/// Decides which recorded conditions become a local notification. Pure: snapshot + settings + history in,
/// notifications + history out. Evidence comes from the Python core; noise control lives here.
public enum AlertEngine {
    /// A trigger older than this is history, not news (a Mac waking after a night asleep).
    public static let maxTriggerAge: TimeInterval = 2 * 3600
    /// Context and pending-call readings older than this are not acted on.
    public static let maxReadingAge: TimeInterval = 10 * 60

    public static func candidates(_ s: Snapshot, settings: AlertSettings, ctx: AlertContext) -> [PendingAlert] {
        let repoBySession = Dictionary(s.runs.map { ($0.sessionId, $0.repo) }, uniquingKeysWith: { a, _ in a })
        let runsById = Dictionary(s.runs.map { ($0.sessionId, $0) }, uniquingKeysWith: { a, _ in a })
        var out: [PendingAlert] = []
        let inc = settings.includeRepoInNotifications

        for a in s.alerts {
            let preset: AlertPreset
            switch a.rule {
            case .repeatedFailure: preset = .repeatedFailure
            case .unresolvedEnd: preset = .unresolvedEnd
            case .unknown: continue
            }
            // A failure that later passed is resolved: worth seeing in the popover, not a ping.
            if a.isResolved { continue }
            out.append(PendingAlert(id: a.signature, preset: preset, patternKey: a.patternKey, sessionId: a.sessionId,
                                    seq: a.triggerSeq, note: Redacted.notification(for: a, repo: repoBySession[a.sessionId] ?? nil,
                                                                                    includeRepo: inc)))
        }
        _ = runsById

        for r in s.runs where r.state == .active {
            if let v = r.context.value, let pct = v.percent, r.context.isAvailable,
               !r.context.isStale(now: ctx.now, maxAge: maxReadingAge), pct >= settings.contextThreshold {
                let t = Int(settings.contextThreshold)
                out.append(PendingAlert(
                    id: "ctx|\(r.sessionId)|\(t)", preset: .contextNearFull, patternKey: "ctx|\(r.sessionId)",
                    sessionId: r.sessionId, seq: r.lastEvent?.seq,
                    note: .init(title: "Context near full · \(r.agentLabel)",
                                body: "\(Format.percent(pct)) of the context window, as \(r.agentLabel) reported it.")))
            }
            if let p = r.pendingTool, let secs = p.elapsed.value, secs >= settings.longToolMinutes * 60 {
                out.append(PendingAlert(
                    id: "tool|\(r.sessionId)|\(p.event.seq)", preset: .longRunningTool, patternKey: "tool|\(p.event.label)",
                    sessionId: r.sessionId, seq: p.event.seq,
                    note: .init(title: "Long-running tool · \(r.agentLabel)",
                                body: "\(p.event.label) has been running for \(Format.duration(TimeInterval(secs))).")))
            }
        }

        for l in s.limits {
            guard let v = l.used.value, l.used.kind == .providerReported, l.used.freshness == .fresh,
                  l.used.confidence != .low, v.leftPercent <= settings.allowanceThresholdLeft else { continue }
            if let reset = l.resetsAt, reset <= ctx.now { continue }
            if l.used.isStale(now: ctx.now, maxAge: 3600) { continue }
            let t = Int(settings.allowanceThresholdLeft)
            out.append(PendingAlert(
                id: "plan|\(l.provider)|\(l.window)|\(l.resetsAt.map { Int($0.timeIntervalSince1970) } ?? 0)|\(t)",
                preset: .allowanceLow, patternKey: "plan|\(l.provider)|\(l.window)", sessionId: nil, seq: nil,
                note: .init(title: "\(l.providerLabel) \(l.windowLabel.lowercased()) allowance low",
                            body: "\(Format.percent(v.leftPercent)) left, resets \(Format.clock(l.resetsAt, timeZone: ctx.timeZone)). Reported by \(l.providerLabel).")))
        }

        if let card = s.learn {
            out.append(PendingAlert(id: "learn|\(card.signature)", preset: .recurringPattern, patternKey: card.patternKey,
                                    sessionId: card.evidence.first?.sessionId, seq: card.evidence.first?.seq,
                                    note: .init(title: "A pattern across your runs", body: card.title.replacingOccurrences(of: "`", with: ""))))
        }
        return out
    }

    /// Trigger time of a candidate, for the watermark and age checks.
    static func triggerTime(_ p: PendingAlert, _ s: Snapshot) -> Date? {
        switch p.preset {
        case .repeatedFailure, .unresolvedEnd:
            return s.alerts.first { $0.signature == p.id }?.triggerAt
        case .contextNearFull:
            return s.runs.first { $0.sessionId == p.sessionId }?.context.observedAt
        case .longRunningTool:
            return s.runs.first { $0.sessionId == p.sessionId }?.pendingTool?.elapsed.observedAt
        case .allowanceLow:
            return s.limits.first { "plan|\($0.provider)|\($0.window)" == p.patternKey }?.used.observedAt
        case .recurringPattern:
            return s.learn?.failures.observedAt
        case .needsYou:
            return nil  // sent by the app the moment a live record changes, not from snapshots
        }
    }

    public static func evaluate(_ s: Snapshot, settings: AlertSettings, history: AlertHistory,
                                ctx: AlertContext) -> (deliver: [PendingAlert], history: AlertHistory) {
        var h = history
        // Sample data never notifies.
        guard !s.isSample, !settings.enabled.isEmpty else { return ([], h) }
        if let until = settings.snoozeUntil, until > ctx.now { return ([], h) }
        if let q = settings.quietHours, q.contains(ctx.now, timeZone: ctx.timeZone) { return ([], h) }
        if settings.silenceWhilePresenting && ctx.isPresenting { return ([], h) }

        let repoBySession = Dictionary(s.runs.map { ($0.sessionId, $0.repo) }, uniquingKeysWith: { a, _ in a })
        // Deliveries "in the future" (the clock moved back) still count against the hourly budget.
        h.recent = h.recent.filter { ctx.now.timeIntervalSince($0) < 3600 }
        var out: [PendingAlert] = []
        var unresolvedGroup: [PendingAlert] = []

        for p in candidates(s, settings: settings, ctx: ctx) {
            guard settings.enabled.contains(p.preset) else { continue }
            guard h.delivered[p.id] == nil else { continue }
            guard !settings.mutedPatterns.contains(p.patternKey) else { continue }
            if let sid = p.sessionId {
                if settings.mutedSessions.contains(sid) { continue }
                if let repo = repoBySession[sid] ?? nil, settings.mutedRepos.contains(repo) { continue }
            }
            guard let at = triggerTime(p, s) else { continue }
            // Watermark: nothing from before the companion started watching.
            guard at > h.watermark else { h.delivered[p.id] = ctx.now; continue }
            guard ctx.now.timeIntervalSince(at) <= maxTriggerAge else { h.delivered[p.id] = ctx.now; continue }
            if let last = h.patternLast[p.patternKey] {
                let gap: TimeInterval = p.preset == .recurringPattern ? 86400 : settings.patternCooldown
                if abs(ctx.now.timeIntervalSince(last)) < gap { continue }
            }
            if p.preset == .unresolvedEnd { unresolvedGroup.append(p); continue }
            // One per pattern per pass, too: the cooldown covers alerts chosen in this same scan.
            if out.contains(where: { $0.patternKey == p.patternKey }) { continue }
            out.append(p)
        }
        if unresolvedGroup.count == 1 {
            out.append(unresolvedGroup[0])
        } else if unresolvedGroup.count > 1 {
            var g = unresolvedGroup[0]
            g.note = .init(title: "Runs ended with errors",
                           body: "\(unresolvedGroup.count) runs ended with unresolved errors.")
            out.append(g)
            for p in unresolvedGroup.dropFirst() { h.delivered[p.id] = ctx.now }
        }

        let budget = max(0, settings.maxPerHour - h.recent.count)
        let send = Array(out.prefix(budget))
        for p in send {
            h.delivered[p.id] = ctx.now
            h.patternLast[p.patternKey] = ctx.now
            h.recent.append(ctx.now)
        }
        // Over budget: not marked delivered, so it can go out next hour if still current.
        return (send, h)
    }
}
