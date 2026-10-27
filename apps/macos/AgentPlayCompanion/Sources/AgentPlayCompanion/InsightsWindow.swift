import AgentPlayCore
import AppKit
import SwiftUI

/// The parts of the Insights window, in sidebar order.
enum InsightsSection: String, CaseIterable, Identifiable {
    case overview, fixes, applied, setup, autotune, steering, usage
    var id: String { rawValue }
    var title: String {
        switch self {
        case .overview: "Overview"
        case .fixes: "Fixes"
        case .applied: "Applied"
        case .setup: "Your Setup"
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
        case .setup: "square.stack.3d.up"
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
        w.setContentSize(NSSize(width: 720, height: 680))
        w.minSize = NSSize(width: 620, height: 480)
        return w
    }
}

struct InsightsView: View {
    @EnvironmentObject var model: CompanionModel

    var insights: Insights? { model.snapshot?.insights }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 34) {
                    section(.overview) { OverviewPane(insights: insights) }
                    section(.fixes) { FixesPane(insights: insights) }
                    if !(insights?.applied ?? []).isEmpty { section(.applied) { AppliedPane(insights: insights) } }
                    if let st = model.snapshot?.usage?.setup { section(.setup) { SetupPane(report: st) } }
                    section(.autotune) { AutotunePane(insights: insights) }
                    section(.steering) { SteeringPane(signals: model.snapshot?.usage?.signals) }
                    section(.usage) { UsagePane(report: model.snapshot?.usage) }
                }
                .padding(.horizontal, 36).padding(.top, 40).padding(.bottom, 48)
                .frame(maxWidth: 680, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            .onAppear { DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { proxy.scrollTo(model.insightsSection, anchor: .top) } }
            .onChange(of: model.insightsSection) { withAnimation { proxy.scrollTo(model.insightsSection, anchor: .top) } }
        }
        .frame(minWidth: 620, minHeight: 480)
    }

    func section<C: View>(_ id: InsightsSection, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 12) { content() }.id(id)
    }
}

// MARK: - Pieces

struct PaneTitle: View {
    var title: String
    var subtitle: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.title2.weight(.semibold))
            if let subtitle { Text(subtitle).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
        }
    }
}

struct Card<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 10) { content }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
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
        Text(text).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - Overview

struct OverviewPane: View {
    @EnvironmentObject var model: CompanionModel
    var insights: Insights?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let ins = insights, let score = ins.score {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("Run health").font(.title2.weight(.semibold))
                    Spacer()
                    Text("\(Int(score.rounded()))").font(.system(size: 34, weight: .semibold, design: .rounded))
                    Text("/ 100").foregroundStyle(.secondary)
                }
                Text(Self.summary(ins)).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Card {
                    ForEach(ins.components) { c in
                        HStack(spacing: 12) {
                            Text(c.name).frame(width: 150, alignment: .leading)
                            ProgressView(value: min(20, max(0, c.points ?? 0)), total: 20)
                                .tint(((c.points ?? 0) < 10) ? .orange : .accentColor)
                            Text(Format.points(c.points ?? 0)).monospacedDigit().frame(width: 34, alignment: .trailing)
                        }
                        .help(Self.meaning(c.key))
                    }
                    Text("Each part is out of 20. Point at one for what it counts.").font(.caption).foregroundStyle(.secondary)
                }
            } else {
                PaneTitle(title: "Run health", subtitle: "Appears after \(insights?.minSessions ?? 10) sessions in the last 30 days.")
            }
        }
    }

    /// One sentence: which way the score went and the part that moved it most.
    static func summary(_ ins: Insights) -> String {
        guard let score = ins.score else { return "" }
        var out = "How your agents' sessions went in the last \(ins.days) days."
        if let prev = ins.prevScore, abs(score - prev) >= 0.5 {
            out = "\(score > prev ? "Up" : "Down") \(Format.points(abs(score - prev))) from \(Int(prev.rounded())) the month before"
            let moved = ins.components.compactMap { c -> (String, Double)? in
                guard let a = c.points, let b = c.prevPoints else { return nil }
                return (c.name, a - b)
            }.max { abs($0.1) < abs($1.1) }
            if let m = moved, abs(m.1) >= 0.5 { out += ", mostly \(m.0.lowercased()) (\(m.1 > 0 ? "+" : "")\(Format.points(m.1)))." } else { out += "." }
        }
        return out
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
        PaneTitle(title: "Do this next", subtitle: "What your own sessions say would help, best first.")
        let fixes = insights?.fixes ?? []
        if fixes.isEmpty {
            EmptyNote(text: "Nothing to fix right now.")
        }
        ForEach(fixes) { f in
            Card {
                HStack(alignment: .firstTextBaseline) {
                    Text(Plain.text(f.title)).font(.headline)
                    Spacer()
                    if let p = f.points, p > 0 { Text("+\(Format.points(p))").font(.subheadline.monospacedDigit()).foregroundStyle(.secondary) }
                }
                Text(Plain.text(f.pattern)).font(.callout).fixedSize(horizontal: false, vertical: true)
                if let cause = f.hook?.cause {
                    Text(cause).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 12) {
                    if f.applicable {
                        Button("Apply…") { model.applyFix(f) }.buttonStyle(.borderedProminent).controlSize(.regular)
                    }
                    Button("Copy Prompt") { model.copyText(FixDetail.prompt(f)) }.controlSize(.regular)
                    if let first = f.evidence.first {
                        Button("Show a Session") { model.openViewer(sessionId: first) }.buttonStyle(.link)
                    }
                    Spacer()
                    Text(Plain.text(f.effect)).font(.caption).foregroundStyle(.secondary).lineLimit(2).frame(maxWidth: 260, alignment: .trailing)
                }
                .padding(.top, 2)
            }
        }
    }
}

