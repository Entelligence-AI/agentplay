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
    case health(Insights)                        // run health, its parts, what was applied and what Autotune holds
    case signals(Signals)                        // hosted card: how you steer, the four rates one level down
    case fix(Insights.Fix)                       // the best fix; a click opens the window on Fixes
    case applied(Insights)                       // what AgentPlay changed and whether it worked
    case tabs(improve: Int)                      // hosted: Now · Improve · Setup, switched in place
    case suggestion(Coach.Suggestion)            // one of Claude's suggestions
    case tip(SetupReport.Tip)                    // a way to improve, from what was measured
    case askClaude(asking: Bool)                 // turn on suggestions from Claude, or "Asking Claude…"
    case setupItem(SetupReport.Item)             // a skill, subagent or MCP server you use, and how sessions go with it
    case unused(kind: String, items: [SetupReport.Item])   // what sits unused, by kind
    case split(SetupReport.Split)                // hosted: your sessions and automated runs, with cost
    case hooks([SetupReport.Item])               // hosted: each hook and its script
    case openInsights(InsightsSection, String)
    case action(MenuAction)
    case separator
}

/// The menu's three tabs, named for what's in them: Usage (limits, sessions, spend), Fixes (what to do next),
/// Tools (skills, MCP servers, subagents and hooks, and how each performs).
enum MenuTab: String, CaseIterable {
    case now, improve, setup
    var title: String {
        switch self {
        case .now: "Usage"
        case .improve: "Fixes"
        case .setup: "Tools"
        }
    }
}

enum MenuAction: String, CaseIterable {
    case leaveSample, showAllSessions, refresh, openViewer, checkUpdates, settings, quit

