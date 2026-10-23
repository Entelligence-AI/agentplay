import Foundation

// The Swift side of `agentplay.companion/v1` (schema/agentplay-companion-v1.schema.json).
// Decoding is tolerant: an unknown enum value from a newer core degrades to the least
// authoritative reading (unavailable, low confidence), never to a confident one.

public let supportedSnapshotSchema = "agentplay.companion/v1"
public let supportedSnapshotVersion = 1

protocol TolerantEnum: RawRepresentable, Codable where RawValue == String {
    static var fallback: Self { get }
}

extension TolerantEnum {
    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Self(rawValue: raw) ?? Self.fallback
    }
}

public enum DataKind: String, TolerantEnum, Sendable {
    case recorded, derived, estimated
    case providerReported = "provider_reported"
    case unavailable
    static var fallback: DataKind { .unavailable }

    public var label: String {
        switch self {
        case .recorded: "Recorded"
        case .derived: "Derived"
        case .estimated: "Estimated"
        case .providerReported: "Reported by provider"
        case .unavailable: "Unavailable"
        }
    }
}

public enum DataSource: String, TolerantEnum, Sendable {
    case agentplayMap = "agentplay_map"
    case localIndex = "local_index"
    case approvedProviderCLI = "approved_provider_cli"
    case officialProviderAPI = "official_provider_api"
    case none
    static var fallback: DataSource { .none }

    public var label: String {
        switch self {
        case .agentplayMap: "AgentPlay map"
        case .localIndex: "Local index"
        case .approvedProviderCLI: "Provider CLI"
        case .officialProviderAPI: "Provider API"
        case .none: "No source"
        }
    }
}

public enum DataWindow: String, TolerantEnum, Sendable {
    case context
    case fiveHour = "five_hour"
    case weekly, session, project, none
    static var fallback: DataWindow { .none }
}

public enum Confidence: String, TolerantEnum, Sendable {
    case high, medium, low
    static var fallback: Confidence { .low }
}

public enum Freshness: String, TolerantEnum, Sendable {
    case fresh, stale, unknown
    static var fallback: Freshness { .unknown }
}

/// One number or state, with where it came from and how far to trust it.
public struct DataPoint<T: Codable & Sendable & Equatable>: Codable, Sendable, Equatable {
    public var value: T?
    public var kind: DataKind
    public var source: DataSource
    public var observedAt: Date?
    public var window: DataWindow
    public var confidence: Confidence
    public var explanation: String
    public var freshness: Freshness

    public init(value: T?, kind: DataKind, source: DataSource, observedAt: Date? = nil, window: DataWindow = .none,
                confidence: Confidence = .high, explanation: String, freshness: Freshness = .unknown) {
        self.value = kind == .unavailable ? nil : value
        self.kind = value == nil ? .unavailable : kind
        self.source = self.kind == .unavailable ? .none : source
        self.observedAt = observedAt
        self.window = window
        self.confidence = self.kind == .unavailable ? .low : confidence
        self.explanation = explanation
        self.freshness = freshness
    }

    enum CodingKeys: String, CodingKey { case value, kind, source, observedAt, window, confidence, explanation, freshness }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let kind = try c.decode(DataKind.self, forKey: .kind)
        // A value we can't read is unavailable, not a default.
        let value = kind == .unavailable ? nil : (try? c.decodeIfPresent(T.self, forKey: .value)) ?? nil
        self.init(
            value: value,
            kind: kind,
            source: try c.decode(DataSource.self, forKey: .source),
            observedAt: try c.decodeIfPresent(Date.self, forKey: .observedAt),
            window: try c.decodeIfPresent(DataWindow.self, forKey: .window) ?? .none,
            confidence: try c.decodeIfPresent(Confidence.self, forKey: .confidence) ?? .low,
            explanation: try c.decodeIfPresent(String.self, forKey: .explanation) ?? "",
            freshness: try c.decodeIfPresent(Freshness.self, forKey: .freshness) ?? .unknown
        )
    }

    public var isAvailable: Bool { kind != .unavailable && value != nil }

    /// Stale if the producer said so, or if it is older than `maxAge`.
    public func isStale(now: Date, maxAge: TimeInterval) -> Bool {
        if freshness == .stale { return true }
        guard let observedAt else { return false }
        return now.timeIntervalSince(observedAt) > maxAge
    }
}

