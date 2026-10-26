import Foundation

/// The opt-in "Live Claude limits" bridge: points Claude Code's documented `statusLine` at a tiny script that
/// saves the statusline JSON for the companion and then runs whatever statusline the user had before.
///
/// Writes exactly two things, both shown to the user first: `statusLine` in `~/.claude/settings.json` (with a
/// backup next to it), and the script plus the saved previous command under `~/.agentplay/live/`.
/// Removing it restores the previous `statusLine` only if the current one is still ours.
public struct ClaudeBridge: Sendable {
    public var claudeSettings: URL
    public var agentplayHome: URL

    public init(claudeSettings: URL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/settings.json"),
                agentplayHome: URL = ClaudeBridge.defaultAgentplayHome()) {
        self.claudeSettings = claudeSettings
        self.agentplayHome = agentplayHome
    }

    public static func defaultAgentplayHome() -> URL {
        if let h = ProcessInfo.processInfo.environment["AGENTPLAY_HOME"], !h.isEmpty { return URL(fileURLWithPath: h) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".agentplay")
    }

    public var scriptURL: URL { agentplayHome.appendingPathComponent("live/claude-statusline-bridge.sh") }
    var previousURL: URL { agentplayHome.appendingPathComponent("live/claude-statusline.previous") }
    var previousJSONURL: URL { agentplayHome.appendingPathComponent("live/claude-statusline.previous.json") }
    public var backupURL: URL { claudeSettings.appendingPathExtension("agentplay-backup") }

    public enum State: Equatable, Sendable { case off, on, otherStatusLine(String) }

    public enum BridgeError: Error, Equatable, LocalizedError {
        case unreadableSettings(String)
        case notOurs
        public var errorDescription: String? {
            switch self {
            case .unreadableSettings(let why): "Can't read ~/.claude/settings.json (\(why)). Nothing was changed."
            case .notOurs: "Claude Code's statusLine was changed by something else since; AgentPlay left it alone."
            }
        }
    }

    func readSettings() throws -> [String: Any] {
        guard FileManager.default.fileExists(atPath: claudeSettings.path) else { return [:] }
        do {
            let data = try Data(contentsOf: claudeSettings)
            if data.isEmpty { return [:] }
            guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw BridgeError.unreadableSettings("not a JSON object")
            }
            return obj
        } catch let e as BridgeError { throw e } catch {
            throw BridgeError.unreadableSettings("invalid JSON")
        }
    }

    public func state() -> State {
        guard let s = try? readSettings(), let line = s["statusLine"] as? [String: Any],
              let cmd = line["command"] as? String else { return .off }
        return cmd.contains(scriptURL.path) ? .on : .otherStatusLine(cmd)
    }

    /// The exact change, for the confirmation sheet: `(before, after)` of the `statusLine` entry.
    public func preview() throws -> (before: String, after: String) {
        let s = try readSettings()
        return (Self.pretty(s["statusLine"]) ?? "(none)", Self.pretty(ourStatusLine) ?? "")
    }

    var ourStatusLine: [String: Any] { ["type": "command", "command": "/bin/sh '\(scriptURL.path)'", "padding": 0] }

    /// Claude Code runs the script without our environment, so the folder it writes to is fixed at install.
    public func pinned(_ script: String) -> String {
        let dir = agentplayHome.appendingPathComponent("live/claude").path.replacingOccurrences(of: "'", with: "'\\''")
        return script.replacingOccurrences(of: "\"${AGENTPLAY_HOME:-$HOME/.agentplay}/live/claude\"", with: "'\(dir)'")
    }

    /// Rewrites an installed script with this build's version; settings are left alone.
    public func refreshScript(_ script: String) {
        guard case .on = state() else { return }
        let want = pinned(script)
        if (try? String(contentsOf: scriptURL, encoding: .utf8)) != want {
            try? want.write(to: scriptURL, atomically: true, encoding: .utf8)
        }
    }

    public func install(script: String) throws {
        var s = try readSettings()
        let fm = FileManager.default
        try fm.createDirectory(at: scriptURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try pinned(script).write(to: scriptURL, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptURL.path)
        // Remember what was there, so it keeps running through the bridge and comes back on removal.
        if case .on = state() {} else {
            let prev = s["statusLine"] as? [String: Any]
            let cmd = (prev?["type"] as? String == "command") ? prev?["command"] as? String : nil
            try (cmd ?? "").write(to: previousURL, atomically: true, encoding: .utf8)
            let prevData = try JSONSerialization.data(withJSONObject: prev ?? [:], options: [.sortedKeys])
            try prevData.write(to: previousJSONURL, options: .atomic)
        }
        if fm.fileExists(atPath: claudeSettings.path) && !fm.fileExists(atPath: backupURL.path) {
            try fm.copyItem(at: claudeSettings.resolvingSymlinksInPath(), to: backupURL)
        }
        s["statusLine"] = ourStatusLine
        try write(s)
    }

    public func uninstall() throws {
        var s = try readSettings()
        guard case .on = state() else {
            if case .off = state() { cleanup(); return }
            throw BridgeError.notOurs
        }
        if let data = try? Data(contentsOf: previousJSONURL),
           let prev = try? JSONSerialization.jsonObject(with: data) as? [String: Any], !prev.isEmpty {
            s["statusLine"] = prev
        } else {
            s.removeValue(forKey: "statusLine")
        }
        try write(s)
        cleanup()
    }

    func cleanup() {
        for u in [scriptURL, previousURL, previousJSONURL] { try? FileManager.default.removeItem(at: u) }
    }

    func write(_ s: [String: Any]) throws {
        try FileManager.default.createDirectory(at: claudeSettings.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: s, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        // Through a symlink (dotfiles), not over it: an atomic write would replace the link with a plain file.
        try data.write(to: claudeSettings.resolvingSymlinksInPath(), options: .atomic)
    }

    static func pretty(_ v: Any?) -> String? {
        guard let v, JSONSerialization.isValidJSONObject(v),
              let d = try? JSONSerialization.data(withJSONObject: v, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        else { return nil }
        return String(decoding: d, as: UTF8.self)
    }
}
