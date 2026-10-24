import Foundation

/// How a plan window is being spent: tinted by pace, not level (a 70% week that's on pace is fine).
public enum PaceLevel: Equatable, Sendable { case normal, ahead, over }

public struct Pace: Equatable, Sendable {
    public var level: PaceLevel
    /// Fraction of the window elapsed, 0...1.
    public var elapsed: Double
    /// When usage would reach 100% at the current rate, if that comes before the reset.
    public var runsOutAt: Date?

    /// `used` 0...100, `windowMinutes` length, `resetsAt` end of the window.
    public static func of(used: Double, windowMinutes: Int?, resetsAt: Date?, now: Date) -> Pace? {
        guard let minutes = windowMinutes, minutes > 0, let reset = resetsAt, reset > now else { return nil }
        let length = TimeInterval(minutes * 60)
        let start = reset.addingTimeInterval(-length)
        let elapsed = min(1, max(0.0001, now.timeIntervalSince(start) / length))
        let fraction = used / 100
        let ratio = fraction / elapsed
        let level: PaceLevel = (fraction >= 0.9 || ratio > 1.5) ? .over : ratio > 1.25 ? .ahead : .normal
        var runsOut: Date?
        if fraction > 0, fraction < 1 {
            let rate = fraction / now.timeIntervalSince(start)  // per second
            let at = now.addingTimeInterval((1 - fraction) / rate)
            if at < reset { runsOut = at }
        } else if fraction >= 1 {
            runsOut = now
        }
        return Pace(level: level, elapsed: elapsed, runsOutAt: runsOut)
    }
}

public struct ProviderSummary: Identifiable, Equatable, Sendable {
    public var provider: String
    public var plan: String?
    /// Windows with real numbers, nearest reset first.
    public var windows: [ProviderLimitWindow]
    /// Why there are no numbers, when there are none.
    public var unavailable: ProviderLimitWindow?
    public var conflictingPlans: [String]? = nil
    public var id: String { provider }
    public var label: String { AgentNames.label(provider) }

    public var tightest: ProviderLimitWindow? {
        windows.filter { $0.used.freshness != .stale }.max { ($0.used.value?.usedPercent ?? 0) < ($1.used.value?.usedPercent ?? 0) }
    }

    public func window(_ name: String) -> ProviderLimitWindow? { windows.first { $0.window == name } }

    public static func build(_ limits: [ProviderLimitWindow]) -> [ProviderSummary] {
        var order: [String] = []
        var groups: [String: [ProviderLimitWindow]] = [:]
        for l in limits {
            if groups[l.provider] == nil { order.append(l.provider) }
            groups[l.provider, default: []].append(l)
        }
        let out = order.map { p -> ProviderSummary in
            let all = groups[p] ?? []
            let real = all.filter { $0.used.isAvailable }
                .sorted { ($0.resetsAt ?? .distantFuture) < ($1.resetsAt ?? .distantFuture) }
            return ProviderSummary(provider: p, plan: all.compactMap(\.plan).first, windows: real,
                                   unavailable: real.isEmpty ? all.first : nil,
                                   conflictingPlans: all.compactMap(\.conflictingPlans).first)
        }
        // Most constrained first; providers without numbers last.
        return out.sorted { a, b in
            let ua = a.tightest?.used.value?.usedPercent ?? -1, ub = b.tightest?.used.value?.usedPercent ?? -1
            return ua > ub
        }
    }
}
