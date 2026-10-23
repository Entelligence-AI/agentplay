import Foundation

/// Everything the companion remembers. Stored as one JSON value in its own UserDefaults domain
/// (`ai.entelligence.agentplay.companion`). No session content: ids, pattern keys and times only.
public struct CompanionPrefs: Codable, Sendable, Equatable {
    public var onboardingDone = false
    public var showActivityInMenuBar = true
    public var soundOnManualActions = false
    public var alerts = AlertSettings()
    public var history: AlertHistory
    public var pinnedSessions: [String] = []
    /// Learn cards dismissed, by pattern key, with when.
    public var dismissedLearn: [String: Date] = [:]
    /// Extra transcript folders the user chose, by agent (passed as `AGENTPLAY_<AGENT>_HOMES`).
    public var customFolders: [String: [String]] = [:]

    public init(now: Date = Date()) { history = AlertHistory(watermark: now) }

    public func isLearnDismissed(_ key: String, now: Date) -> Bool {
        guard let d = dismissedLearn[key] else { return false }
        return now.timeIntervalSince(d) < 14 * 86400
    }

    /// The variables `discover.Homes.default()` reads (python/src/agentplay/discover.py), comma-separated.
    public static let homesVariable: [String: String] = [
        "claude-code": "AGENTPLAY_CLAUDE_HOMES", "codex": "AGENTPLAY_CODEX_HOMES", "cursor": "AGENTPLAY_CURSOR_HOMES",
        "copilot": "AGENTPLAY_COPILOT_HOMES", "copilot-vscode": "AGENTPLAY_COPILOT_VSCODE_HOMES",
        "opencode": "AGENTPLAY_OPENCODE_HOMES", "kiro": "AGENTPLAY_KIRO_HOMES",
    ]

    /// `AGENTPLAY_CLAUDE_HOMES` etc. for the core: the default home plus anything the user added.
    public var folderEnvironment: [String: String] {
        var env: [String: String] = [:]
        let defaults: [String: String] = ["claude-code": "~/.claude", "codex": "~/.codex", "cursor": "~/.cursor",
                                          "copilot": "~/.copilot", "kiro": "~/.kiro"]
        for (agent, paths) in customFolders where !paths.isEmpty {
            guard let key = Self.homesVariable[agent] else { continue }
            let all = (defaults[agent].map { [$0] } ?? []) + paths
            env[key] = all.joined(separator: ",")
        }
        return env
    }
}

public final class PrefsStore: @unchecked Sendable {
    public static let domain = "ai.entelligence.agentplay.companion"
    private let defaults: UserDefaults
    private let key = "prefs.v1"
    private let lock = NSLock()

    public init(defaults: UserDefaults? = nil) {
        self.defaults = defaults ?? UserDefaults(suiteName: Self.domain) ?? .standard
    }

    public func load(now: Date = Date()) -> CompanionPrefs {
        lock.withLock {
            guard let data = defaults.data(forKey: key),
                  let p = try? JSONDecoder().decode(CompanionPrefs.self, from: data) else {
                // First launch: the watermark is now, so already-recorded history never alerts.
                return CompanionPrefs(now: now)
            }
            return p
        }
    }

    public func save(_ p: CompanionPrefs) {
        lock.withLock {
            if let data = try? JSONEncoder().encode(p) { defaults.set(data, forKey: key) }
        }
    }

    /// Settings → Privacy → "Forget alert history".
    public func purgeHistory(now: Date = Date()) {
        var p = load(now: now)
        p.history.purge(now: now)
        save(p)
    }

    public func reset() { lock.withLock { defaults.removeObject(forKey: key) } }
}
