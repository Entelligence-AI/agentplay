import AgentPlayCore
import AppKit
import SwiftUI

/// The parts of the Insights window, in sidebar order.
enum InsightsSection: String, CaseIterable, Identifiable {
    case overview, fixes, applied, autotune, steering, usage
    var id: String { rawValue }
    var title: String {
        switch self {
        case .overview: "Overview"
        case .fixes: "Fixes"
        case .applied: "Applied"
        case .autotune: "Autotune"
        case .steering: "How You Steer"
        case .usage: "Usage"
        }
    }
    var symbol: String {
        switch self {
        case .overview: "heart.text.square"
        case .fixes: "wrench.and.screwdriver"
        case .applied: "checkmark.seal"
        case .autotune: "wand.and.stars"
        case .steering: "steeringwheel"
        case .usage: "chart.bar.xaxis"
        }
    }
}

/// What the menu can't hold: why the score is what it is, the fixes in plain words with the way to do them, every
/// change AgentPlay made with whether it worked, what Autotune is waiting on, and the longer views.
enum InsightsWindow {
    @MainActor static func make(model: CompanionModel) -> NSWindow {
        let w = NSWindow(contentViewController: NSHostingController(rootView: AnyView(InsightsView().environmentObject(model))))
        w.title = "AgentPlay Insights"
        w.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
        w.titlebarAppearsTransparent = true
        w.setContentSize(NSSize(width: 860, height: 600))
        w.minSize = NSSize(width: 720, height: 480)
        return w
    }
}

struct InsightsView: View {
    @EnvironmentObject var model: CompanionModel

    var insights: Insights? { model.snapshot?.insights }

    var body: some View {
        NavigationSplitView {
            List(InsightsSection.allCases, selection: Binding(get: { model.insightsSection }, set: { model.insightsSection = $0 ?? .overview })) { s in
                Label(s.title, systemImage: s.symbol)
                    .badge(badge(s))
                    .tag(s)
            }
            .navigationSplitViewColumnWidth(min: 180, ideal: 200)
        } detail: {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    switch model.insightsSection {
                    case .overview: OverviewPane(insights: insights)
                    case .fixes: FixesPane(insights: insights)
                    case .applied: AppliedPane(insights: insights)
                    case .autotune: AutotunePane(insights: insights)
                    case .steering: SteeringPane(signals: model.snapshot?.usage?.signals)
                    case .usage: UsagePane(report: model.snapshot?.usage)
                    }
                }
                .padding(28)
                .frame(maxWidth: 640, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    func badge(_ s: InsightsSection) -> Int {
        switch s {
        case .fixes: insights?.fixes.count ?? 0
        case .applied: insights?.applied?.count ?? 0
        case .autotune: insights?.autotune?.pending.count ?? 0
        default: 0
        }
    }
}

// MARK: - Pieces

struct PaneTitle: View {
    var title: String
    var subtitle: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.largeTitle.weight(.semibold))
            if let subtitle { Text(subtitle).font(.title3).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
        }
    }
}

struct Card<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 10) { content }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

