import Foundation
import Security

/// Opt-in: Claude's 5-hour and weekly usage from the user's Claude account, the way CodexBar and the other
/// usage menus read it. The statusline bridge only works for terminal Claude Code; editor agent panels (Zed's
/// claude-acp and other Agent SDK hosts) never report plan usage locally, so this is the only source for them.
///
/// It reads the login Claude Code saved in the Keychain ("Claude Code-credentials") through `/usr/bin/security`,
/// so macOS asks once and "Always Allow" sticks to Apple's tool rather than to each build of this app, and sends
/// one GET to `api.anthropic.com/api/oauth/usage`. It never refreshes the login (that would rotate Claude
/// Code's own token); an expired one is reported so the user can open Claude Code. Nothing is stored but the
/// numbers it returns.
public enum ClaudeAccount {
    public static let keychainService = "Claude Code-credentials"
    public static let usageURL = URL(string: "https://api.anthropic.com/api/oauth/usage")!
    public static let betaHeader = "oauth-2025-04-20"
    public static let usagePage = "https://claude.ai/settings/usage"

    public struct Credentials: Equatable, Sendable {
        public var accessToken: String
        public var expiresAt: Date?
        public var plan: String?
    }

    public enum Failure: Error, Equatable, Sendable {
        case noLogin, keychainDenied, expired, unauthorized, rateLimited(Date?), network(String), badResponse(Int)

        /// What the menu says, and what to do about it.
        public var message: String {
            switch self {
            case .noLogin: "No Claude login found on this Mac. Sign in to Claude Code first."
            case .keychainDenied: "macOS didn't allow reading Claude Code's login. Turn it on again and choose Allow."
            case .expired, .unauthorized: "Claude's saved login has expired. Use Claude Code once and it renews."
            case .rateLimited: "Anthropic asked to slow down; trying again shortly."
            case .network(let m): "Couldn't reach Anthropic: \(m)"
            case .badResponse(let code): "Anthropic answered with an error (HTTP \(code))."
            }
        }
    }

    /// Who Claude Code is signed in as, from `oauthAccount` in `~/.claude.json`. Local, no network.
    public struct Identity: Equatable, Sendable {
        public var accountUuid: String?
        public var email: String?
        public var organization: String?
        public var seatTier: String?

        /// Shown with sample data, so a sample never carries the real login.
        public static let sample = Identity(accountUuid: nil, email: "you@example.com", organization: "Example Org", seatTier: nil)

        /// "you@example.com · Example Org"
        public var line: String? {
            let parts = [email, organization].compactMap { $0 }.filter { !$0.isEmpty }
            return parts.isEmpty ? nil : parts.joined(separator: " · ")
        }
    }

