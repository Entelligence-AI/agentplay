import Foundation
import Testing
@testable import AgentPlayCore

@Suite struct ForecastTests {
    let cal: Calendar = { var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: "UTC")!; return c }()
    let f: DateFormatter = { let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.timeZone = TimeZone(identifier: "UTC"); f.dateFormat = "yyyy-MM-dd"; return f }()

    /// 28 days, weekdays costing `weekday`, weekends `weekend`, ending on `today`.
    func days(_ today: Date, weekday: Double, weekend: Double) -> [CostDay] {
        (0..<28).reversed().map { i in
            let d = cal.date(byAdding: .day, value: -i, to: today)!
            let w = cal.component(.weekday, from: d)
            return CostDay(date: f.string(from: d), costUsd: (w == 1 || w == 7) ? weekend : weekday)
        }
    }

    @Test func runsOutEarlyWhenWeekdaysAreHeavy() throws {
        // Monday noon, week reset Thursday noon: three days left, weekdays cost 10, weekends 0.
        let now = f.date(from: "2026-10-26")!.addingTimeInterval(12 * 3600)
        let reset = now.addingTimeInterval(3 * 86400)
        let fc = try #require(Forecast.of(used: 80, windowMinutes: 7 * 1440, resetsAt: reset,
                                          days: days(cal.startOfDay(for: now), weekday: 10, weekend: 0), now: now, calendar: cal))
        let out = try #require(fc.runsOutAt)
        #expect(out < reset && fc.daysEarly > 0.5)
        #expect(abs(fc.perDay - 20.0 / 3) < 0.01)
        #expect(fc.sentence(reset: reset, now: now).hasPrefix("At your usual pace, out "))
    }

    @Test func onTrackWhenWhatIsLeftIsTheWeekend() throws {
        // Friday noon, reset Monday noon: the rest is a weekend that usually costs nothing.
        let now = f.date(from: "2026-10-23")!.addingTimeInterval(12 * 3600)
        let reset = now.addingTimeInterval(3 * 86400)
        let fc = try #require(Forecast.of(used: 70, windowMinutes: 7 * 1440, resetsAt: reset,
                                          days: days(cal.startOfDay(for: now), weekday: 10, weekend: 0), now: now, calendar: cal))
        #expect(fc.runsOutAt == nil)
        #expect((fc.leftAtReset ?? 0) > 0)
        #expect(fc.sentence(reset: reset, now: now).hasPrefix("On track"))
    }

    @Test func saysNothingWithoutHistory() {
        let now = Date()
        #expect(Forecast.of(used: 50, windowMinutes: 7 * 1440, resetsAt: now.addingTimeInterval(86400), days: [], now: now) == nil)
        #expect(Forecast.of(used: 50, windowMinutes: 300, resetsAt: now.addingTimeInterval(3600), days: [], now: now) == nil)
    }
}