public struct ContextValue: Codable, Sendable, Equatable {
    public var tokens: Int
    public var limit: Int?
    public var percent: Double?

    public init(tokens: Int, limit: Int?, percent: Double?) {
        self.tokens = tokens
        self.limit = limit
        // No percentage without a denominator, whatever the producer sent.
        self.percent = limit == nil ? nil : percent
    }

    enum CodingKeys: String, CodingKey { case tokens, limit, percent }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(tokens: try c.decode(Int.self, forKey: .tokens), limit: try c.decodeIfPresent(Int.self, forKey: .limit),
                  percent: try c.decodeIfPresent(Double.self, forKey: .percent))
    }
}

public struct LimitValue: Codable, Sendable, Equatable {
    public var usedPercent: Double
    public var leftPercent: Double
}

public enum ActivityState: String, TolerantEnum, Sendable {
    case active, recent, idle
    static var fallback: ActivityState { .idle }
}

public struct SessionEvent: Codable, Sendable, Equatable, Identifiable {
    public var seq: Int
    public var at: Date?
    public var kind: String
    public var cat: String?
    public var agent: String
    public var label: String
    public var target: String?
    public var err: Bool
    public var pending: Bool
    public var errClass: String?
    public var retryOf: Int?
    public var attempt: Int?
    public var id: Int { seq }

    public init(seq: Int, at: Date?, kind: String, cat: String? = nil, agent: String = "main", label: String,
                target: String? = nil, err: Bool = false, pending: Bool = false, errClass: String? = nil,
                retryOf: Int? = nil, attempt: Int? = nil) {
        self.seq = seq; self.at = at; self.kind = kind; self.cat = cat; self.agent = agent; self.label = label
        self.target = target; self.err = err; self.pending = pending; self.errClass = errClass
        self.retryOf = retryOf; self.attempt = attempt
    }

    /// The ordinal step this event is, for the micro timeline: read → tool → error → retry → edit.
    public var step: String {
        if pending { return "running" }
        if err { return "error" }
        if retryOf != nil { return "retry" }
        switch kind {
        case "read", "search": return "read"
        case "edit": return "edit"
        case "exec", "verify": return "tool"
        case "mcp": return "mcp"
        case "subagent": return "subagent"
        default: return kind
        }
    }
}

public struct PendingTool: Codable, Sendable, Equatable {
    public var event: SessionEvent
    public var elapsed: DataPoint<Int>
}

public struct RunCounts: Codable, Sendable, Equatable {
    public var events: Int?
    public var calls: Int?
    public var errors: Int?
    public var retries: Int?
    public var compactions: Int?
}

public struct RunActivity: Codable, Sendable, Equatable, Identifiable {
    public var sessionId: String
    public var agentTool: String?
    public var agentLabel: String
    public var repo: String?
    public var repoCount: Int?
    public var startedAt: Date?
    public var lastEventAt: Date?
    public var lastActivityAt: Date?
    /// Seconds the transcript changed after its map was built: events in that gap aren't here yet.
    public var mapBehindSeconds: Int?
    public var activity: DataPoint<ActivityState>
    public var outcome: String?
    public var models: [String]?
    public var pathMode: String?
    public var counts: RunCounts
    public var categories: [String: Int]
    public var lastEvent: SessionEvent?
    public var lastError: SessionEvent?
    public var recentEvents: [SessionEvent]
    public var pendingTool: PendingTool?
    public var context: DataPoint<ContextValue>
    public var imported: Bool?
    public var id: String { sessionId }

    public var state: ActivityState { activity.value ?? .idle }
    /// The newest wall-clock time anything is known to have happened in this run.
    public var latestAt: Date? { [lastEventAt, lastActivityAt].compactMap { $0 }.max() }
}

public enum AlertRuleKind: String, TolerantEnum, Sendable, CaseIterable {
    case repeatedFailure = "repeated_failure"
    case unresolvedEnd = "unresolved_end"
    case unknown
    static var fallback: AlertRuleKind { .unknown }
}

