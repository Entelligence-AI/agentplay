import Foundation
import Security

/// Switching the Claude account Claude Code is signed in as, the way claude-swap does it, from the menu.
///
/// A saved account is two things: Claude Code's login (the JSON in its Keychain item "Claude Code-credentials")
/// and its `oauthAccount` block from `~/.claude.json`. The login is kept in AgentPlay's own Keychain item per
/// account ("AgentPlay Claude accounts", account = accountUuid); names, organization and last-seen usage live in
/// prefs, never the login. Every Keychain read and write goes through `/usr/bin/security`, which is what Claude
/// Code itself uses, so the items' access lists don't change and nobody gets new prompts; writes go through
/// `security -i` on stdin so a token never shows in `ps`.
///
/// Rules, learned from claude-swap: save the outgoing account's newest login before switching (Claude Code
/// rotates tokens); keep the live MCP server logins (`mcpOAuth`) rather than a saved copy's; never switch onto
/// a login whose refresh token has expired; never while a session is mid-step; never call /logout.
public enum ClaudeSwitch {
    public static let store = "AgentPlay Claude accounts"

    /// Where logins live. `.live` is the real thing; tests point it at throwaway Keychain items and a temp home.
    public struct Env: Sendable {
        public var home: URL
        public var claudeService: String
        public var storeService: String
        public var user: String
        public static var live: Env {
            Env(home: FileManager.default.homeDirectoryForCurrentUser, claudeService: ClaudeAccount.keychainService,
                storeService: store, user: NSUserName())
        }
        public init(home: URL, claudeService: String, storeService: String, user: String) {
            self.home = home; self.claudeService = claudeService; self.storeService = storeService; self.user = user
        }
    }

    public struct Saved: Codable, Equatable, Sendable, Identifiable {
        public var accountUuid: String
        public var email: String?
        public var organization: String?
        public var seatTier: String?
        public var savedAt: Date
        public var lastSession: Double?   // percent, when last active
        public var lastWeekly: Double?
        public var lastSeen: Date?
        public var id: String { accountUuid }
        public init(accountUuid: String, email: String?, organization: String?, seatTier: String?, savedAt: Date) {
            self.accountUuid = accountUuid; self.email = email; self.organization = organization
            self.seatTier = seatTier; self.savedAt = savedAt
        }
        public var title: String { email ?? organization ?? "Claude account" }
    }

    public enum Failure: Error, Equatable, Sendable {
        case notSignedIn, notSaved, busy, expired, keychain(String), claudeJSON
        public var message: String {
            switch self {
            case .notSignedIn: "Claude Code isn't signed in on this Mac."
            case .notSaved: "That account's login isn't saved here anymore. Sign in with it once and save it again."
            case .busy: "A session is mid-step. Switch when your sessions are idle or waiting, so no reply is cut off."
            case .expired: "That account's saved login has expired. Sign in with it in Claude Code (/login) and save it again."
            case .keychain(let m): "The Keychain refused: \(m)"
            case .claudeJSON: "Couldn't update ~/.claude.json, so nothing was changed."
            }
        }
    }

    // MARK: pure parts

    /// The target's Claude login with the live MCP logins kept: MCP servers are per machine, not per account.
    public static func merged(target: Data, live: Data?) -> Data? {
        guard var t = try? JSONSerialization.jsonObject(with: target) as? [String: Any], t["claudeAiOauth"] != nil else { return nil }
        if let live, let l = try? JSONSerialization.jsonObject(with: live) as? [String: Any], let mcp = l["mcpOAuth"] {
            t["mcpOAuth"] = mcp
        }
        return try? JSONSerialization.data(withJSONObject: t, options: [.sortedKeys])
    }

