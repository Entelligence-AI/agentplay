import Foundation

/// Links into the local viewer: `/?session=<id>&at=<seq>` (viewer/src/explorer/SessionExplorer.tsx `useSeekParam`).
/// `at` is the whole-session seq, which the main lens keeps for every lane, so no `sub` is needed.
public enum ViewerLink {
    /// RFC 3986 unreserved only. `URLComponents` leaves `+`, `&`-free reserved characters and `:` alone, and the
    /// viewer's `URLSearchParams` reads a bare `+` as a space, so ids are encoded strictly.
    static let unreserved = CharacterSet(charactersIn:
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    public static func encode(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: unreserved) ?? ""
    }

    /// The viewer's Insights pages: "overview", "health" (Run health and its fixes) or "autotune".
    public static func insights(base: URL, page: String = "health") -> URL {
        var c = URLComponents(url: base, resolvingAgainstBaseURL: false) ?? URLComponents()
        c.path = "/"
        c.percentEncodedQuery = "tab=insights&ipage=\(encode(page))"
        c.fragment = nil
        return c.url ?? base
    }

    public static func url(base: URL, sessionId: String, seq: Int? = nil, errorsTab: Bool = false) -> URL {
        var q = "session=\(encode(sessionId))"
        if let seq, seq >= 0 { q += "&at=\(seq)" }
        if errorsTab { q += "&stab=errors" }
        var c = URLComponents(url: base, resolvingAgainstBaseURL: false) ?? URLComponents()
        c.path = "/"
        c.percentEncodedQuery = q
        c.fragment = nil
        return c.url ?? base
    }

    /// Whether a URL is one the companion would open: loopback http on a port, nothing else.
    public static func isLocalViewer(_ url: URL) -> Bool {
        guard url.scheme == "http", let host = url.host, url.port != nil else { return false }
        return host == "127.0.0.1" || host == "localhost" || host == "::1"
    }

    /// The port in the viewer's start line, `Viewer → http://127.0.0.1:7317/   (Ctrl+C to stop)`.
    /// The line may carry ANSI bold codes when a terminal is attached.
    public static func parseStartLine(_ line: String) -> URL? {
        let plain = line.replacingOccurrences(of: "\u{1B}\\[[0-9;]*m", with: "", options: .regularExpression)
        guard let r = plain.range(of: #"http://127\.0\.0\.1:(\d{1,5})/"#, options: .regularExpression) else { return nil }
        guard let url = URL(string: String(plain[r])), let port = url.port, (1...65535).contains(port) else { return nil }
        return url
    }
}

public enum Format {
    public static func relative(_ date: Date?, now: Date) -> String {
        guard let date else { return "time unknown" }
        let s = now.timeIntervalSince(date)
        if s < -60 { return "in the future (clock changed?)" }
        if s < 45 { return "just now" }
        if s < 3600 { return "\(max(1, Int(s / 60)))m ago" }
        if s < 86400 { return "\(Int(s / 3600))h ago" }
        let days = Int(s / 86400)
        return days == 1 ? "yesterday" : "\(days)d ago"
    }

    /// Compact elapsed duration: `4m`, `1h 12m`, `3d`.
    public static func duration(_ seconds: TimeInterval) -> String {
        let s = max(0, Int(seconds))
        if s < 60 { return "\(s)s" }
        if s < 3600 { return "\(s / 60)m" }
        if s < 86400 { return s % 3600 / 60 == 0 ? "\(s / 3600)h" : "\(s / 3600)h \(s % 3600 / 60)m" }
        return "\(s / 86400)d"
    }

    public static func tokens(_ n: Int) -> String {
        if n >= 1_000_000_000 { return String(format: "%.2fB", Double(n) / 1_000_000_000) }
        if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
        if n >= 10_000 { return "\(n / 1000)k" }
        if n >= 1000 { return String(format: "%.1fk", Double(n) / 1000) }
        return "\(n)"
    }

    /// Run health points: one decimal below 10, whole numbers above.
    public static func points(_ p: Double) -> String {
        p < 10 ? String(format: "%.1f", p) : String(Int(p.rounded()))
    }

