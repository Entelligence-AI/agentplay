import AgentPlayCore
import Foundation

/// The menu as data: one flat, ordered list. The view renders it top to bottom and keyboard navigation walks
/// the selectable items, so what you see and what the arrow keys reach can't drift apart.
enum MenuItem: Identifiable, Equatable {
    case header(updated: String)
    case message(title: String, detail: String?)
    case provider(ProviderSummary, expanded: Bool)
    case meter(ProviderLimitWindow)
    case enableLimits(provider: String, detail: String)
    case sectionHeader(String, trailing: String?)
    case usage(period: String, UsagePeriod, expandable: Bool, expanded: Bool)
    case usageDetail(UsageReport)
    case session(SessionLine)
    case error(ErrorLine)
    case action(MenuAction)
    case separator(String)

    var id: String {
        switch self {
        case .header: "header"
        case .message(let t, _): "msg:\(t)"
        case .provider(let p, _): "prov:\(p.provider)"
        case .meter(let w): "meter:\(w.id)"
        case .enableLimits(let p, _): "enable:\(p)"
        case .sectionHeader(let t, _): "sec:\(t)"
        case .usage(let p, _, _, _): "usage:\(p)"
        case .usageDetail: "usage-detail"
        case .session(let s): "session:\(s.id)"
        case .error(let e): "error:\(e.id)"
        case .action(let a): "action:\(a.rawValue)"
        case .separator(let k): "sep:\(k)"
        }
    }

    var isSelectable: Bool {
        switch self {
        case .provider, .meter, .enableLimits, .session, .error, .action: true
        case .usage(_, _, let expandable, _): expandable
        default: false
        }
    }
}

enum MenuAction: String, CaseIterable {
    case showAllSessions, refresh, openViewer, settings, quit

    var title: String {
        switch self {
        case .showAllSessions: "Show All Sessions…"
        case .refresh: "Refresh"
        case .openViewer: "Open AgentPlay"
        case .settings: "Settings…"
        case .quit: "Quit AgentPlay"
        }
    }

    var symbol: String? {
        switch self {
        case .refresh: "arrow.clockwise"
        case .openViewer: "macwindow"
        default: nil
        }
    }

    var shortcut: String? {
        switch self {
        case .refresh: "⌘R"
        case .openViewer: "⌘O"
        case .settings: "⌘,"
        case .quit: "⌘Q"
        case .showAllSessions: nil
        }
    }
}

/// One session row: a live process, a built run, or both.
struct SessionLine: Equatable, Identifiable {
    enum Dot: Equatable { case working, waiting, idle, recent, ended }
    var id: String
    var title: String          // "Claude Code · payments-api"
    var detail: String         // "Edited src/charge.test.ts · context 71%"
    var trailing: String       // "4m"
    var dot: Dot
    var sessionId: String
    var seq: Int?
    var openable: Bool         // false until its map exists
    var repo: String?
}

struct ErrorLine: Equatable, Identifiable {
    var id: String
    var title: String          // "pnpm test failed 3×"
    var detail: String         // "Claude Code · payments-api · non-zero exit"
    var trailing: String       // "10:42"
    var sessionId: String?
    var seq: Int?
    var patternKey: String?
    var resolved: Bool
    var isPattern: Bool
}

