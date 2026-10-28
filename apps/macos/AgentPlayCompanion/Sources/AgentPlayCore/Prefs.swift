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
    /// Opt-in Claude usage from the user's Claude account (ClaudeAccount). Optional so older saved prefs decode.
    public var claudeAccountUsage: Bool? = nil
    /// On unless switched off: the limits are the first thing people open the menu for, and sessions run from
    /// an editor's agent panel have no other source for them.
    public var usesClaudeAccount: Bool { claudeAccountUsage ?? true }
    /// Pip in the menu bar (else the two plain bars), and the line beside it when something happens.
    public var pipEnabled: Bool? = nil
    public var pipIslandEnabled: Bool? = nil
    public var showsPip: Bool { pipEnabled ?? true }
    /// Claude accounts saved for switching: names and last-seen usage only; logins live in the Keychain.
    public var claudeAccounts: [ClaudeSwitch.Saved]? = nil
    /// Save each new account Claude Code signs in to (off: only when the user chooses Save).
    public var rememberClaudeAccounts: Bool? = nil
    /// Ask Claude (Haiku, through the person's own Claude Code) for suggestions once a day. Off unless turned on.
    public var claudeSuggestions: Bool? = nil
    /// How alerts appear: "pip" (AgentPlay's animated banner), "system" (macOS notifications) or "both".
    public var alertStyle: String? = nil
    public var alertsAs: String { alertStyle ?? "pip" }
    /// Pip walks along a strip under the menu bar while agents work. Off unless turned on.
    public var pipWalker: Bool? = nil
    public var walksWhileWorking: Bool { pipWalker ?? false }
    public var asksClaudeForSuggestions: Bool { claudeSuggestions ?? false }
    public var showsPipIsland: Bool { pipIslandEnabled ?? true }

    public init(now: Date = Date()) { history = AlertHistory(watermark: now) }

    public func isLearnDismissed(_ key: String, now: Date) -> Bool {
        guard let d = dismissedLearn[key] else { return false }
        return now.timeIntervalSince(d) < 14 * 86400
    }

    /// The variables `discover.Homes.default()` reads (python/src/agentplay/discover.py), comma-separated.
    /// Every agent the core knows is here, so a folder added for any source in Settings reaches it.
    public static let homesVariable: [String: String] = [
        "claude-code": "AGENTPLAY_CLAUDE_HOMES", "codex": "AGENTPLAY_CODEX_HOMES", "cursor": "AGENTPLAY_CURSOR_HOMES",
        "copilot": "AGENTPLAY_COPILOT_HOMES", "copilot-vscode": "AGENTPLAY_COPILOT_VSCODE_HOMES",
        "opencode": "AGENTPLAY_OPENCODE_HOMES", "kiro": "AGENTPLAY_KIRO_HOMES",
        "gemini-cli": "AGENTPLAY_GEMINI_HOMES", "antigravity-cli": "AGENTPLAY_ANTIGRAVITY_HOMES",
    ]

    /// Where the core looks when nothing overrides it, as `Homes.default()` works it out on a Mac. The core
    /// expands `~` itself; `COPILOT_HOME` and `XDG_DATA_HOME` are the two other variables it consults.
    static func defaultHomes(_ agent: String, parent: [String: String]) -> [String] {
        switch agent {
        case "claude-code": return ["~/.claude"]
        case "codex": return ["~/.codex"]
        case "cursor": return ["~/.cursor"]
        case "copilot":
            let home = parent["COPILOT_HOME"]?.trimmingCharacters(in: .whitespaces) ?? ""
            return [home.isEmpty ? "~/.copilot" : home]
        case "copilot-vscode":
            return ["Code", "Code - Insiders", "VSCodium"].map { "~/Library/Application Support/\($0)/User" }
        case "opencode":
            let xdg = parent["XDG_DATA_HOME"] ?? ""
            return [(xdg.isEmpty ? "~/.local/share" : xdg) + "/opencode"]
        case "kiro": return ["~/.kiro"]
        case "gemini-cli": return ["~/.gemini"]
        case "antigravity-cli": return ["~/.gemini/antigravity-cli"]
        default: return []
        }
    }

    /// `AGENTPLAY_CLAUDE_HOMES` etc. for the core: what it would scan anyway, plus anything the user added.
    public var folderEnvironment: [String: String] { folderEnvironment(parent: ProcessInfo.processInfo.environment) }

    /// The homes the core already has (an `AGENTPLAY_<AGENT>_HOMES` the user set, else its defaults) come first,
    /// so adding a folder never takes one away. Only agents with an added folder get a variable; the rest keep
    /// whatever the parent environment passes through.
    public func folderEnvironment(parent: [String: String]) -> [String: String] {
        var env: [String: String] = [:]
        for (agent, paths) in customFolders where !paths.isEmpty {
            guard let key = Self.homesVariable[agent] else { continue }
            let inherited = (parent[key] ?? "").split(separator: ",")
                .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
            var all = inherited.isEmpty ? Self.defaultHomes(agent, parent: parent) : inherited
            for p in paths where !all.contains(p) { all.append(p) }
            env[key] = all.joined(separator: ",")
        }
        return env
    }
}

public final class PrefsStore: @unchecked Sendable {
    public static let domain = "ai.entelligence.agentplay.companion"
    private let defaults: UserDefaults
    /// The same domain, for small UI state (which blocks are open) that isn't worth a schema change.
    public var ui: UserDefaults { defaults }
    private let key = "prefs.v1"
    private let lock = NSLock()

    public init(defaults: UserDefaults? = nil) {
        // Inside the app bundle the domain is the app's own, which is `.standard`; macOS refuses it as a suite.
        // Outside it (tests, `swift run`) a named suite keeps the same domain.
        self.defaults = defaults ?? (Bundle.main.bundleIdentifier == Self.domain ? .standard
            : UserDefaults(suiteName: Self.domain) ?? .standard)
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