public struct AlertCandidate: Codable, Sendable, Equatable, Identifiable {
    public var rule: AlertRuleKind
    public var signature: String
    public var patternKey: String
    public var sessionId: String
    public var agentTool: String?
    public var command: String?
    public var errClass: String?
    public var agent: String?
    public var count: Int?
    public var firstSeq: Int?
    public var firstAt: Date?
    public var triggerSeq: Int?
    public var triggerAt: Date?
    public var resolvedSeq: Int?
    public var evidence: DataPoint<AnyScalar>
    public var id: String { signature }
    public var isResolved: Bool { resolvedSeq != nil }
}

/// A JSON scalar (the evidence value of an alert can be a count or an outcome name).
public enum AnyScalar: Codable, Sendable, Equatable {
    case int(Int), double(Double), string(String), bool(Bool)

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let v = try? c.decode(Int.self) { self = .int(v) }
        else if let v = try? c.decode(Double.self) { self = .double(v) }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else { self = .string(try c.decode(String.self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .int(let v): try c.encode(v)
        case .double(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        }
    }
}

public struct ProviderLimitWindow: Codable, Sendable, Equatable, Identifiable {
    public var provider: String
    public var window: String
    public var windowMinutes: Int?
    public var plan: String?
    public var limitId: String?
    public var resetsAt: Date?
    public var usageUrl: String?
    public var used: DataPoint<LimitValue>
    public var id: String { "\(provider)|\(window)" }

    public var windowLabel: String {
        switch window {
        case "five_hour": return "5-hour"
        case "weekly": return "Weekly"
        case "none": return "Plan"
        default: return windowMinutes.map { "\($0 / 60)-hour" } ?? window
        }
    }

    public var providerLabel: String { AgentNames.label(provider) }
}

public struct LearnEvidence: Codable, Sendable, Equatable, Identifiable {
    public var sessionId: String
    public var agentTool: String?
    public var seq: Int
    public var at: Date?
    public var id: String { "\(sessionId)#\(seq)" }
}

public struct LearnCard: Codable, Sendable, Equatable {
    public var signature: String
    public var patternKey: String
    public var kind: String
    public var command: String?
    public var title: String
    public var denominator: String
    public var detail: String
    public var failures: DataPoint<Int>
    public var runs: Int
    public var sessions: Int
    public var evidence: [LearnEvidence]
}

public enum SourceStatus: String, TolerantEnum, Sendable {
    case found
    case notFound = "not_found"
    case error
    static var fallback: SourceStatus { .error }
}

public struct SourceCapability: Codable, Sendable, Equatable, Identifiable {
    public var agent: String
    public var label: String
    public var status: SourceStatus
    public var paths: [String]
    public var sessionCount: Int
    public var latestAt: String?
    public var remedy: String?
    public var id: String { agent }
}

public struct StoreInfo: Codable, Sendable, Equatable {
    public var available: Bool
    public var sessionCount: Int
    public var indexError: String?
}

public struct UnreadableSession: Codable, Sendable, Equatable {
    public var sessionId: String
    public var agentTool: String?
    public var message: String
}

public struct Snapshot: Codable, Sendable, Equatable {
    public var schema: String
    public var schemaVersion: Int
    public var agentplayVersion: String
    public var generatedAt: Date
    public var sample: Bool?
    public var store: StoreInfo
    public var sources: [SourceCapability]
    public var runs: [RunActivity]
    public var unreadable: [UnreadableSession]?
    public var alerts: [AlertCandidate]
    public var limits: [ProviderLimitWindow]
    public var learn: LearnCard?

    public var isSample: Bool { sample == true }
    public var foundSources: [SourceCapability] { sources.filter { $0.status == .found } }
    public var activeRuns: [RunActivity] { runs.filter { $0.state == .active } }
}

public enum AgentNames {
    public static func label(_ agent: String?) -> String {
        switch agent {
        case "claude-code": "Claude Code"
        case "codex": "Codex"
        case "cursor": "Cursor"
        case "copilot": "Copilot CLI"
        case "copilot-vscode": "Copilot (VS Code)"
        case "opencode": "OpenCode"
        case "kiro": "Kiro"
        case let a?: a
        case nil: "Unknown agent"
        }
    }
}

// MARK: - Decoding

public enum SnapshotError: Error, Equatable, LocalizedError {
    case malformed(String)
    case incompatibleSchema(String)
    case newerVersion(found: Int, supported: Int)
    case coreTooOld(String)
    case runtimeMissing(String)
    case processFailed(code: Int32, message: String)
    case timedOut

    public var errorDescription: String? {
        switch self {
        case .malformed(let why): "AgentPlay returned data the companion can't read (\(why))."
        case .incompatibleSchema(let s): "AgentPlay speaks \(s), which this companion doesn't understand. Update the companion."
        case .newerVersion(let f, let s): "AgentPlay's data is version \(f); this companion reads up to \(s). Update the companion."
        case .coreTooOld(let v): "This AgentPlay (\(v)) has no companion snapshot. It needs 0.2.4 or later."
        case .runtimeMissing(let p): "The bundled AgentPlay runtime is missing (\(p)). Reinstall the app."
        case .processFailed(let code, let msg): "AgentPlay exited with status \(code). \(msg)"
        case .timedOut: "AgentPlay took too long to answer. It will retry."
        }
    }
}

public enum SnapshotDecoder {
    public static func decoder() -> JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { dec in
            let s = try dec.singleValueContainer().decode(String.self)
            if let date = parseISO(s) { return date }
            throw DecodingError.dataCorrupted(.init(codingPath: dec.codingPath, debugDescription: "bad date \(s)"))
        }
        return d
    }

    public static func parseISO(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    public static func decode(_ data: Data) throws -> Snapshot {
        struct Header: Decodable { var schema: String?; var schemaVersion: Int? }
        guard let header = try? JSONDecoder().decode(Header.self, from: data) else {
            throw SnapshotError.malformed("not a JSON object")
        }
        guard header.schema == supportedSnapshotSchema else {
            throw SnapshotError.incompatibleSchema(header.schema ?? "an unknown format")
        }
        if let v = header.schemaVersion, v > supportedSnapshotVersion {
            // v1 only ever adds fields, so a higher minor would still decode; a bump means
            // the producer says the old reader is wrong.
            throw SnapshotError.newerVersion(found: v, supported: supportedSnapshotVersion)
        }
        do {
            return try decoder().decode(Snapshot.self, from: data)
        } catch let DecodingError.keyNotFound(key, ctx) {
            throw SnapshotError.malformed("missing \(ctx.codingPath.map(\.stringValue).joined(separator: ".")).\(key.stringValue)")
        } catch {
            throw SnapshotError.malformed(String(describing: error).prefix(160).description)
        }
    }

    /// Shift every time in a fixture so it reads as "now" (dev sample mode only).
    public static func rebased(_ snap: Snapshot, to now: Date) -> Snapshot {
        let shift = now.timeIntervalSince(snap.generatedAt)
        func m(_ d: Date?) -> Date? { d.map { $0.addingTimeInterval(shift) } }
        func mp<T>(_ p: DataPoint<T>) -> DataPoint<T> { var p = p; p.observedAt = m(p.observedAt); return p }
        func me(_ e: SessionEvent?) -> SessionEvent? { e.map { var e = $0; e.at = m(e.at); return e } }
        var s = snap
        s.generatedAt = now
        s.runs = s.runs.map { r in
            var r = r
            r.startedAt = m(r.startedAt); r.lastEventAt = m(r.lastEventAt); r.lastActivityAt = m(r.lastActivityAt)
            r.activity = mp(r.activity); r.context = mp(r.context)
            r.lastEvent = me(r.lastEvent); r.lastError = me(r.lastError)
            r.recentEvents = r.recentEvents.compactMap { me($0) }
            if var p = r.pendingTool { p.event = me(p.event)!; p.elapsed = mp(p.elapsed); r.pendingTool = p }
            return r
        }
        s.alerts = s.alerts.map { a in
            var a = a; a.firstAt = m(a.firstAt); a.triggerAt = m(a.triggerAt); a.evidence = mp(a.evidence); return a
        }
        s.limits = s.limits.map { l in var l = l; l.resetsAt = m(l.resetsAt); l.used = mp(l.used); return l }
        if var l = s.learn {
            l.failures = mp(l.failures)
            l.evidence = l.evidence.map { e in var e = e; e.at = m(e.at); return e }
            s.learn = l
        }
        return s
    }
}