    public static func percent(_ p: Double) -> String {
        p.rounded() == p ? "\(Int(p))%" : String(format: "%.0f%%", p)
    }

    /// `$0.42`, `$12.30`, `$1,240`.
    public static func money(_ usd: Double) -> String {
        if usd >= 1000 { return "$" + String(format: "%.0f", usd).replacingOccurrences(of: #"(\d)(?=(\d{3})+$)"#, with: "$1,", options: .regularExpression) }
        if usd >= 100 { return String(format: "$%.0f", usd) }
        return String(format: "$%.2f", usd)
    }

    /// `Mon 4:15 PM`.
    public static func weekdayTime(_ date: Date, timeZone: TimeZone = .current) -> String {
        let f = DateFormatter()
        f.timeZone = timeZone
        f.locale = Locale.current
        f.setLocalizedDateFormatFromTemplate("EEE j:mm")
        return f.string(from: date)
    }

    public static func clock(_ date: Date?, timeZone: TimeZone = .current) -> String {
        guard let date else { return "unknown" }
        let f = DateFormatter()
        f.timeZone = timeZone
        f.locale = Locale.current
        f.dateStyle = Calendar.current.isDate(date, inSameDayAs: Date()) ? .none : .short
        f.timeStyle = .short
        return f.string(from: date)
    }

    /// A model id as people say it: `claude-opus-5-5` is "Opus 5.5", `gpt-5.5-codex` is "GPT 5.5 Codex",
    /// `claude-sonnet-5` is "Sonnet 5". Date stamps (`20250514`) are dropped. Unknown shapes come back as they are.
    public static func modelName(_ id: String) -> String {
        var parts = id.lowercased().split(whereSeparator: { $0 == "-" || $0 == "_" }).map(String.init)
        if parts.first == "claude" { parts.removeFirst() }
        guard !parts.isEmpty else { return id }
        var words: [String] = []
        var version: [String] = []
        for p in parts {
            let digits = p.filter(\.isNumber)
            if digits.count >= 8 { continue }
            if p.allSatisfy({ $0.isNumber || $0 == "." }) { version.append(p); continue }
            if !version.isEmpty { words.append(version.joined(separator: ".")); version = [] }
            words.append(p == "gpt" ? "GPT" : p.prefix(1).uppercased() + p.dropFirst())
        }
        if !version.isEmpty { words.append(version.joined(separator: ".")) }
        return words.joined(separator: " ")
    }

    /// Middle-truncate a label (`payments-…-api`). Repo names are shown truncated by default.
    /// Names, joined, cut at a whole name: "a, b and 3 more". Never mid-word.
    public static func names(_ names: [String], max: Int = 34) -> String {
        var out: [String] = []
        for (i, n) in names.enumerated() {
            let rest = names.count - i - 1
            let next = (out + [n]).joined(separator: ", ") + (rest > 0 ? " and \(rest) more" : "")
            if !out.isEmpty, next.count > max { return out.joined(separator: ", ") + " and \(names.count - out.count) more" }
            out.append(n)
        }
        return out.joined(separator: ", ")
    }

    public static func truncate(_ s: String?, max: Int = 22) -> String {
        guard let s, !s.isEmpty else { return "" }
        guard s.count > max, max > 3 else { return s }
        let head = (max - 1) / 2, tail = max - 1 - head
        return String(s.prefix(head)) + "…" + String(s.suffix(tail))
    }

    public static func outcome(_ o: String?) -> String? {
        switch o {
        case "pr_created": "Opened a PR"
        case "verified": "Ended on a passing check"
        case "unresolved_errors": "Ended with unresolved errors"
        case "interrupted": "Interrupted"
        case "blocked": "Ended blocked"
        case "incomplete": "Ended mid-step"
        case "completed": "Completed"
        default: nil
        }
    }

    public static func errClass(_ c: String?) -> String {
        switch c {
        case "exit_code": "non-zero exit"
        case "timeout": "timed out"
        case "not_found": "not found"
        case "permission_denied": "permission denied"
        case "rate_limit": "rate limited"
        case "usage_limit": "usage limit hit"
        case let c?: c.replacingOccurrences(of: "_", with: " ")
        case nil: "error"
        }
    }
}

/// Text that leaves the popover (clipboard, notifications). Built from redacted fields only:
/// agent, repo name, command family, counts, times. Never file paths, titles or session ids in notifications.
public enum Redacted {
    public static func runSummary(_ r: RunActivity, now: Date, includeRepo: Bool = true) -> String {
        var lines = ["\(r.agentLabel) run" + (includeRepo && r.repo != nil ? " in \(r.repo!)" : "")]
        if let started = r.startedAt {
            lines.append("Started \(Format.relative(started, now: now)), last event \(Format.relative(r.lastEventAt, now: now)).")
        }
        let c = r.counts
        lines.append("\(c.calls ?? 0) tool calls, \(c.errors ?? 0) errors, \(c.retries ?? 0) retries, \(c.compactions ?? 0) compactions.")
        if let e = r.lastError {
            lines.append("Last error: \(e.label) (\(Format.errClass(e.errClass))).")
        }
        if let o = Format.outcome(r.outcome) { lines.append("Outcome: \(o).") }
        lines.append("From AgentPlay's local map. No prompt text, file contents or arguments.")
        return lines.joined(separator: "\n")
    }

    public struct Note: Equatable, Sendable {
        public var title: String
        public var body: String
    }

    /// "docs-site needs you" only when the user allows repo names in notifications.
    public static func needsYou(_ l: LiveSession, includeRepo: Bool) -> Note {
        let agent = AgentNames.label(l.agentTool)
        if includeRepo, let p = l.project {
            return Note(title: "\(Format.truncate(p, max: 32)) needs you", body: "\(agent) is waiting for your input.")
        }
        return Note(title: "\(agent) needs you", body: "A session is waiting for your input.")
    }

    /// Worth a banner only after real work: a quick reply isn't news.
    public static let finishedMinimum: TimeInterval = 120

    /// "payments-api is done", "Worked 14m · 32 tool calls · 1 error · last: pnpm test". Counts and tool or
    /// command names only, from the map; nothing from the conversation.
    public static func finished(_ l: LiveSession, run: RunActivity?, busySince: Date?, includeRepo: Bool, now: Date) -> Note? {
        let worked = busySince.map { now.timeIntervalSince($0) }
        guard let worked, worked >= finishedMinimum else { return nil }
        let agent = AgentNames.label(l.agentTool)
        let title = includeRepo && l.project != nil ? "\(Format.truncate(l.project, max: 32)) is done" : "\(agent) is done"
        var parts = ["Worked \(Format.duration(worked))"]
        if let c = run?.counts {
            if let n = c.calls, n > 0 { parts.append(n == 1 ? "1 tool call" : "\(n) tool calls") }
            if let e = c.errors, e > 0 { parts.append(e == 1 ? "1 error" : "\(e) errors") }
        }
        if let e = run?.lastEvent { parts.append("last: \(e.err ? "\(e.label) failed" : e.label)") }
        return Note(title: title, body: parts.joined(separator: " · "))
    }

    public static func notification(for a: AlertCandidate, repo: String?, includeRepo: Bool) -> Note {
        let agent = AgentNames.label(a.agentTool)
        let place = includeRepo && repo != nil ? " in \(Format.truncate(repo, max: 24))" : ""
        switch a.rule {
        case .repeatedFailure:
            let n = a.count ?? 3
            return Note(title: "A run worth checking · \(agent)",
                        body: "\(a.command ?? "A command") failed \(n) times in a row\(place).")
        case .unresolvedEnd:
            return Note(title: "Run ended with errors · \(agent)",
                        body: "The run\(place) ended with unresolved errors.")
        case .unknown:
            return Note(title: "AgentPlay · \(agent)", body: "Something in a run\(place) may need a look.")
        }
    }
}