    var title: String {
        switch self {
        case .leaveSample: "Show My Data"
        case .showAllSessions: "All Sessions…"
        case .refresh: "Refresh"
        case .openViewer: "Open AgentPlay"
        case .checkUpdates: "Check for Updates…"
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
        case .showAllSessions, .leaveSample, .checkUpdates: ""
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
    var run: RunActivity?      // its map, for the detail one level down
    var live: LiveSession?     // its process, while one runs
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
    /// Each `trim` step drops a row of the least important sections so the menu fits a short screen. Past this
    /// many, the only rows left to drop are the waiting sessions, one per step; StatusMenu keeps going until the
    /// menu fits or another step changes nothing.
    static let maxTrim = 4

    static func entries(snapshot s: Snapshot?, phase: Phase, prefs: CompanionPrefs, lastError: SnapshotError?,
                        isSample: Bool, now: Date, bridgeOn: Bool, trim: Int = 0, tab: MenuTab = .now,
                        askingClaude: Bool = false) -> [MenuEntry] {
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
        if !isSample, prefs.showsPip { out.append(.pip) }
        let ins = s.insights.flatMap { $0.score == nil ? nil : $0 }
        // The badge counts what the Improve tab shows: fixes, and Claude's suggestions when they're on.
        // The same count Pip says: the next steps measured here (Claude's suggestions are extra, below them).
        out.append(.tabs(improve: doNext(s, ins: ins).count))
        switch tab {
        case .now: break
        case .improve: return out + improve(s, ins: ins, prefs: prefs, trim: trim, askingClaude: askingClaude) + footer()
        case .setup: return out + setup(s, trim: trim) + footer()
        }

        // 1. Sessions waiting on you: the one thing worth interrupting for.
        let lines = sessionLines(s, now: now)
        let waiting = lines.filter { $0.dot == .waiting }
        if !waiting.isEmpty {
            // These rows go last: only once everything else is as short as it gets, and never below one. The
            // header carries the count when some are hidden; Pip's own line names them too.
            let keep = max(1, waiting.count - max(0, trim - maxTrim))
            out.append(.header(keep < waiting.count ? "Needs You · \(waiting.count)" : "Needs You"))
            out += waiting.prefix(keep).map(MenuEntry.session)
            out.append(.separator)
        }

        // 2. Plan limits, most constrained provider first; only agents this Mac uses.
        // Claude always (its card carries the account and the switcher); others only with numbers to show.
        let providers = ProviderSummary.build(s.limits, now: now).filter { p in
            !p.windows.isEmpty || (p.provider == "claude-code" && s.sources.contains { $0.agent == p.provider && $0.status == .found })
        }
        for p in providers {
            out.append(.provider(p))
            if p.windows.isEmpty, p.provider == "claude-code" { out.append(.enableLimits(provider: p.provider)) }
            if p.provider == "claude-code" { out.append(.claudeAccounts) }
        }
        if !providers.isEmpty { out.append(.separator) }

        // 3. What's open now. Only live processes: finished runs live in All Sessions (right-click), not here.
        let working = lines.filter { $0.dot != .waiting }
        out.append(.header(waiting.isEmpty ? "Live Sessions" : "Also Open"))
        if working.isEmpty {
            if !waiting.isEmpty {
                out.append(.message(title: "Nothing else is running", detail: nil))
            } else if let last = s.runs.max(by: { ($0.latestAt ?? .distantPast) < ($1.latestAt ?? .distantPast) }) {
                let who = [last.agentLabel, last.repo].compactMap { $0 }.joined(separator: " in ")
                out.append(.message(title: "No agent is running",
                                    detail: "Last: \(who), \(Format.relative(last.latestAt, now: now)). Right-click Pip for all sessions."))
            } else if s.foundSources.isEmpty {
                out.append(.message(title: "No Agents Found", detail: "Start Claude Code or Codex, then reopen."))
            } else if !s.store.available {
                out.append(.message(title: "No Sessions Indexed Yet", detail: "Refresh to scan \(s.foundSources.map(\.label).joined(separator: ", "))."))
            } else {
                out.append(.message(title: "No agent is running", detail: "Right-click Pip for all sessions."))
            }
        }
        // Three rows at most, the working ones first; idle processes are often forgotten tabs. All Sessions has the rest.
        let busy = working.filter { $0.dot == .working }, idle = working.filter { $0.dot == .idle }
        out += (busy + idle.prefix(1)).prefix(max(1, 3 - trim)).map(MenuEntry.session)

        // 4. The best next step stays on the first screen; Improve has all of them.
        if let first = doNext(s, ins: ins).first, trim < 4 {
            out.append(.separator)
            out.append(.header("Do This Next"))
            out.append(first)
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

    /// Improve: what to fix, what Claude suggests, what was changed and whether it worked, and how runs are going.
    static func improve(_ s: Snapshot, ins: Insights?, prefs: CompanionPrefs, trim: Int, askingClaude: Bool) -> [MenuEntry] {
        var out: [MenuEntry] = []
        let next = doNext(s, ins: ins)
        if !next.isEmpty {
            out.append(.header("Do This Next"))
            out += next.prefix(max(2, 6 - trim))
        }
        out.append(.header("Claude Suggests"))
        if let c = s.coach, !c.suggestions.isEmpty, prefs.asksClaudeForSuggestions {
            out += c.suggestions.prefix(max(1, 3 - trim)).map(MenuEntry.suggestion)
        } else {
            out.append(.askClaude(asking: askingClaude))
        }
        if let ins, !(ins.applied ?? []).isEmpty {
            out.append(.header("What Changed"))
            out.append(.applied(ins))
        }
        let sig = s.usage?.signals.flatMap { $0.current.messages >= 20 ? $0 : nil }
        if ins != nil || sig != nil {
            out.append(.header("How It's Going · Last \(s.insights?.days ?? 30) Days"))
            if let ins { out.append(.health(ins)) }
            if let sig { out.append(.signals(sig)) }
        }
        if ins == nil, sig == nil, s.coach == nil {
            out.append(.message(title: "Not enough sessions yet", detail: "Insights start after a few sessions with Claude Code."))
        }
        out.append(.separator)
        out.append(.openInsights(.fixes, "Open Insights…"))
        return out
    }

    /// Every next step, best first: fixes worth points, then what was measured (failing and heavy tools,
    /// sessions that run out of room, unused setup, tools worth using more), then fixes worth no points.
    static func doNext(_ s: Snapshot, ins: Insights?) -> [MenuEntry] {
        let fixes = ins?.fixes ?? []
        let tips = s.usage?.setup?.tips ?? []
        let order = ["failing", "idle", "context", "room", "unused", "use"]
        let sorted = tips.sorted { (order.firstIndex(of: $0.kind) ?? 9) < (order.firstIndex(of: $1.kind) ?? 9) }
        return fixes.filter { ($0.points ?? 0) >= 0.5 }.map(MenuEntry.fix)
            + sorted.map(MenuEntry.tip)
            + fixes.filter { ($0.points ?? 0) < 0.5 }.map(MenuEntry.fix)
    }

    /// Setup: what Claude Code loads for you, how often each ran, and how sessions went with it.
    static func setup(_ s: Snapshot, trim: Int) -> [MenuEntry] {
        guard let st = s.usage?.setup, !st.items.isEmpty else {
            return [.message(title: "No setup found", detail: "Skills, subagents and MCP servers you add to Claude Code show here.")]
        }
        var out: [MenuEntry] = []
        if let sp = st.sessions, sp.automated > 0 {
            out.append(.header("Sessions · Last \(st.days) Days"))
            out.append(.split(sp))
        }
        // Yours first (what you built or installed), then the MCP servers and connectors you call.
        let live = st.items.filter { !$0.isOff }
        let used = live.filter { ($0.uses > 0 || $0.verdict == "used") && $0.kind != "hook" }
        // Yours before built-ins, and what a hook runs before what's merely called most.
        let mine = used.filter { $0.kind != "mcp" }.sorted {
            ($0.source == "you" ? 0 : 1, $0.runBy == nil ? 1 : 0, -$0.uses) < ($1.source == "you" ? 0 : 1, $1.runBy == nil ? 1 : 0, -$1.uses)
        }
        let servers = used.filter { $0.kind == "mcp" }
        if used.isEmpty { out.append(.message(title: "Nothing ran in \(st.days) days", detail: nil)) }
        if !mine.isEmpty {
            out.append(.header("Skills & Subagents · Last \(st.days) Days"))
            out += mine.prefix(max(2, 4 - trim)).map(MenuEntry.setupItem)
        }
        if !servers.isEmpty {
            out.append(.header("MCP Servers"))
            out += servers.prefix(max(2, 4 - trim)).map(MenuEntry.setupItem)
        }
        let unused = live.filter { $0.verdict == "unused" && $0.kind != "hook" }
        if !unused.isEmpty {
            out.append(.header("Not Used in \(st.days) Days"))
            for kind in ["skill", "agent", "mcp", "command"] {
                let these = unused.filter { $0.kind == kind }
                if !these.isEmpty { out.append(.unused(kind: kind, items: these)) }
            }
        }
        let hooks = live.filter { $0.kind == "hook" }
        if !hooks.isEmpty, trim < 3 {
            out.append(.header("Hooks"))
            out.append(.hooks(Array(hooks.prefix(max(1, 5 - trim)))))
        }
        let off = st.items.filter(\.isOff)
        if !off.isEmpty {
            out.append(.header("Turned Off"))
            out += off.prefix(8).map(MenuEntry.setupItem)
        }
        out.append(.separator)
        out.append(.openInsights(.setup, "Open in Insights…"))
        return out
    }

    /// The left-click menu ends with its content. Open, refresh, settings and quit live on right-click, where
    /// menu-bar apps keep them; their key equivalents still work here through hidden items.
    static func footer() -> [MenuEntry] { [] }

    /// Right-click on Pip: the app's own commands, kept out of the way of what the agents are doing.
    @MainActor static func contextMenu(hasStore: Bool) -> [MenuEntry] {
        var out: [MenuEntry] = [.action(.openViewer)]
        if hasStore { out.append(.action(.showAllSessions)) }
        out += [.action(.refresh), .separator]
        if Updater.shared.available { out.append(.action(.checkUpdates)) }
        out += [.action(.settings), .action(.quit)]
        return out
    }

    static func shortReason(_ p: ProviderSummary) -> String {
        switch p.provider {
        case "codex": p.plan.map { "Codex reports no usage windows for the \($0.capitalized) plan" }
            ?? "Codex hasn't reported limits on this Mac yet"
        case "claude-code": "Waiting for Claude Code to report"
        default: "\(p.label) doesn't report limits locally"
        }
    }

    /// The sessions with a live process: waiting first, then working. Idle processes and finished runs are
    /// not rows here; the menu shows what is happening, All Sessions shows what happened.
    static func sessionLines(_ s: Snapshot, now: Date) -> [SessionLine] {
        var out: [SessionLine] = []
        let runs = Dictionary(s.runs.map { ($0.sessionId, $0) }, uniquingKeysWith: { a, _ in a })
        let order: [LiveStatus: Int] = [.waiting: 0, .busy: 1, .idle: 2]
        let live = (s.live ?? []).sorted { (order[$0.status] ?? 3, $1.startedAt ?? .distantPast) < (order[$1.status] ?? 3, $0.startedAt ?? .distantPast) }
        for l in live {
            // An agent idle for hours is a tab someone forgot, not a session happening: it stays off the menu.
            if l.status == .idle, let since = l.statusSince, now.timeIntervalSince(since) > 6 * 3600 { continue }
            let run = runs[l.sessionId]
            let repo = run?.repo ?? l.project
            // Named by Claude Code's own title when it has one; the repo moves to the line under it.
            var d = l.name != nil ? [repo ?? AgentNames.label(l.agentTool)] : [AgentNames.label(l.agentTool)]
            if l.name == nil, let m = run?.models?.first { d.append(Format.modelName(m)) }
            if l.status == .idle {
                // An open agent with nothing happening: still a terminal to jump to, so it stays, in grey.
                d.append("Idle" + (l.statusSince.map { " \(Format.duration(now.timeIntervalSince($0)))" } ?? ""))
            } else {
                d.append(Format.truncate(detail(run, status: l.status, since: l.statusSince, now: now), max: 26))
            }
            out.append(SessionLine(
                id: l.sessionId, title: Format.truncate(l.name ?? repo ?? "Session", max: 40),
                detail: d.filter { !$0.isEmpty }.joined(separator: " · "),
                dot: l.status == .waiting ? .waiting : l.status == .idle ? .idle : .working, sessionId: l.sessionId,
                seq: run?.lastEvent?.seq, openable: l.inStore, pid: l.pid, run: run, live: l))
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
