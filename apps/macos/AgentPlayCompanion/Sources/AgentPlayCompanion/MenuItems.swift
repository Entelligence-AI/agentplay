import AgentPlayCore
import Foundation

/// The menu as data, in display order. StatusMenu turns each entry into a native NSMenuItem; only the
/// provider cards and the usage summary and chart are custom views, and those are display-only.
/// Order follows attention: what needs you, then limits, then sessions, then usage.
enum MenuEntry: Equatable {
    case pip                                     // hosted: Pip, large, and what it has to say
    case header(String)                          // native section header
    case message(title: String, detail: String?) // informational, not clickable
    case provider(ProviderSummary)               // hosted card: name, plan, freshness, meters, pace
    case enableLimits(provider: String)          // "Turn On Live Limits…"
    case claudeAccounts                          // submenu: switch, save, add
    case session(SessionLine)
    case usage(UsageReport)                      // hosted two-line summary
    case usageDetails(UsageReport)               // submenu: chart, 7 and 30 days, by model, by project
    case errors([ErrorLine])                     // each error, its details one level down
    case health(Insights)                        // hosted: run health score and components
    case fix(Insights.Fix)                       // a fix, its details one level down
    case action(MenuAction)
    case separator
}

enum MenuAction: String, CaseIterable {
    case leaveSample, showAllSessions, refresh, openViewer, settings, quit

    var title: String {
        switch self {
        case .leaveSample: "Show My Data"
        case .showAllSessions: "Show All Sessions…"
        case .refresh: "Refresh"
        case .openViewer: "Open AgentPlay"
        case .settings: "Settings…"
        case .quit: "Quit AgentPlay"
        }
    }

    var key: String {
        switch self {
        case .refresh: "r"
        case .openViewer: "o"
        case .settings: ","
        case .quit: "q"
        case .showAllSessions, .leaveSample: ""
        }
    }
}

/// One session row: a live process, a built run, or both.
struct SessionLine: Equatable, Identifiable {
    enum Dot: Equatable { case working, waiting, idle, recent, ended }
    var id: String
    var title: String          // "payments-api"
    var detail: String         // "Claude Code · Edited charge.test.ts · context 71% · 4m"
    var dot: Dot
    var sessionId: String
    var seq: Int?
    var openable: Bool         // false until its map exists
    var pid: Int32?            // set while an agent process runs it
}

struct ErrorLine: Equatable, Identifiable {
    var id: String
    var title: String          // "pnpm test failed 3×"
    var detail: String         // "Claude Code · payments-api · non-zero exit · 10:42"
    var sessionId: String?
    var seq: Int?
    var patternKey: String?
    var resolved: Bool
    var isPattern: Bool
    var alert: AlertCandidate? = nil
    var learn: LearnCard? = nil
    var repo: String? = nil
}

