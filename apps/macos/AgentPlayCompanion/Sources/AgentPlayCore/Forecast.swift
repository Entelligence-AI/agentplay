import Foundation

/// When a weekly plan window runs out at your usual pace, learnt from your own spend per weekday over the last
/// four weeks, and what a day can take for it to last. Straight-line pace (`Pace`) assumes every hour of the week
/// is like the hours so far; people code less on weekends, so this weighs each remaining day by what that
/// weekday usually costs.
public struct Forecast: Equatable, Sendable {
    /// When usage reaches 100% at your usual pace, if that comes before the reset.
    public var runsOutAt: Date?
    /// Percent left when the window resets, when it doesn't run out.
    public var leftAtReset: Double?
    /// Percent a day that lasts until the reset.
    public var perDay: Double
    /// Days early it runs out (0 when it doesn't).
    public var daysEarly: Double

    /// `used` 0...100 of a window that ends at `resetsAt` and is `windowMinutes` long. `days`: one entry per local
    /// day, oldest first, ending today. Nil when there isn't enough history to say anything honest.
    public static func of(used: Double, windowMinutes: Int?, resetsAt: Date?, days: [CostDay], now: Date,
                          calendar: Calendar = .current) -> Forecast? {
        guard let minutes = windowMinutes, minutes >= 24 * 60, let reset = resetsAt, reset > now, used > 0 else { return nil }
        let parser = DateFormatter()
        parser.locale = Locale(identifier: "en_US_POSIX")
        parser.timeZone = calendar.timeZone
        parser.dateFormat = "yyyy-MM-dd"
        let parsed = days.compactMap { d -> (Date, Double)? in
            guard let day = parser.date(from: d.date) else { return nil }
            return (calendar.startOfDay(for: day), d.costUsd)
        }
        let today = calendar.startOfDay(for: now)
        let past = parsed.filter { $0.0 < today }
        guard past.count >= 14, past.contains(where: { $0.1 > 0 }) else { return nil }
        // Usual spend per weekday.
        var sums = [Int: Double](), counts = [Int: Int]()
        for (day, cost) in past {
            let w = calendar.component(.weekday, from: day)
            sums[w, default: 0] += cost
            counts[w, default: 0] += 1
        }
        func usual(_ day: Date) -> Double {
            let w = calendar.component(.weekday, from: day)
            return (sums[w] ?? 0) / Double(max(1, counts[w] ?? 0))
        }
        // Spend so far in this window, the first day pro-rated from the window's start.
        let start = reset.addingTimeInterval(-TimeInterval(minutes * 60))
        var spent = 0.0
        for (day, cost) in parsed where day.addingTimeInterval(86400) > start && day <= now {
            let from = max(day, start), to = min(day.addingTimeInterval(86400), now)
            let share = day == today ? 1 : max(0, to.timeIntervalSince(from)) / 86400
            spent += cost * share
        }
        guard spent > 0.01 else { return nil }
        let percentPerDollar = used / spent
        // Walk forward a day at a time to the reset.
        var level = used
        var cursor = now
        var runsOut: Date?
        let todaySpent = parsed.first { $0.0 == today }?.1 ?? 0
        while cursor < reset {
            let day = calendar.startOfDay(for: cursor)
            let end = min(day.addingTimeInterval(86400), reset)
            let span = end.timeIntervalSince(cursor)
            var expected = usual(day) * span / 86400
            if day == today { expected = max(0, usual(day) - todaySpent) * min(1, span / max(1, end.timeIntervalSince(day))) }
            let rise = expected * percentPerDollar
            if level + rise >= 100, rise > 0 {
                runsOut = cursor.addingTimeInterval(span * (100 - level) / rise)
                level = 100
                break
            }
            level += rise
            cursor = end
        }
        let daysLeft = max(1.0 / 24, reset.timeIntervalSince(now) / 86400)
        return Forecast(runsOutAt: runsOut, leftAtReset: runsOut == nil ? max(0, 100 - level) : nil,
                        perDay: max(0, 100 - used) / daysLeft,
                        daysEarly: runsOut.map { reset.timeIntervalSince($0) / 86400 } ?? 0)
    }

    /// A few words under the week's bar: when it runs out at your pace, or that it lasts.
    public func short(reset: Date, now: Date) -> String {
        if let out = runsOutAt {
            let budget = perDay < 1 ? String(format: "%.1f%%", perDay) : Format.percent(perDay)
            return "At your pace: out \(Format.weekdayTime(out)). Lasts at \(budget) a day."
        }
        return "At your pace: lasts, about \(Format.percent(leftAtReset ?? 0)) spare."
    }

    /// One line for the plan card.
    public func sentence(reset: Date, now: Date) -> String {
        if let out = runsOutAt {
            let when = Format.weekdayTime(out)
            let early = daysEarly >= 1 ? "\(Int(daysEarly.rounded())) day\(Int(daysEarly.rounded()) == 1 ? "" : "s") early"
                : "\(Format.duration(reset.timeIntervalSince(out))) early"
            let budget = perDay < 1 ? String(format: "%.1f%%", perDay) : Format.percent(perDay)
            return "At your usual pace, out \(when), \(early). To last: about \(budget) a day."
        }
        return "On track: about \(Format.percent(leftAtReset ?? 0)) left when it resets."
    }

}
