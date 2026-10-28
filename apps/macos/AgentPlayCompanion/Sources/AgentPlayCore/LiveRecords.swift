import Foundation

/// Claude Code's own live-process records, `~/.claude/sessions/<pid>.json`, read directly by the app.
///
/// The core's snapshot reads the same files, but only when it runs (every minute or so). These are a few
/// hundred bytes each, so the app re-reads them the moment the folder changes: a session that starts waiting
/// on you shows up in about a second, without running the core. Kept: the folder name of `cwd` and Claude Code's
/// title for the session (the menu names rows by it). The full path and the socket are never read in.
public enum LiveRecords {
    /// One record, or nil when it is not a live Claude Code session record.
    public static func parse(_ data: Data) -> LiveSession? {
        guard let rec = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let pid = rec["pid"] as? Int, pid > 1, pid <= Int(Int32.max),
              let sid = rec["sessionId"] as? String, !sid.isEmpty,
              let raw = rec["status"] as? String, let status = LiveStatus(rawValue: raw) else { return nil }
        func ms(_ v: Any?) -> Date? {
            guard let n = v as? Double, n > 0 else { return nil }
            return Date(timeIntervalSince1970: n / 1000)
        }
        let project = (rec["cwd"] as? String).flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0).lastPathComponent }
        var s = LiveSession(agentTool: "claude-code", sessionId: "local:claude-code:\(sid)", status: status,
                            pid: Int32(pid), project: project, startedAt: ms(rec["startedAt"]),
                            statusSince: ms(rec["statusUpdatedAt"]) ?? ms(rec["updatedAt"]), inStore: false)
        s.entrypoint = rec["entrypoint"] as? String
        if let n = (rec["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !n.isEmpty {
            s.name = String(n.prefix(80))
        }
        return s
    }

    /// Every record in `dir` whose process is still alive, waiting first.
    public static func read(dir: URL, isAlive: (Int32) -> Bool = processIsAlive) -> [LiveSession] {
        guard let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return [] }
        let out = files.filter { $0.pathExtension == "json" }.sorted { $0.path < $1.path }.prefix(200)
            .compactMap { (try? Data(contentsOf: $0)).flatMap(parse) }
            .filter { $0.pid.map(isAlive) ?? false }
        return sorted(out)
    }

    /// Fresh records replace the snapshot's live list; `inStore` comes from the snapshot, which knows the store.
    public static func merge(snapshot live: [LiveSession]?, records: [LiveSession]) -> [LiveSession] {
        let built = Dictionary((live ?? []).map { ($0.sessionId, $0.inStore) }, uniquingKeysWith: { a, _ in a })
        return sorted(records.map { r in
            var r = r
            r.inStore = built[r.sessionId] ?? false
            return r
        })
    }

    /// Sessions that are waiting now and weren't waiting in `before`. A session already waiting when we first
    /// look (`before` is nil) is not news.
    public static func newlyWaiting(before: [LiveSession]?, after: [LiveSession]) -> [LiveSession] {
        guard let before else { return [] }
        let was = Dictionary(before.map { ($0.sessionId, $0.status) }, uniquingKeysWith: { a, _ in a })
        return after.filter { $0.status == .waiting && was[$0.sessionId] != .waiting }
    }

    public static func processIsAlive(_ pid: Int32) -> Bool {
        kill(pid, 0) == 0 || errno == EPERM
    }

    static func sorted(_ s: [LiveSession]) -> [LiveSession] {
        let order: [LiveStatus: Int] = [.waiting: 0, .busy: 1, .idle: 2]
        return s.sorted { (order[$0.status] ?? 3, $0.startedAt ?? .distantPast) < (order[$1.status] ?? 3, $1.startedAt ?? .distantPast) }
    }
}