struct Labelled: View {
    var label: String
    var text: String
    var tint: Color = .primary
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption.weight(.semibold)).foregroundStyle(.secondary).textCase(.uppercase)
            Text(text).font(.body).foregroundStyle(tint).fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct EmptyNote: View {
    var text: String
    var body: some View {
        Text(text).font(.title3).foregroundStyle(.secondary).padding(.top, 8).fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - Overview

struct OverviewPane: View {
    @EnvironmentObject var model: CompanionModel
    var insights: Insights?

    var body: some View {
        if let ins = insights, let score = ins.score {
            PaneTitle(title: "Run health", subtitle: "How well your agents' sessions went in the last \(ins.days) days, out of 100.")
            Card {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("\(Int(score.rounded()))").font(.system(size: 56, weight: .semibold, design: .rounded))
                    Text("/ 100").font(.title2).foregroundStyle(.secondary)
                    Spacer()
                    if let prev = ins.prevScore {
                        let d = score - prev
                        Text(abs(d) < 0.5 ? "Same as the month before" : "\(d > 0 ? "Up" : "Down") \(Format.points(abs(d))) from \(Int(prev.rounded()))")
                            .font(.title3).foregroundStyle(d <= -3 ? Color.orange : d >= 3 ? Color.green : .secondary)
                    }
                }
                if let gain = ins.fixes.compactMap(\.points).reduce(0, +) as Double?, gain > 0 {
                    Button("\(ins.fixes.count) fix\(ins.fixes.count == 1 ? "" : "es") would add \(Format.points(gain)) points") {
                        model.insightsSection = .fixes
                    }
                    .buttonStyle(.link)
                }
            }
            Text("What it's made of").font(.title2.weight(.semibold)).padding(.top, 6)
            ForEach(ins.components) { c in
                Card {
                    HStack(alignment: .firstTextBaseline) {
                        Text(c.name).font(.headline)
                        Spacer()
                        Text("\(Format.points(c.points ?? 0)) of 20").font(.headline.monospacedDigit())
                        if let p = c.prevPoints, let now = c.points, abs(now - p) >= 0.5 {
                            Text(now > p ? "↑" : "↓").foregroundStyle(now > p ? .green : .orange)
                        }
                    }
                    ProgressView(value: min(20, max(0, c.points ?? 0)), total: 20).tint(((c.points ?? 0) < 10) ? .orange : .accentColor)
                    Text(Self.meaning(c.key)).font(.callout).foregroundStyle(.secondary)
                }
            }
        } else {
            PaneTitle(title: "Run health", subtitle: nil)
            EmptyNote(text: "Run health needs at least \(insights?.minSessions ?? 10) sessions in the last 30 days. It fills in as your agents work.")
        }
    }

    static func meaning(_ key: String) -> String {
        switch key {
        case "shipping": "Sessions that changed code and ended in a pull request."
        case "verification": "Sessions where the agent ran a test or check after its last edit, before saying done."
        case "efficiency": "Few failed commands and retries for the work done; cached context reused."
        case "context": "Sessions that didn't overflow their context and re-read files afterwards."
        default: "Tools, hooks and servers that work, without permission stops or missing commands."
        }
    }
}

// MARK: - Fixes

struct FixesPane: View {
    @EnvironmentObject var model: CompanionModel
    var insights: Insights?

    var body: some View {
        PaneTitle(title: "Fixes", subtitle: "Changes to your agent setup that your own sessions say would help, best first.")
        let fixes = insights?.fixes ?? []
        if fixes.isEmpty {
            EmptyNote(text: "Nothing to fix right now. Pip will tell you when your sessions show a pattern worth changing.")
        }
        ForEach(fixes) { f in
            Card {
                HStack(alignment: .firstTextBaseline) {
                    Text(Plain.text(f.title)).font(.title3.weight(.semibold))
                    Spacer()
                    if let p = f.points, p > 0 { Text("+\(Format.points(p))").font(.title3.weight(.semibold)).foregroundStyle(.green) }
                }
                Labelled(label: "What happens", text: Plain.text(f.pattern))
                if let cause = f.hook?.cause { Labelled(label: "Why", text: cause, tint: .orange) }
                Labelled(label: "After the fix", text: Plain.text(f.effect))
                HStack(spacing: 10) {
                    if f.applicable {
                        Button("Preview and Apply…") { model.applyFix(f) }.buttonStyle(.borderedProminent)
                    }
                    Button("Copy a Prompt for Claude Code") { model.copyText(FixDetail.prompt(f)) }
                    if let first = f.evidence.first {
                        Button("Where It Happened") { model.openViewer(sessionId: first) }.buttonStyle(.link)
                    }
                }
                .padding(.top, 4)
                Text(f.applicable ? "You see the exact lines before anything is written, and can undo it from Applied."
                     : "AgentPlay can't change this one itself; the prompt hands the whole case to Claude Code.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

// MARK: - Applied

struct AppliedPane: View {
    @EnvironmentObject var model: CompanionModel
    var insights: Insights?

    var body: some View {
        PaneTitle(title: "Applied", subtitle: "Every change AgentPlay made to your setup, and whether it worked: the thing the fix was chosen to move, measured over the 30 days before and the sessions since.")
        let applied = insights?.applied ?? []
        if applied.isEmpty {
            EmptyNote(text: "Nothing applied yet. Apply a fix and it shows here, with before and after.")
        }
        ForEach(applied) { e in
            Card {
                HStack(alignment: .firstTextBaseline) {
                    Text(Plain.text(e.title ?? "A change")).font(.title3.weight(.semibold))
                    Spacer()
                    VerdictBadge(verdict: e.verdict)
                }
                Text([e.file, e.repo, Self.when(e.at), e.source == "autotune" ? "by Autotune" : nil].compactMap { $0 }.joined(separator: " · "))
                    .font(.callout).foregroundStyle(.secondary)
                if let m = e.measure, let before = e.before {
                    HStack(alignment: .firstTextBaseline, spacing: 18) {
                        Labelled(label: Self.measureName(m), text: "before \(Self.value(m, before))")
                        Image(systemName: "arrow.right").foregroundStyle(.secondary)
                        Labelled(label: "since, \(e.sessionsSince ?? 0) session\(e.sessionsSince == 1 ? "" : "s")",
                                 text: e.after.map { Self.value(m, $0) } ?? "no sessions yet")
                    }
                }
                Text(Self.explain(e)).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Button("Undo") { model.undoApplied(e) }
            }
        }
    }

    static func when(_ iso: String?) -> String? {
        guard let iso, let d = ISO8601DateFormatter().date(from: iso) else { return nil }
        return "applied " + Format.weekdayTime(d)
    }

    static func measureName(_ m: String) -> String {
        switch m {
        case "hook_fails": "Hook failures per session"
        case "mcp_fail_rate": "Server failure rate"
        case "fail_rate": "Command failure rate"
        case "out_bytes": "Output per run"
        case "denials": "Permission stops per session"
        case "verified_share": "Sessions checked after the last edit"
        default: "Measure"
        }
    }

    static func value(_ m: String, _ v: Double) -> String {
        switch m {
        case "fail_rate", "mcp_fail_rate", "verified_share": Format.percent(v * 100)
        case "out_bytes": "\(Int((v / 4 / 1000).rounded()))k tokens"
        default: String(format: "%.1f", v)
        }
    }

    static func explain(_ e: Insights.Applied) -> String {
        switch e.verdict {
        case "worked": "It worked: this moved the way the fix said it would."
        case "no_change": "No change yet. If it stays that way, undo it and try the prompt instead."
        case "measuring": "Measuring: a verdict after three sessions that could show it."
        default: "Not measured: this change has nothing it was chosen to move."
        }
    }
}

struct VerdictBadge: View {
    var verdict: String?
    var body: some View {
        let (text, color): (String, Color) = switch verdict {
        case "worked": ("Worked", .green)
        case "no_change": ("No change", .orange)
        case "measuring": ("Measuring", .blue)
        default: ("Not measured", .secondary)
        }
        Text(text).font(.callout.weight(.semibold)).foregroundStyle(color)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(color.opacity(0.15), in: Capsule())
    }
}

// MARK: - Autotune

struct AutotunePane: View {
    @EnvironmentObject var model: CompanionModel
    var insights: Insights?

    var body: some View {
        PaneTitle(title: "Autotune", subtitle: "Once a day, AgentPlay applies the safe fixes (notes in CLAUDE.local.md) in repos you allow, measures them, and takes them back if they didn't help. Changes to settings wait here for you.")
        let at = insights?.autotune
        Card {
            Label(at?.enabled == true ? "On for at least one repo" : "Off", systemImage: at?.enabled == true ? "checkmark.circle.fill" : "pause.circle")
                .font(.headline).foregroundStyle(at?.enabled == true ? .green : .secondary)
            Text(at?.lastRun.map { "Last ran \($0.prefix(10))." } ?? "Hasn't run yet.").font(.callout).foregroundStyle(.secondary)
            Button("Choose Repos in AgentPlay…") { model.openInsights(page: "autotune") }.buttonStyle(.link)
        }
        Text("Waiting for you").font(.title2.weight(.semibold)).padding(.top, 6)
        let pending = at?.pending ?? []
        if pending.isEmpty { EmptyNote(text: "Nothing waiting.") }
        ForEach(pending) { p in
            Card {
                HStack(alignment: .firstTextBaseline) {
                    Text(Plain.text(p.title)).font(.headline)
                    Spacer()
                    if let pts = p.points, pts > 0 { Text("+\(Format.points(pts))").foregroundStyle(.green) }
                }
                if let repo = p.repo { Text(repo).font(.callout).foregroundStyle(.secondary) }
                HStack {
                    Button("Approve") { model.autotune("approve", p) }.buttonStyle(.borderedProminent)
                    Button("Dismiss") { model.autotune("dismiss", p) }
                }
            }
        }
    }
}

// MARK: - Steering and usage

struct SteeringPane: View {
    var signals: Signals?
    var body: some View {
        PaneTitle(title: "How you steer", subtitle: "Counted from what you type to Claude Code. No message text is kept.")
        if let s = signals {
            Card { SteeringCard(signals: s) }
            Card { SignalsCard(signals: s) }
        } else {
            EmptyNote(text: "Appears after about 20 messages to Claude Code.")
        }
    }
}

struct UsagePane: View {
    var report: UsageReport?
    var body: some View {
        PaneTitle(title: "Usage", subtitle: "Estimated from your agents' own transcripts, at list prices.")
        if let r = report { Card { UsageDetail(report: r) } } else { EmptyNote(text: "No usage yet.") }
    }
}
