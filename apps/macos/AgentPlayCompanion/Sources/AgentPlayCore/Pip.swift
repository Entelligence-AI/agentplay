import Foundation

/// Pip, the character in the menu bar. Its body fills with the 5-hour session, it stands on the week, and its
/// pose is the one thing worth knowing right now. Pure logic here; drawing is in the app (PipIcon).
public enum PipPose: String, Sendable, CaseIterable {
    case sleep     // nothing running
    case work      // an agent is working
    case wave      // a session is waiting on you
    case cheer     // a session just finished
    case oops      // a command keeps failing
    case sweat     // ahead of pace on a plan window
    case limit     // a plan window is spent until it resets
}

/// Something worth a moment of the user's attention, shown beside Pip and acted on by a click.
public struct PipEvent: Equatable, Sendable, Identifiable {
    public enum Kind: Equatable, Sendable { case needsYou, finished, failed, limit }
    public var kind: Kind
    public var text: String          // "docs-site needs you": tooltip and VoiceOver
    public var label: String         // "docs-site": beside Pip, whose pose says the rest
    public var sessionId: String?
    public var pid: Int32?
    public var seq: Int?
    public var id: String

    public init(kind: Kind, text: String, label: String? = nil, sessionId: String?, pid: Int32?, seq: Int?, id: String) {
        self.kind = kind; self.text = text; self.label = label ?? text
        self.sessionId = sessionId; self.pid = pid; self.seq = seq; self.id = id
    }

    public var pose: PipPose {
        switch kind {
        case .needsYou: .wave
        case .finished: .cheer
        case .failed: .oops
        case .limit: .limit
        }
    }
}

public enum Pip {
    /// The pose for the current state. Waiting on you beats everything; a spent window beats activity;
    /// a fresh event shows its own pose for a moment.
    public static func pose(live: [LiveSession], limits: [ProviderLimitWindow], recent: PipEvent?, now: Date) -> PipPose {
        if live.contains(where: { $0.status == .waiting }) { return .wave }
        let fresh = limits.filter { $0.used.isAvailable && $0.used.freshness != .stale && ($0.resetsAt.map { $0 > now } ?? true) }
        if fresh.contains(where: { ($0.used.value?.usedPercent ?? 0) >= 100 }) { return .limit }
        if let recent { return recent.pose }
        if hotWindow(fresh, now: now) != nil { return .sweat }
        if live.contains(where: { $0.status == .busy }) { return .work }
        return .sleep
    }

    /// What Pip says at the top of the menu: one line about now, one with the numbers.
    public static func sentence(_ pose: PipPose, live: [LiveSession], limits: [ProviderLimitWindow],
                                recent: PipEvent?, now: Date) -> (title: String, detail: String?) {
        let lv = levels(limits)
        var nums: [String] = []
        if let s = lv.session { nums.append("Session \(Format.percent(s * 100))") }
        if let w = lv.weekly { nums.append("week \(Format.percent(w * 100))") }
        let detail = nums.isEmpty ? nil : nums.joined(separator: " · ")
        let working = live.filter { $0.status == .busy }.count
        switch pose {
        case .wave:
            let waiting = live.filter { $0.status == .waiting }
            let names = waiting.compactMap(\.project).prefix(2).joined(separator: ", ")
            return ("\(waiting.count) waiting on you" + (names.isEmpty ? "" : ": \(names)"), detail)
        case .limit:
            let spent = limits.first { ($0.used.value?.usedPercent ?? 0) >= 100 }
            return ("Out of \(spent.map { $0.window == "five_hour" ? "session" : "weekly" } ?? "plan") usage"
                    + (spent?.resetsAt.map { " until \(Format.clock($0))" } ?? ""), detail)
        case .oops:
            return (recent?.text ?? "A command keeps failing", detail)
        case .cheer:
            return (recent?.text ?? "Done", detail)
        case .sweat:
            guard let w = hotWindow(limits, now: now) else { return ("Going fast", detail) }
            let name = w.window == "five_hour" ? "session" : "week"
            let used = Format.percent(w.used.value?.usedPercent ?? 0)
            let left = w.resetsAt.map { ", \(Format.duration($0.timeIntervalSince(now))) left" } ?? ""
            return ("Going fast: \(name) at \(used)\(left)", detail)
        case .work:
            return (working == 1 ? "1 agent working" : "\(working) agents working", detail)
        case .sleep:
            return ("All quiet", detail)
        }
    }

    /// The first fresh window that's 90% used or ahead of pace.
    public static func hotWindow(_ limits: [ProviderLimitWindow], now: Date) -> ProviderLimitWindow? {
        limits.first { w in
            guard w.used.isAvailable, w.used.freshness != .stale, w.resetsAt.map({ $0 > now }) ?? true else { return false }
            let used = w.used.value?.usedPercent ?? 0
            let p = Pace.of(used: used, windowMinutes: w.windowMinutes, resetsAt: w.resetsAt, now: now)
            return used >= 90 || (p.map { $0.level != .normal } ?? false)
        }
    }

    /// The fill and the ground: the most constrained provider's session and weekly fractions.
    public static func levels(_ limits: [ProviderLimitWindow]) -> (session: Double?, weekly: Double?) {
        guard let p = ProviderSummary.build(limits).first(where: { !$0.windows.isEmpty }) else { return (nil, nil) }
        return (p.window("five_hour")?.used.value.map { $0.usedPercent / 100 },
                p.window("weekly")?.used.value.map { $0.usedPercent / 100 })
    }

    /// What changed between two looks at the live sessions and alerts, most urgent first.
    public static func events(liveBefore: [LiveSession]?, liveAfter: [LiveSession],
                              alertsBefore: Set<String>?, alerts: [AlertCandidate], repos: [String: String]) -> [PipEvent] {
        guard let liveBefore else { return [] }  // the first look at launch is not news
        let was = Dictionary(liveBefore.map { ($0.sessionId, $0.status) }, uniquingKeysWith: { a, _ in a })
        func name(_ l: LiveSession) -> String { Format.truncate(l.project ?? repos[l.sessionId] ?? "A session", max: 14) }
        var out: [PipEvent] = []
        for l in liveAfter where l.status == .waiting && was[l.sessionId] != .waiting {
            out.append(PipEvent(kind: .needsYou, text: "\(name(l)) needs you", label: name(l), sessionId: l.sessionId, pid: l.pid, seq: nil,
                                id: "needs|\(l.sessionId)|\(Int(l.statusSince?.timeIntervalSince1970 ?? 0))"))
        }
        for l in liveAfter where l.status == .idle && was[l.sessionId] == .busy {
            out.append(PipEvent(kind: .finished, text: "\(name(l)) is done", label: name(l), sessionId: l.sessionId, pid: l.pid, seq: nil,
                                id: "done|\(l.sessionId)|\(Int(l.statusSince?.timeIntervalSince1970 ?? 0))"))
        }
        if let alertsBefore {
            for a in alerts where !a.isResolved && a.rule == .repeatedFailure && !alertsBefore.contains(a.signature) {
                let cmd = Format.truncate(a.command ?? "A command", max: 12)
                out.append(PipEvent(kind: .failed, text: "\(cmd) failed \(a.count ?? 3)×", label: "\(cmd) ×\(a.count ?? 3)",
                                    sessionId: a.sessionId, pid: nil,
                                    seq: a.firstSeq ?? a.triggerSeq, id: "fail|\(a.signature)"))
            }
        }
        return out
    }
}