enum MenuBuilder {
    static func items(snapshot s: Snapshot?, phase: Phase, prefs: CompanionPrefs, lastUpdated: Date?,
                      lastError: SnapshotError?, isSample: Bool, now: Date, expanded: Set<String>,
                      usageExpanded: Bool, bridgeOn: Bool) -> [MenuItem] {
        var out: [MenuItem] = []
        let updated: String
        if isSample { updated = "Sample data" }
        else if case .scanning(let n) = phase { updated = n.map { "Scanning · \($0)" } ?? "Scanning…" }
        else if let u = lastUpdated {
            let age = now.timeIntervalSince(u)
            updated = age > 600 ? "Stale, \(Format.duration(age))" : "Updated \(Format.relative(u, now: now))"
        } else { updated = "" }
        out.append(.header(updated: updated))

        guard let s else {
            switch phase {
            case .failed(let e):
                out.append(.message(title: "Can't read your sessions", detail: e.localizedDescription))
            default:
                out.append(.message(title: "Finding your sessions…", detail: "Reading agent folders on this Mac"))
            }
            out += footer()
            return out
        }
        if let e = lastError { out.append(.message(title: "Last refresh failed", detail: e.localizedDescription)) }

        // Providers, most constrained first.
        let providers = ProviderSummary.build(s.limits).filter { p in
            // Show a provider block only for agents this Mac uses.
            s.sources.contains { $0.agent == p.provider && $0.status == .found } || !p.windows.isEmpty
        }
        if !providers.isEmpty { out.append(.separator("top")) }
        for (i, p) in providers.enumerated() {
            let isOpen = expanded.contains(p.provider) || (expanded.isEmpty && i == 0)
            out.append(.provider(p, expanded: isOpen))
            guard isOpen else { continue }
            if p.windows.isEmpty {
                if p.provider == "claude-code" && !bridgeOn {
                    out.append(.enableLimits(provider: p.provider, detail: "Show 5-hour and weekly usage"))
                } else {
                    out.append(.message(title: "Limits unavailable", detail: shortReason(p)))
                }
            } else {
                for w in p.windows { out.append(.meter(w)) }
                if let plans = p.conflictingPlans {
                    out.append(.message(title: "Plans disagree",
                                        detail: "Recent sessions reported \(plans.map(\.capitalized).joined(separator: " and ")). They may be different accounts."))
                }
            }
        }

        // Sessions: live processes first, then recent runs.
        let lines = sessionLines(s, prefs: prefs, now: now)
        out.append(.separator("sessions"))
        let working = (s.live ?? []).filter { $0.status == .busy }.count
        let waiting = (s.live ?? []).filter { $0.status == .waiting }.count
        var counts: [String] = []
        if working > 0 { counts.append("\(working) working") }
        if waiting > 0 { counts.append("\(waiting) need\(waiting == 1 ? "s" : "") you") }
        out.append(.sectionHeader("Sessions", trailing: counts.isEmpty ? nil : counts.joined(separator: " · ")))
        if lines.isEmpty {
            if s.store.available {
                out.append(.message(title: "No sessions running", detail: nil))
            } else if s.foundSources.isEmpty {
                out.append(.message(title: "No agents found", detail: "Start Claude Code or Codex, then reopen."))
            } else {
                out.append(.message(title: "No sessions built yet", detail: "Refresh to scan \(s.foundSources.map(\.label).joined(separator: ", "))."))
            }
        }
        for l in lines.prefix(4) { out.append(.session(l)) }
        if s.store.available { out.append(.action(.showAllSessions)) }

        // Recent errors and one cross-run pattern.
        let errs = errorLines(s, prefs: prefs, now: now)
        if !errs.isEmpty {
            out.append(.separator("errors"))
            out.append(.sectionHeader("Recent errors", trailing: nil))
            for e in errs.prefix(3) { out.append(.error(e)) }
        }

        // Usage (estimated from local maps). Last, so opening its detail never pushes sessions out of view.
        if let u = s.usage, u.last30Days.requests > 0 {
            out.append(.separator("usage"))
            out.append(.sectionHeader("Usage", trailing: "estimated"))
            if let b = u.claudeBlock, b.end > now {
                var block = UsagePeriod(tokens: b.tokens, costUsd: b.costUsd, requests: b.requests ?? 0,
                                        unpricedRequests: b.unpricedRequests ?? 0, sessions: 0)
                block.byModel = nil
                out.append(.usage(period: "Current 5-hour block", block, expandable: false, expanded: false))
            }
            out.append(.usage(period: "Today", u.today, expandable: true, expanded: usageExpanded))
            if usageExpanded { out.append(.usageDetail(u)) }
        }
        out += footer()
        return out
    }

    static func footer() -> [MenuItem] {
        [.separator("actions"), .action(.refresh), .action(.openViewer), .separator("app"),
         .action(.settings), .action(.quit)]
    }

    static func shortReason(_ p: ProviderSummary) -> String {
        switch p.provider {
        case "codex": "Codex hasn't reported limits on this Mac yet"
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
            out.append(SessionLine(
                id: l.sessionId, title: "\(AgentNames.label(l.agentTool)) · \(Format.truncate(repo ?? "session", max: 24))",
                detail: detail(run, status: l.status, since: l.statusSince, now: now),
                trailing: l.startedAt.map { Format.duration(now.timeIntervalSince($0)) } ?? "",
                dot: l.status == .waiting ? .waiting : .working, sessionId: l.sessionId,
                seq: run?.lastEvent?.seq, openable: l.inStore, repo: repo))
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
            out.append(SessionLine(
                id: r.sessionId, title: "\(r.agentLabel) · \(Format.truncate(r.repo ?? "outside a repo", max: 24))",
                detail: detail(r, status: r.liveStatus, since: nil, now: now),
                trailing: Format.relative(r.latestAt, now: now).replacingOccurrences(of: " ago", with: ""),
                dot: dot, sessionId: r.sessionId, seq: r.lastEvent?.seq, openable: true, repo: r.repo))
        }
        return out
    }

    static func detail(_ r: RunActivity?, status: LiveStatus?, since: Date?, now: Date) -> String {
        var parts: [String] = []
        if status == .waiting { parts.append("Waiting for you" + (since.map { " · \(Format.duration(now.timeIntervalSince($0)))" } ?? "")) }
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
                return ErrorLine(id: a.signature, title: title, detail: d.joined(separator: " · "),
                                 trailing: Format.clock(a.firstAt ?? a.triggerAt), sessionId: a.sessionId,
                                 seq: a.firstSeq ?? a.triggerSeq, patternKey: a.patternKey, resolved: a.isResolved,
                                 isPattern: false)
            }
        if let l = s.learn, !prefs.isLearnDismissed(l.patternKey, now: now) {
            out.append(ErrorLine(id: "learn", title: "\(l.command ?? "A command") failed \(l.failures.value ?? 0)× in \(l.runs) runs",
                                 detail: "Pattern across \(l.sessions) recent sessions · passed later in some",
                                 trailing: "", sessionId: l.evidence.first?.sessionId, seq: l.evidence.first?.seq,
                                 patternKey: l.patternKey, resolved: false, isPattern: true))
        }
        return out
    }
}