// MARK: - Applied

struct AppliedPane: View {
    @EnvironmentObject var model: CompanionModel
    var insights: Insights?

    var body: some View {
        PaneTitle(title: "What changed", subtitle: "Changes AgentPlay made, and whether they worked.")
        let applied = insights?.applied ?? []
        if applied.isEmpty { EmptyNote(text: "Nothing applied yet.") }
        Card {
            ForEach(Array(applied.enumerated()), id: \.element.id) { i, e in
                if i > 0 { Divider() }
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(Plain.text(e.title ?? "A change")).font(.body.weight(.medium))
                        Text(Self.detail(e)).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    VerdictBadge(verdict: e.verdict)
                    Button("Undo") { model.undoApplied(e) }.controlSize(.small)
                }
                .padding(.vertical, 4)
            }
        }
    }

    static func detail(_ e: Insights.Applied) -> String {
        var bits: [String] = []
        if let iso = e.at, let d = ISO8601DateFormatter().date(from: iso) { bits.append(Format.weekdayTime(d)) }
        if let m = e.measure, let before = e.before {
            let after = e.after.map { value(m, $0) } ?? "–"
            bits.append("\(measureName(m)): \(value(m, before)) → \(after) over \(e.sessionsSince ?? 0) session\(e.sessionsSince == 1 ? "" : "s")")
        }
        return bits.joined(separator: " · ")
    }

    static func measureName(_ m: String) -> String {
        switch m {
        case "hook_fails": "hook failures per session"
        case "mcp_fail_rate": "server failure rate"
        case "fail_rate": "failure rate"
        case "out_bytes": "output per run"
        case "denials": "permission stops per session"
        case "verified_share": "sessions checked after the last edit"
        default: "measure"
        }
    }

    static func value(_ m: String, _ v: Double) -> String {
        switch m {
        case "fail_rate", "mcp_fail_rate", "verified_share": Format.percent(v * 100)
        case "out_bytes": "\(Int((v / 4 / 1000).rounded()))k tokens"
        default: String(format: "%.1f", v)
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

// MARK: - Your setup

struct SetupPane: View {
    var report: SetupReport

    var body: some View {
        PaneTitle(title: "Your setup", subtitle: "Skills, subagents, commands, MCP servers and hooks Claude Code loads for you, and how often each ran in the last \(report.days) days.")
        let used = report.items.filter { $0.uses > 0 && $0.kind != "hook" }
        let top = max(used.map(\.uses).max() ?? 1, 1)
        let unused = report.items.filter { $0.verdict == "unused" }
        let hooks = report.items.filter { $0.kind == "hook" }
        Card {
            Text("Used").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            if used.isEmpty { Text("Nothing ran in the last \(report.days) days.").foregroundStyle(.secondary) }
            ForEach(used) { it in
                HStack(spacing: 12) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(it.name).lineLimit(1)
                        Text(SetupCard.kindName(it.kind)).font(.caption).foregroundStyle(.secondary)
                    }
                    .frame(width: 220, alignment: .leading)
                    GeometryReader { g in
                        Capsule().fill(.quaternary).overlay(alignment: .leading) {
                            // Log scale, so one heavy MCP server doesn't flatten everything else.
                            Capsule().fill(Color.accentColor)
                                .frame(width: max(6, g.size.width * log(Double(it.uses) + 1) / log(Double(top) + 1)))
                        }
                    }
                    .frame(height: 6)
                    Text("\(it.uses)×").font(.callout.monospacedDigit()).foregroundStyle(.secondary).frame(width: 52, alignment: .trailing)
                }
            }
        }
        if !unused.isEmpty {
            Card {
                Text("Not used in \(report.days) days · \(unused.count)").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                ForEach(["skill", "agent", "mcp", "command"], id: \.self) { kind in
                    let names = unused.filter { $0.kind == kind }.map(\.name)
                    if !names.isEmpty {
                        Labelled(label: Self.title(kind), text: names.joined(separator: ", "))
                    }
                }
                Text("Their descriptions still load into every session. Removing the ones you don't need gives the agent more room.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        if !hooks.isEmpty {
            Card {
                Text("Hooks").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                ForEach(hooks) { it in
                    HStack(alignment: .firstTextBaseline) {
                        Text(it.name)
                        Spacer()
                        if let d = it.detail { Text(d).font(.callout.monospaced()).foregroundStyle(.secondary) }
                    }
                }
            }
        }
    }

    static func title(_ k: String) -> String {
        ["skill": "Skills", "agent": "Subagents", "mcp": "MCP servers", "command": "Slash commands", "hook": "Hooks"][k] ?? k
    }

}

// MARK: - Autotune

struct AutotunePane: View {
    @EnvironmentObject var model: CompanionModel
    var insights: Insights?

    var body: some View {
        PaneTitle(title: "Autotune", subtitle: "Applies safe notes in repos you allow, once a day, and takes back what didn't help.")
        let at = insights?.autotune
        Card {
            HStack {
                Text(at?.enabled == true ? "On" : "Off").font(.body.weight(.medium))
                Text(at?.lastRun.map { "· last ran \($0.prefix(10))" } ?? "").foregroundStyle(.secondary)
                Spacer()
                Button("Choose Repos…") { model.openInsights(page: "autotune") }.controlSize(.small)
            }
            ForEach(at?.pending ?? []) { p in
                Divider()
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(Plain.text(p.title)).font(.body.weight(.medium))
                        Text([p.repo, p.points.map { "+\(Format.points($0))" }].compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Approve") { model.autotune("approve", p) }.controlSize(.small)
                    Button("Dismiss") { model.autotune("dismiss", p) }.controlSize(.small)
                }
                .padding(.vertical, 3)
            }
        }
    }
}

// MARK: - Steering and usage

struct SteeringPane: View {
    var signals: Signals?
    var body: some View {
        PaneTitle(title: "How you steer", subtitle: "From what you type to Claude Code, the last 30 days against the 30 before.")
        if let s = signals {
            Card {
                row("Corrections", s.current.correctionRate, s.previous.correctionRate, "messages that turn the agent around")
                Divider()
                row("Frustration", s.current.frustrationRate, s.previous.frustrationRate, "messages that swear or pile up ! and ?")
                Divider()
                row("Interruptions", s.current.interruptRate, s.previous.interruptRate, "messages that stop the agent mid-step")
                Divider()
                row("Refusals", s.current.refusalRate, s.previous.refusalRate, "tool calls you said no to")
                Text("Of \(s.current.messages) messages. No message text is kept.").font(.caption).foregroundStyle(.secondary).padding(.top, 4)
            }
        } else {
            EmptyNote(text: "Appears after about 20 messages to Claude Code.")
        }
    }

    func row(_ name: String, _ now: Double?, _ before: Double?, _ what: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 1) {
                Text(name).font(.body.weight(.medium))
                Text(what).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Text(now.map { $0 < 1 && $0 > 0 ? String(format: "%.1f%%", $0) : Format.percent($0) } ?? "–")
                .font(.body.monospacedDigit())
            if let now, let before, abs(now - before) >= 0.5 {
                // Lower is better for all four: up is orange.
                Text("\(now > before ? "↑" : "↓") from \(Format.percent(before))").font(.caption.monospacedDigit())
                    .foregroundStyle(now > before ? .orange : .secondary).frame(width: 90, alignment: .trailing)
            } else {
                Text("same").font(.caption).foregroundStyle(.secondary).frame(width: 90, alignment: .trailing)
            }
        }
        .padding(.vertical, 3)
    }
}

struct UsagePane: View {
    var report: UsageReport?
    var body: some View {
        PaneTitle(title: "Usage", subtitle: "Estimated from your agents' own transcripts, at list prices.")
        if let r = report {
            Card {
                HStack(alignment: .firstTextBaseline) {
                    Text("Today").font(.body.weight(.medium)); Spacer()
                    Text("\(Format.money(r.today.costUsd)) · \(Format.tokens(r.today.tokens.total))").monospacedDigit()
                }
                HStack(alignment: .firstTextBaseline) {
                    Text("Last 7 days").font(.body.weight(.medium)); Spacer()
                    Text("\(Format.money(r.last7Days.costUsd)) · \(Format.tokens(r.last7Days.tokens.total))").monospacedDigit()
                }
                HStack(alignment: .firstTextBaseline) {
                    Text("Last 30 days").font(.body.weight(.medium)); Spacer()
                    Text("\(Format.money(r.last30Days.costUsd)) · \(Format.tokens(r.last30Days.tokens.total))").monospacedDigit()
                }
            }
            Card {
                Text("By model, 7 days").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                ForEach((r.last7Days.byModel ?? []).prefix(4)) { m in
                    HStack { Text(Format.modelName(m.key)); Spacer()
                        Text(m.isUnpriced ? "\(Format.tokens(m.tokens.total)) · unpriced" : "\(Format.money(m.costUsd)) · \(Format.tokens(m.tokens.total))").foregroundStyle(.secondary).monospacedDigit() }
                }
                Text("By project, 7 days").font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding(.top, 8)
                ForEach((r.last7Days.byRepo ?? []).prefix(4)) { p in
                    HStack { Text(p.key); Spacer()
                        Text("\(Format.money(p.costUsd)) · \(Format.tokens(p.tokens.total))").foregroundStyle(.secondary).monospacedDigit() }
                }
            }
        } else {
            EmptyNote(text: "No usage yet.")
        }
    }
}
