import Foundation

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

    /// `{"claudeAiOauth": {"accessToken", "expiresAt" (ms), "subscriptionType"}}`, Claude Code's own format.
    public static func parseCredentials(_ data: Data) -> Credentials? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let o = obj["claudeAiOauth"] as? [String: Any],
              let token = o["accessToken"] as? String, !token.isEmpty else { return nil }
        let exp = (o["expiresAt"] as? Double).map { Date(timeIntervalSince1970: $0 / 1000) }
        return Credentials(accessToken: token, expiresAt: exp, plan: o["subscriptionType"] as? String)
    }

    /// The Keychain item through `/usr/bin/security`, else `~/.claude/.credentials.json`.
    public static func readCredentials() -> Result<Credentials, Failure> {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        p.arguments = ["find-generic-password", "-s", keychainService, "-w"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        if (try? p.run()) != nil {
            let data = out.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            if p.terminationStatus == 0, let c = parseCredentials(data) { return .success(c) }
            // 128: the user clicked Deny; 44: no such item.
            if p.terminationStatus == 128 { return .failure(.keychainDenied) }
        }
        let file = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/.credentials.json")
        if let data = try? Data(contentsOf: file), let c = parseCredentials(data) { return .success(c) }
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