    /// `~/.claude.json` with only `oauthAccount` replaced.
    public static func withAccount(_ claudeJSON: Data, oauthAccount: Data) -> Data? {
        guard var obj = try? JSONSerialization.jsonObject(with: claudeJSON) as? [String: Any],
              let acct = try? JSONSerialization.jsonObject(with: oauthAccount) as? [String: Any] else { return nil }
        obj["oauthAccount"] = acct
        return try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys])
    }

    /// The refresh token is what lets Claude Code renew the access token; once it's past, the login is dead.
    public static func isUsable(_ login: Data, now: Date) -> Bool {
        guard let o = (try? JSONSerialization.jsonObject(with: login) as? [String: Any])?["claudeAiOauth"] as? [String: Any],
              let token = o["accessToken"] as? String, !token.isEmpty else { return false }
        if let ms = o["refreshTokenExpiresAt"] as? Double { return Date(timeIntervalSince1970: ms / 1000) > now }
        return o["refreshToken"] != nil || (o["expiresAt"] as? Double).map { Date(timeIntervalSince1970: $0 / 1000) > now } ?? false
    }

    /// One `security -i` command line: the value double-quoted with its quotes and backslashes escaped.
    public static func securityLine(service: String, account: String, label: String?, value: String) -> String {
        func q(_ s: String) -> String { "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\"" }
        var line = "add-generic-password -U -s \(q(service)) -a \(q(account))"
        if let label { line += " -l \(q(label))" }
        return line + " -w \(q(value))\n"
    }

    /// Where the `claude` command is: the usual install places, then a login shell's PATH.
    public static func claudeExecutable() -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        for p in ["\(home)/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude", "\(home)/.claude/local/claude"]
            where FileManager.default.isExecutableFile(atPath: p) { return p }
        let sh = Process()
        sh.executableURL = URL(fileURLWithPath: "/bin/zsh")
        sh.arguments = ["-lc", "command -v claude"]
        let out = Pipe()
        sh.standardOutput = out
        sh.standardError = Pipe()
        guard (try? sh.run()) != nil else { return nil }
        let path = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        sh.waitUntilExit()
        return path.hasPrefix("/") ? path : nil
    }

    /// A Terminal script that signs Claude Code in to another account. No /logout: that can cancel the saved login.
    public static func loginScript(claude: String) -> String {
        let q = "'" + claude.replacingOccurrences(of: "'", with: "'\\''") + "'"
        return """
        #!/bin/sh
        clear
        echo "Sign in with the Claude account you want to add. Your current one is already saved in AgentPlay."
        echo
        \(q) auth login --claudeai
        echo
        echo "Done. AgentPlay saves this account by itself. You can close this window."
        """
    }

    // MARK: Keychain and files

    static func runSecurity(_ args: [String], stdin: String? = nil) -> (Data, Int32) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        p.arguments = args
        let out = Pipe(), inp = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        if stdin != nil { p.standardInput = inp }
        guard (try? p.run()) != nil else { return (Data(), -1) }
        if let stdin {
            inp.fileHandleForWriting.write(Data(stdin.utf8))
            try? inp.fileHandleForWriting.close()
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (data, p.terminationStatus)
    }

    /// Claude Code's live login item: the user's own account first (see ClaudeAccount.order).
    static func liveItem(_ env: Env) -> ClaudeAccount.KeychainItem? {
        if env.claudeService != ClaudeAccount.keychainService { return .init(service: env.claudeService, account: env.user, modified: nil) }
        return ClaudeAccount.order(ClaudeAccount.keychainItems(), user: env.user).first { $0.service == env.claudeService }
    }

    public static func liveLogin(_ env: Env = .live) -> (item: ClaudeAccount.KeychainItem, data: Data)? {
        guard let item = liveItem(env) else { return nil }
        let (data, status) = ClaudeAccount.readSecret(item)
        guard status == 0, let data, !data.isEmpty else { return nil }
        return (item, data.trimmingTrailingNewline)
    }

    /// When Claude Code last wrote its login (it rotates tokens): attributes only, no prompt.
    public static func liveLoginModified() -> Date? { liveItem(.live)?.modified }

    static func oauthAccountBlock(home: URL) -> Data? {
        guard let data = try? Data(contentsOf: home.appendingPathComponent(".claude.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let acct = obj["oauthAccount"] else { return nil }
        return try? JSONSerialization.data(withJSONObject: acct, options: [.sortedKeys])
    }

    /// Saves who Claude Code is signed in as now: its login and its account block.
    public static func saveCurrent(_ env: Env = .live, now: Date = Date()) -> Result<Saved, Failure> {
        guard let id = ClaudeAccount.readIdentity(home: env.home), let uuid = id.accountUuid,
              let block = oauthAccountBlock(home: env.home), let live = liveLogin(env) else { return .failure(.notSignedIn) }
        let payload: [String: Any] = ["login": String(decoding: live.data, as: UTF8.self),
                                      "oauthAccount": String(decoding: block, as: UTF8.self)]
        guard let body = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else { return .failure(.notSignedIn) }
        let line = securityLine(service: env.storeService, account: uuid, label: id.email, value: String(decoding: body, as: UTF8.self))
        let (_, status) = runSecurity(["-i"], stdin: line)
        guard status == 0 else { return .failure(.keychain("couldn't save (status \(status))")) }
        return .success(Saved(accountUuid: uuid, email: id.email, organization: id.organization, seatTier: id.seatTier, savedAt: now))
    }

    static func savedPayload(_ uuid: String, _ env: Env) -> (login: Data, oauthAccount: Data)? {
        let (data, status) = runSecurity(["find-generic-password", "-s", env.storeService, "-a", uuid, "-w"])
        guard status == 0, let obj = try? JSONSerialization.jsonObject(with: data.trimmingTrailingNewline) as? [String: String],
              let login = obj["login"], let acct = obj["oauthAccount"] else { return nil }
        return (Data(login.utf8), Data(acct.utf8))
    }

    public static func forget(_ uuid: String, _ env: Env = .live) {
        _ = runSecurity(["delete-generic-password", "-s", env.storeService, "-a", uuid])
    }

    /// Switches Claude Code to a saved account. `liveBusy`: some session is mid-step right now.
    public static func switchTo(_ target: Saved, liveBusy: Bool, env: Env = .live, backups: URL, now: Date = Date())
        -> Result<Saved, Failure> {
        let home = env.home
        guard !liveBusy else { return .failure(.busy) }
        guard let saved = savedPayload(target.accountUuid, env) else { return .failure(.notSaved) }
        guard isUsable(saved.login, now: now) else { return .failure(.expired) }
        // 1. The outgoing account's newest login first, so its rotated token isn't lost.
        let outgoing = ClaudeAccount.readIdentity(home: home)?.accountUuid
        if outgoing != nil, outgoing != target.accountUuid, case .failure(let f) = saveCurrent(env, now: now) { return .failure(f) }
        // 2. The new ~/.claude.json, computed before anything is written.
        let claudeJSONURL = home.appendingPathComponent(".claude.json")
        guard let current = try? Data(contentsOf: claudeJSONURL),
              let newJSON = withAccount(current, oauthAccount: saved.oauthAccount) else { return .failure(.claudeJSON) }
        // 3. The login, with this Mac's MCP logins kept.
        let live = liveLogin(env)
        guard let login = merged(target: saved.login, live: live?.data) else { return .failure(.notSaved) }
        let account = live?.item.account ?? env.user
        let (_, status) = runSecurity(["-i"], stdin: securityLine(service: env.claudeService, account: account,
                                                                 label: nil, value: String(decoding: login, as: UTF8.self)))
        guard status == 0 else { return .failure(.keychain("couldn't write Claude Code's login (status \(status))")) }
        // 4. ~/.claude.json, with a copy of the old one kept.
        try? FileManager.default.createDirectory(at: backups, withIntermediateDirectories: true)
        try? current.write(to: backups.appendingPathComponent("claude.json.\(Int(now.timeIntervalSince1970))"))
        guard (try? newJSON.write(to: claudeJSONURL, options: .atomic)) != nil else { return .failure(.claudeJSON) }
        var t = target
        t.savedAt = now
        return .success(t)
    }
}

extension Data {
    var trimmingTrailingNewline: Data {
        var d = self
        while let last = d.last, last == 0x0A || last == 0x0D { d.removeLast() }
        return d
    }
}