    public static func parseIdentity(_ data: Data) -> Identity? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let o = obj["oauthAccount"] as? [String: Any] else { return nil }
        let id = Identity(accountUuid: o["accountUuid"] as? String, email: o["emailAddress"] as? String,
                          organization: o["organizationName"] as? String, seatTier: o["seatTier"] as? String)
        return id.email == nil && id.accountUuid == nil ? nil : id
    }

    public static func readIdentity(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> Identity? {
        (try? Data(contentsOf: home.appendingPathComponent(".claude.json"))).flatMap(parseIdentity)
    }

    /// `{"claudeAiOauth": {"accessToken", "expiresAt" (ms), "subscriptionType"}}`, Claude Code's own format.
    public static func parseCredentials(_ data: Data) -> Credentials? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let o = obj["claudeAiOauth"] as? [String: Any],
              let token = o["accessToken"] as? String, !token.isEmpty else { return nil }
        let exp = (o["expiresAt"] as? Double).map { Date(timeIntervalSince1970: $0 / 1000) }
        return Credentials(accessToken: token, expiresAt: exp, plan: o["subscriptionType"] as? String)
    }

    public struct KeychainItem: Equatable, Sendable {
        public var service: String
        public var account: String
        public var modified: Date?
        public init(service: String, account: String, modified: Date?) {
            self.service = service; self.account = account; self.modified = modified
        }
    }

    /// Claude Code's login items, best first. There can be several: a stale one under "unknown" next to the
    /// live one under the user's name, and `Claude Code-credentials-<hash>` for other config folders.
    /// The user's own account first, then the most recently written.
    public static func order(_ items: [KeychainItem], user: String) -> [KeychainItem] {
        items.filter { $0.service == keychainService || $0.service.hasPrefix(keychainService + "-") }
            .sorted { a, b in
                if (a.account == user) != (b.account == user) { return a.account == user }
                return (a.modified ?? .distantPast) > (b.modified ?? .distantPast)
            }
    }

    /// Attributes only (service, account, date): macOS doesn't ask for these.
    static func keychainItems() -> [KeychainItem] {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecMatchLimit as String: kSecMatchLimitAll,
                                kSecReturnAttributes as String: true]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let rows = out as? [[String: Any]] else { return [] }
        return rows.compactMap { r in
            guard let s = r[kSecAttrService as String] as? String, s.hasPrefix(keychainService) else { return nil }
            return KeychainItem(service: s, account: r[kSecAttrAccount as String] as? String ?? "",
                                modified: r[kSecAttrModificationDate as String] as? Date)
        }
    }

    /// The secret through `/usr/bin/security`, so "Always Allow" sticks to Apple's tool. Exit 128 is Deny.
    static func readSecret(_ item: KeychainItem) -> (Data?, Int32) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        p.arguments = ["find-generic-password", "-s", item.service, "-a", item.account, "-w"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        guard (try? p.run()) != nil else { return (nil, -1) }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (data, p.terminationStatus)
    }

    /// The first usable login: a current one if any, else the newest expired one (to say it expired).
    public static func readCredentials(now: Date = Date()) -> Result<Credentials, Failure> {
        var expired: Credentials?
        var denied = false
        for item in order(keychainItems(), user: NSUserName()) {
            let (data, status) = readSecret(item)
            if status == 128 { denied = true; break }
            guard status == 0, let data, let c = parseCredentials(data) else { continue }
            if let exp = c.expiresAt, exp <= now { expired = expired ?? c; continue }
            return .success(c)
        }
        let file = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/.credentials.json")
        if let data = try? Data(contentsOf: file), let c = parseCredentials(data) { return .success(c) }
        if denied { return .failure(.keychainDenied) }
        if let expired { return .success(expired) }  // fetch() reports it as expired
        return .failure(.noLogin)
    }

    public static func request(token: String, claudeVersion: String?) -> URLRequest {
        var r = URLRequest(url: usageURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        r.httpMethod = "GET"
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        r.setValue(betaHeader, forHTTPHeaderField: "anthropic-beta")
        r.setValue("claude-cli/\(claudeVersion ?? "2.1.0") (external, agentplay-companion)", forHTTPHeaderField: "User-Agent")
        return r
    }

    /// `five_hour` and `seven_day`, each `{"utilization": percent, "resets_at": ISO 8601}`, as limit rows.
    public static func parseUsage(_ data: Data, plan: String?, now: Date) -> [ProviderLimitWindow] {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [] }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        func date(_ v: Any?) -> Date? {
            guard let s = v as? String else { return nil }
            return iso.date(from: s) ?? plain.date(from: s)
        }
        var out: [ProviderLimitWindow] = []
        for (key, name, minutes) in [("five_hour", "five_hour", 300), ("seven_day", "weekly", 10080)] {
            guard let w = obj[key] as? [String: Any], let used = (w["utilization"] as? NSNumber)?.doubleValue else { continue }
            let point = DataPoint<LimitValue>(
                value: LimitValue(usedPercent: used, leftPercent: max(0, 100 - used)),
                kind: .providerReported, source: .officialProviderAPI, observedAt: now, window: name == "five_hour" ? .fiveHour : .weekly,
                confidence: .high,
                explanation: "From your Claude account (api.anthropic.com), the same numbers as claude.ai's usage page.",
                freshness: .fresh)
            out.append(ProviderLimitWindow(provider: "claude-code", window: name, windowMinutes: minutes, plan: plan,
                                           limitId: key, resetsAt: date(w["resets_at"]), usageUrl: usagePage, used: point))
        }
        return out
    }

    /// Credentials, then one request. `claudeVersion` only shapes the User-Agent.
    public static func fetch(claudeVersion: String?, session: URLSession = .shared, now: Date = Date()) async
        -> Result<[ProviderLimitWindow], Failure> {
        let creds: Credentials
        switch readCredentials() {
        case .failure(let f): return .failure(f)
        case .success(let c): creds = c
        }
        if let exp = creds.expiresAt, exp <= now { return .failure(.expired) }
        do {
            let (data, resp) = try await session.data(for: request(token: creds.accessToken, claudeVersion: claudeVersion))
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            switch code {
            case 200:
                let rows = parseUsage(data, plan: creds.plan, now: now)
                return rows.isEmpty ? .failure(.badResponse(code)) : .success(rows)
            case 401, 403: return .failure(.unauthorized)
            case 429:
                let after = ((resp as? HTTPURLResponse)?.value(forHTTPHeaderField: "Retry-After")).flatMap(Double.init)
                return .failure(.rateLimited(after.map { now.addingTimeInterval($0) }))
            default: return .failure(.badResponse(code))
            }
        } catch {
            return .failure(.network(error.localizedDescription))
        }
    }

    /// Account rows replace whatever else claimed Claude's windows (the unavailable row, a refusal); other
    /// providers are untouched.
    public static func merge(_ limits: [ProviderLimitWindow], account: [ProviderLimitWindow]) -> [ProviderLimitWindow] {
        guard !account.isEmpty else { return limits }
        return account + limits.filter { $0.provider != "claude-code" }
    }
}