enum MenuBuilder {
    static func entries(snapshot s: Snapshot?, phase: Phase, prefs: CompanionPrefs, lastError: SnapshotError?,
                        isSample: Bool, now: Date, bridgeOn: Bool) -> [MenuEntry] {
        var out: [MenuEntry] = []
        if isSample {
            // Unmissable and one click to leave: sample numbers must never pass for yours.
            out.append(.header("Sample Data, Not Yours"))
            out.append(.action(.leaveSample))
            out.append(.separator)
        }
        guard let s else {
            switch phase {
            case .failed(let e):
                out.append(.message(title: "Can't Read Your Sessions", detail: e.localizedDescription))
            case .scanning(let n?):
                out.append(.message(title: "Finding Your Sessions…", detail: "\(n) indexed so far"))
            default:
                out.append(.message(title: "Finding Your Sessions…", detail: "Reading agent folders on this Mac"))
            }
            return out + footer()
        }
        if let e = lastError { out.append(.message(title: "Last Refresh Failed", detail: e.localizedDescription)) }
        if !isSample, prefs.showsPip { out += [.pip, .separator] }

        // 1. Sessions waiting on you: the one thing worth interrupting for.
        let lines = sessionLines(s, prefs: prefs, now: now)
        let waiting = lines.filter { $0.dot == .waiting }
        if !waiting.isEmpty {
            out.append(.header("Needs You"))
            out += waiting.map(MenuEntry.session)
            out.append(.separator)
        }

        // 2. Plan limits, most constrained provider first; only agents this Mac uses.
        // Claude always (its card carries the account and the switcher); others only with numbers to show.
        let providers = ProviderSummary.build(s.limits).filter { p in
            !p.windows.isEmpty || (p.provider == "claude-code" && s.sources.contains { $0.agent == p.provider && $0.status == .found })
        }
        for p in providers {
            out.append(.provider(p))
            if p.windows.isEmpty, p.provider == "claude-code" { out.append(.enableLimits(provider: p.provider)) }
            if p.provider == "claude-code" { out.append(.claudeAccounts) }
        }
        if !providers.isEmpty { out.append(.separator) }

        // 3. Everything else that ran recently.
        let rest = lines.filter { $0.dot != .waiting }
        out.append(.header("Sessions"))
        if rest.isEmpty {
            if !waiting.isEmpty {
                // All of them are above.
            } else if s.store.available {
                out.append(.message(title: "No Recent Sessions", detail: nil))
            } else if s.foundSources.isEmpty {
                out.append(.message(title: "No Agents Found", detail: "Start Claude Code or Codex, then reopen."))
            } else {
                out.append(.message(title: "No Sessions Indexed Yet", detail: "Refresh to scan \(s.foundSources.map(\.label).joined(separator: ", "))."))
            }
        }
        // Four when insights follow, so the menu still fits a laptop screen.
        out += rest.prefix(s.insights?.score != nil ? 4 : 5).map(MenuEntry.session)
        if s.store.available { out.append(.action(.showAllSessions)) }

        // 4. Insights: run health, the fixes that would raise it, and what keeps failing, details one level down.
        let errs = errorLines(s, prefs: prefs, now: now)
        let ins = s.insights.flatMap { $0.score == nil ? nil : $0 }
        if ins != nil || !errs.isEmpty {
            out.append(.separator)
            out.append(.header("Insights · Last \(s.insights?.days ?? 30) Days"))
            if let ins {
                out.append(.health(ins))
                out += ins.fixes.prefix(3).map(MenuEntry.fix)
            }
            if !errs.isEmpty { out.append(.errors(Array(errs.prefix(3)))) }
        }

        // 5. Estimated usage: two lines here, the rest one level down.
        if let u = s.usage, u.last30Days.requests > 0 {
            out.append(.separator)
            out.append(.header("Usage"))
            out.append(.usage(u))
            out.append(.usageDetails(u))
        }
        return out + footer()
    }

    static func footer() -> [MenuEntry] {
        [.separator, .action(.openViewer), .action(.refresh), .separator, .action(.settings), .action(.quit)]
    }

    static func shortReason(_ p: ProviderSummary) -> String {
        switch p.provider {
        case "codex": p.plan.map { "Codex reports no usage windows for the \($0.capitalized) plan" }
            ?? "Codex hasn't reported limits on this Mac yet"
        case "claude-code": "Waiting for Claude Code to report"
        default: "\(p.label) doesn't report limits locally"
        }
    }

    static func sessionLines(_ s: Snapshot, prefs: CompanionPrefs, now: Date) -> [SessionLine] {
        var out: [SessionLine] = []
        var seen = Set<String>()
        let runs = Dictionary(s.runs.map { ($0.sessionId, $0) }, uniquingKeysWith: { a, _ in a })
        let order: [LiveStatus: Int] = [.waiting: 0, .busy: 1, .idle: 2]
        let live = (s.live ?? []).sorted { (order[$0.status] ?? 3, $1.startedAt ?? .distantPast) < (order[$1.status] ?? 3, $0.startedAt ?? .distantPast) }
        for l in live where l.status != .idle {
            let run = runs[l.sessionId]
            seen.insert(l.sessionId)
            let repo = run?.repo ?? l.project
            var d = [AgentNames.label(l.agentTool), Format.truncate(detail(run, status: l.status, since: l.statusSince, now: now), max: 26)]
            out.append(SessionLine(
                id: l.sessionId, title: Format.truncate(repo ?? "Session", max: 32),
                detail: d.filter { !$0.isEmpty }.joined(separator: " · "),
                dot: l.status == .waiting ? .waiting : .working, sessionId: l.sessionId,
                seq: run?.lastEvent?.seq, openable: l.inStore, pid: l.pid))
        }
        let pinned = prefs.pinnedSessions
        let rest = s.runs.filter { !seen.contains($0.sessionId) }.sorted { a, b in
            let pa = pinned.firstIndex(of: a.sessionId) ?? Int.max, pb = pinned.firstIndex(of: b.sessionId) ?? Int.max
            if pa != pb { return pa < pb }
            return (a.latestAt ?? .distantPast) > (b.latestAt ?? .distantPast)
        }
        for r in rest {
            let dot: SessionLine.Dot = r.liveStatus == .idle ? .idle : r.state == .active ? .working
                : r.state == .recent ? .recent : .ended
            let pid = (s.live ?? []).first { $0.sessionId == r.sessionId }?.pid
            let d = [r.agentLabel, Format.truncate(detail(r, status: r.liveStatus, since: nil, now: now), max: 26), Format.relative(r.latestAt, now: now)]
            out.append(SessionLine(
                id: r.sessionId, title: Format.truncate(r.repo ?? "Outside a repo", max: 32),
                detail: d.filter { !$0.isEmpty }.joined(separator: " · "),
                dot: dot, sessionId: r.sessionId, seq: r.lastEvent?.seq, openable: true, pid: pid))
        }
        return out
    }

    static func detail(_ r: RunActivity?, status: LiveStatus?, since: Date?, now: Date) -> String {
        var parts: [String] = []
        if status == .waiting { parts.append("Waiting" + (since.map { " \(Format.duration(now.timeIntervalSince($0)))" } ?? "")) }
        if let r {
            if let p = r.pendingTool { parts.append("Running \(p.event.label)") }
            else if let e = r.lastEvent, status != .waiting {
                switch e.kind {
                case "edit": parts.append("Edited \(e.target ?? "a file")")
                case "read", "search": parts.append("Read \(e.target ?? e.label)")
                case "user": parts.append("Prompted")
                default: parts.append(e.err ? "\(e.label) failed" : e.label)
                }
            }
            if let pct = r.context.value?.percent { parts.append("context \(Format.percent(pct))") }
        } else if let status, status != .waiting {
            // Live, but its map isn't built yet: say what it's doing, not what we haven't done.
            parts.append(status.label)
        }
        return parts.joined(separator: " · ")
    }

    static func errorLines(_ s: Snapshot, prefs: CompanionPrefs, now: Date) -> [ErrorLine] {
        let repo = Dictionary(s.runs.map { ($0.sessionId, $0.repo) }, uniquingKeysWith: { a, _ in a })
        let recent = now.addingTimeInterval(-24 * 3600)
        var out = s.alerts
            .filter { !prefs.alerts.mutedPatterns.contains($0.patternKey) && ($0.triggerAt ?? .distantPast) > recent }
            .sorted { ($0.triggerAt ?? .distantPast) > ($1.triggerAt ?? .distantPast) }
            .map { a -> ErrorLine in
                let title = a.rule == .unresolvedEnd ? "Run ended with errors" : "\(a.command ?? "A command") failed \(a.count ?? 3)×"
                var d = [AgentNames.label(a.agentTool)]
                if let r = repo[a.sessionId] ?? nil { d.append(Format.truncate(r, max: 18)) }
                d.append(a.isResolved ? "passed later" : Format.errClass(a.errClass))
                d.append(Format.clock(a.firstAt ?? a.triggerAt))
                return ErrorLine(id: a.signature, title: title, detail: d.filter { !$0.isEmpty }.joined(separator: " · "),
                                 sessionId: a.sessionId,
                                 seq: a.firstSeq ?? a.triggerSeq, patternKey: a.patternKey, resolved: a.isResolved,
                                 isPattern: false, alert: a, repo: repo[a.sessionId] ?? nil)
            }
        if let l = s.learn, !prefs.isLearnDismissed(l.patternKey, now: now) {
            out.append(ErrorLine(id: "learn", title: "\(l.command ?? "A command") failed \(l.failures.value ?? 0)× in \(l.runs) runs",
                                 detail: "A pattern across \(l.sessions) recent sessions",
                                 sessionId: l.evidence.first?.sessionId, seq: l.evidence.first?.seq,
                                 patternKey: l.patternKey, resolved: false, isPattern: true, learn: l))
        }
        return out
    }
}
