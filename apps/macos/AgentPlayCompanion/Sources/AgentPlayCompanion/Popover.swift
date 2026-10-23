import AgentPlayCore
import AppKit
import SwiftUI

struct PopoverView: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    @Environment(\.openWindow) private var openWindow
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var now = Date()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().overlay(Theme.hairline(scheme))
            Group {
                switch model.direction {
                case .v1: v1
                case .pulse: PulseDirection(now: now)
                case .breakpoint: BreakpointDirection(now: now)
                case .lab: LabDirection(now: now)
                }
            }
            .padding(Theme.s4)
            if let t = model.toast {
                Text(t).font(.system(size: 11)).foregroundStyle(Theme.ink(scheme))
                    .padding(.horizontal, Theme.s4).padding(.bottom, Theme.s2)
                    .task(id: t) { try? await Task.sleep(for: .seconds(3)); if model.toast == t { model.toast = nil } }
                    .accessibilityAddTraits(.updatesFrequently)
            }
            Divider().overlay(Theme.hairline(scheme))
            footer
        }
        .frame(width: Theme.popoverWidth)
        .background(Theme.paper(scheme))
        .onAppear { model.popoverOpen = true }
        .onDisappear { model.popoverOpen = false; model.showConstellation = false }
    }

    @ViewBuilder private var header: some View {
        HStack(spacing: Theme.s2) {
            Button {
                // The hidden detail: the mark flips Now into the session constellation.
                withAnimation(Theme.motion(reduceMotion)) { model.showConstellation.toggle() }
            } label: {
                Image(nsImage: MenuBarMark.image(pulse: false, size: 16)).renderingMode(.template)
                    .foregroundStyle(Theme.ink(scheme))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Session constellation")
            Text("AgentPlay").font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.ink(scheme)).fixedSize()
            Spacer()
            if model.isSample { SampleBadge() }
            LocalBadge()
        }
        .padding(.horizontal, Theme.s4).padding(.top, Theme.s3).padding(.bottom, model.direction == .v1 ? Theme.s1 : Theme.s3)
        if model.direction == .v1 {
            HStack(spacing: 2) {
                ForEach(Tab.allCases) { t in
                    Button(t.rawValue) { model.tab = t; model.showConstellation = false }
                        .buttonStyle(TabButtonStyle(selected: model.tab == t))
                        .keyboardShortcut(KeyEquivalent(Character("\(Tab.allCases.firstIndex(of: t)! + 1)")), modifiers: .command)
                }
                Spacer()
            }
            .padding(.horizontal, Theme.s3).padding(.bottom, Theme.s2)
        }
    }

    @ViewBuilder private var v1: some View {
        if model.showConstellation, let run = model.orderedRuns.first {
            Constellation(run: run)
                .onTapGesture { withAnimation(Theme.motion(reduceMotion)) { model.showConstellation = false } }
        } else {
            switch model.tab {
            case .now: NowView(now: now)
            case .limits: LimitsView(now: now)
            case .learn: LearnView(now: now)
            }
        }
    }

    private var footer: some View {
        HStack(spacing: Theme.s2) {
            Button("Open full AgentPlay") { model.openViewer() }
                .buttonStyle(QuietButtonStyle())
                .keyboardShortcut("o", modifiers: .command)
                .disabled(model.viewerBusy)
            if model.viewerBusy { ProgressView().controlSize(.small) }
            Spacer()
            if let u = model.lastUpdated, !model.isSample {
                Text("Updated \(Format.relative(u, now: now))").font(.system(size: 10)).foregroundStyle(Theme.muted(scheme))
            }
            Button {
                NSApp.activate(ignoringOtherApps: true)
                openWindow(id: "settings")
            } label: { Image(systemName: "gearshape") }
                .buttonStyle(QuietButtonStyle())
                .keyboardShortcut(",", modifiers: .command)
                .accessibilityLabel("Settings")
        }
        .padding(.horizontal, Theme.s4).padding(.vertical, Theme.s2)
    }
}

struct TabButtonStyle: ButtonStyle {
    var selected: Bool
    @Environment(\.colorScheme) private var scheme
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11, weight: selected ? .semibold : .regular))
            .lineLimit(1)
            .fixedSize()
            .foregroundStyle(selected ? Theme.ink(scheme) : Theme.muted(scheme))
            .padding(.horizontal, 8).frame(minHeight: 22)
            .background(Capsule().fill(selected ? Theme.hairline(scheme).opacity(1.8) : .clear))
            .contentShape(Rectangle())
            .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

struct LocalBadge: View {
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(Theme.limeInk(scheme)).frame(width: 5, height: 5)
            Text("Local").font(.system(size: 10, weight: .medium)).foregroundStyle(Theme.muted(scheme))
        }
        .help("Everything here is read from this Mac. Nothing is uploaded.")
        .accessibilityLabel("Local only. Nothing is uploaded.")
    }
}

// MARK: - Now

struct NowView: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    var now: Date
    @State private var query = ""
    @FocusState private var searchFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            switch model.phase {
            case .starting:
                ScanningState(found: nil)
            case .scanning(let found) where model.snapshot == nil:
                ScanningState(found: found)
            case .failed(let e):
                ErrorState(error: e)
            default:
                if let s = model.snapshot { content(s) } else { ScanningState(found: nil) }
            }
        }
    }

    @ViewBuilder private func content(_ s: Snapshot) -> some View {
        if model.scanStalled { PermissionHint() }
        if case .scanning(let found) = model.phase {
            // A rescan with data already on screen: keep the cards, say what's happening.
            HStack(spacing: 6) {
                ProgressView().controlSize(.mini)
                Text(found.map { "Rescanning · \($0) sessions indexed" } ?? "Rescanning")
                    .font(.system(size: 11)).foregroundStyle(Theme.muted(scheme))
                Button("Cancel") { model.cancelScan() }.buttonStyle(LinkButtonStyle())
            }
        }
        if let e = model.lastError {
            Text(e.localizedDescription).font(.system(size: 11)).foregroundStyle(Theme.error(scheme))
        }
        if !s.store.available {
            NoAgentsState(snapshot: s)
        } else {
            let runs = model.orderedRuns
            let active = runs.filter { $0.state != .idle }
            if active.isEmpty, let last = runs.first {
                VStack(alignment: .leading, spacing: 4) {
                    SectionLabel(text: "No active runs detected")
                    Text("Last run \(Format.relative(last.latestAt, now: now)).")
                        .font(.system(size: 12)).foregroundStyle(Theme.muted(scheme))
                }
            } else {
                SectionLabel(text: "Current runs")
            }
            let visible = model.showAll ? filtered(runs) : Array((active.isEmpty ? Array(runs.prefix(1)) : active).prefix(2))
            if model.showAll {
                TextField("Search recent runs", text: $query)
                    .textFieldStyle(.roundedBorder).font(.system(size: 12))
                    .focused($searchFocused)
                    .onAppear { searchFocused = true }
            }
            ScrollView {
                VStack(spacing: Theme.s2) {
                    ForEach(visible) { RunCard(run: $0, now: now) }
                }
            }
            .frame(maxHeight: model.showAll ? 340 : nil)
            .scrollDisabled(!model.showAll)
            HStack {
                if runs.count > visible.count || model.showAll {
                    Button(model.showAll ? "Show less" : "View all \(runs.count)") {
                        model.showAll.toggle(); query = ""
                    }
                    .buttonStyle(LinkButtonStyle())
                    .keyboardShortcut("k", modifiers: .command)
                }
                Spacer()
            }
            if !model.showAll, let a = model.worthALook {
                WorthALook(alert: a, now: now)
            }
            if let bad = s.unreadable, !bad.isEmpty {
                Text("Can't read \(bad.count) session\(bad.count == 1 ? "" : "s"). \(bad[0].message)")
                    .font(.system(size: 11)).foregroundStyle(Theme.muted(scheme))
            }
        }
    }

    private func filtered(_ runs: [RunActivity]) -> [RunActivity] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return runs }
        return runs.filter { r in
            [r.agentLabel, r.repo ?? "", r.lastEvent?.label ?? "", r.models?.joined(separator: " ") ?? ""]
                .joined(separator: " ").lowercased().contains(q)
        }
    }
}

/// Direction B's evidence card, used by v1 as "Worth a look".
struct WorthALook: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    var alert: AlertCandidate
    var now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionLabel(text: alert.isResolved ? "Worth a look · since resolved" : "Worth a look")
            Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.ink(scheme))
            Text(subtitle).font(.system(size: 12)).foregroundStyle(Theme.muted(scheme))
            HStack(spacing: Theme.s2) {
                Button("See sequence") { model.openViewer(sessionId: alert.sessionId, seq: alert.firstSeq ?? alert.triggerSeq) }
                    .buttonStyle(QuietButtonStyle(prominent: !alert.isResolved))
                Button("Mute this pattern") { model.mutePattern(alert.patternKey) }
                    .buttonStyle(QuietButtonStyle())
            }
        }
        .padding(Theme.s3)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous)
            .fill(alert.isResolved ? Theme.card(scheme) : Theme.limeWash(scheme)))
        .accessibilityElement(children: .contain)
    }

    var title: String {
        switch alert.rule {
        case .repeatedFailure: "\(alert.command ?? "A command") failed \(alert.count ?? 3) times · \(AgentNames.label(alert.agentTool))"
        case .unresolvedEnd: "A run ended with unresolved errors · \(AgentNames.label(alert.agentTool))"
        case .unknown: "A run worth checking"
        }
    }

    var subtitle: String {
        let repo = model.run(alert.sessionId)?.repo.map { " · \(Format.truncate($0))" } ?? ""
        let when = (alert.firstAt ?? alert.triggerAt).map { "Since \(Format.clock($0))" } ?? "Time unknown"
        let resolved = alert.isResolved ? " · passed later in the run" : ""
        return "\(when)\(repo) · \(Format.errClass(alert.errClass))\(resolved)"
    }
}

struct ScanningState: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    var found: Int?
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s2) {
            HStack(spacing: Theme.s2) {
                ProgressView().controlSize(.small)
                Text("Finding your sessions…").font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.ink(scheme))
            }
            Text(found.map { "\($0) sessions indexed so far. Only known agent folders are read, and nothing leaves this Mac." }
                 ?? "Reading known agent folders on this Mac. Nothing leaves it.")
                .font(.system(size: 12)).foregroundStyle(Theme.muted(scheme))
            if model.scanStalled { PermissionHint() }
            Button("Cancel") { model.cancelScan() }.buttonStyle(QuietButtonStyle())
        }
        .accessibilityElement(children: .combine)
    }
}

struct ErrorState: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    var error: SnapshotError
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s2) {
            Text("Can't read your sessions right now").font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.ink(scheme))
            Text(error.localizedDescription).font(.system(size: 12)).foregroundStyle(Theme.muted(scheme))
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("Try again") { Task { await model.refresh(.read) } }.buttonStyle(QuietButtonStyle(prominent: true))
                Button("Try a sample") { model.loadSample() }.buttonStyle(QuietButtonStyle())
            }
        }
    }
}

struct NoAgentsState: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    var snapshot: Snapshot
    var body: some View {
        let found = snapshot.foundSources
        VStack(alignment: .leading, spacing: Theme.s2) {
            Text(found.isEmpty ? "No local coding-agent sessions yet" : "No sessions built yet")
                .font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.ink(scheme))
            Text(found.isEmpty
                 ? "AgentPlay looked in the usual folders for Claude Code, Codex, Cursor, Copilot, OpenCode and Kiro. Start an agent, or point AgentPlay at the folder it uses."
                 : "Found \(found.map(\.label).joined(separator: ", ")). Scan to build replays from them.")
                .font(.system(size: 12)).foregroundStyle(Theme.muted(scheme)).fixedSize(horizontal: false, vertical: true)
            ForEach(snapshot.sources.filter { $0.status == .error }) { s in
                Text("\(s.label): \(s.remedy ?? "can't be read")").font(.system(size: 11)).foregroundStyle(Theme.error(scheme))
            }
            HStack(spacing: Theme.s2) {
                if !found.isEmpty {
                    Button("Scan now") { Task { await model.refresh(.fullScan) } }.buttonStyle(QuietButtonStyle(prominent: true))
                }
                Button("Choose folder") { model.chooseFolder(for: "claude-code") }.buttonStyle(QuietButtonStyle())
                Button("Try a sample") { model.loadSample() }.buttonStyle(QuietButtonStyle())
            }
            ExternalLink(title: "See supported agents",
                         url: URL(string: "https://github.com/Entelligence-AI/agentplay#supported-agents")!)
        }
    }
}

// MARK: - Limits

struct LimitsView: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    var now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            if let s = model.snapshot {
                let runs = model.orderedRuns.filter { $0.state != .idle }.prefix(3)
                SectionLabel(text: "Context · this run's window")
                if runs.isEmpty {
                    Text("No active runs. Context is per run, so it shows here while one is going.")
                        .font(.system(size: 12)).foregroundStyle(Theme.muted(scheme))
                }
                ForEach(Array(runs)) { r in ContextRow(run: r, now: now) }
                Divider().overlay(Theme.hairline(scheme))
                SectionLabel(text: "Plan · your account's allowance")
                ForEach(s.limits) { l in PlanRow(limit: l, now: now) }
                Text("Context is how full one conversation is. Plan usage is your account's allowance across every device. Token counts can't tell you either one.")
                    .font(.system(size: 11)).foregroundStyle(Theme.muted(scheme)).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct ContextRow: View {
    var run: RunActivity
    var now: Date
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        let c = run.context
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(run.agentLabel).font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.ink(scheme))
                if let repo = run.repo { Text(Format.truncate(repo, max: 16)).font(.system(size: 12)).foregroundStyle(Theme.muted(scheme)) }
                Spacer()
                Text(valueText).font(.system(size: 12, weight: .semibold).monospacedDigit()).foregroundStyle(Theme.ink(scheme))
            }
            if let pct = c.value?.percent {
                Meter(fraction: pct / 100)
            }
            HStack(spacing: 4) {
                ProvenanceChip(text: c.kind == .providerReported ? "Reported by \(run.agentLabel)" : c.kind.label,
                               stale: c.isStale(now: now, maxAge: 1800))
                if let at = c.observedAt { Text("as of \(Format.clock(at))").font(.system(size: 10)).foregroundStyle(Theme.muted(scheme)) }
            }
            if c.value?.percent == nil || c.isStale(now: now, maxAge: 1800) {
                Text(c.explanation).font(.system(size: 11)).foregroundStyle(Theme.muted(scheme))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .help(c.explanation)
        .accessibilityElement(children: .combine)
    }

    var valueText: String {
        guard let v = run.context.value else { return "Context usage unavailable" }
        if let pct = v.percent, let limit = v.limit { return "\(Format.percent(pct)) · \(Format.tokens(v.tokens)) of \(Format.tokens(limit))" }
        return "\(Format.tokens(v.tokens)) tokens"
    }
}

struct PlanRow: View {
    var limit: ProviderLimitWindow
    var now: Date
    @Environment(\.colorScheme) private var scheme

    var stale: Bool {
        limit.used.freshness == .stale || (limit.resetsAt.map { $0 <= now } ?? false)
            || limit.used.isStale(now: now, maxAge: 6 * 3600)
    }

    var timing: String {
        var parts: [String] = []
        if let at = limit.used.observedAt { parts.append("updated \(Format.relative(at, now: now))") }
        if let r = limit.resetsAt, r > now { parts.append("resets \(Format.clock(r))") }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(limit.providerLabel).font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.ink(scheme))
                Text(limit.windowLabel).font(.system(size: 12)).foregroundStyle(Theme.muted(scheme))
                Spacer()
                if let v = limit.used.value {
                    // A stale reading is shown greyed with its age, never as a confident "left" badge.
                    Text("\(Format.percent(v.leftPercent)) left")
                        .font(.system(size: 12, weight: .semibold).monospacedDigit())
                        .foregroundStyle(stale ? Theme.muted(scheme) : Theme.ink(scheme))
                        .strikethrough(stale && (limit.resetsAt.map { $0 <= now } ?? false))
                } else {
                    Text("Plan usage unavailable").font(.system(size: 12)).foregroundStyle(Theme.muted(scheme))
                }
            }
            if let v = limit.used.value, !stale { Meter(fraction: v.usedPercent / 100) }
            if limit.used.isAvailable {
                HStack(spacing: 4) {
                    ProvenanceChip(text: "Reported by \(limit.providerLabel)", stale: stale)
                    Text(timing).font(.system(size: 10)).foregroundStyle(Theme.muted(scheme)).lineLimit(1)
                }
            }
            if !limit.used.isAvailable || stale || limit.used.confidence == .low {
                Text(limit.used.explanation).font(.system(size: 11)).foregroundStyle(Theme.muted(scheme))
                    .fixedSize(horizontal: false, vertical: true)
                if let u = limit.usageUrl.flatMap(URL.init(string:)) {
                    ExternalLink(title: "Open provider usage", url: u)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }
}

struct Meter: View {
    var fraction: Double
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.hairline(scheme).opacity(1.5))
                Capsule().fill(fraction >= 0.9 ? Theme.error(scheme) : Theme.limeInk(scheme))
                    .frame(width: g.size.width * min(1, max(0, fraction)))
            }
        }
        .frame(height: 4)
        .accessibilityHidden(true)
    }
}

// MARK: - Learn

struct LearnView: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    var now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            if let card = model.learnCard {
                LearnCardView(card: card, now: now)
            } else {
                SectionLabel(text: "Learn")
                Text("Nothing stands out across your recent runs. A failure has to recur in at least two runs before it shows here.")
                    .font(.system(size: 12)).foregroundStyle(Theme.muted(scheme)).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct LearnCardView: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    var card: LearnCard
    var now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionLabel(text: card.denominator)
            Text(.init(card.title)).font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.ink(scheme))
            Text(card.detail).font(.system(size: 12)).foregroundStyle(Theme.muted(scheme)).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 4) {
                ProvenanceChip(text: "Observed")
                Text("Counted from your local maps. Not a suggestion, and not a score.")
                    .font(.system(size: 10)).foregroundStyle(Theme.muted(scheme))
            }
            HStack(spacing: Theme.s2) {
                Button("Review evidence") {
                    if let e = card.evidence.first { model.openViewer(sessionId: e.sessionId, seq: e.seq) }
                }
                .buttonStyle(QuietButtonStyle(prominent: true))
                Button("Dismiss") { model.dismissLearn(card.patternKey) }.buttonStyle(QuietButtonStyle())
            }
            if card.evidence.count > 1 {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(card.evidence) { e in
                        Button {
                            model.openViewer(sessionId: e.sessionId, seq: e.seq)
                        } label: {
                            HStack {
                                Text(AgentNames.label(e.agentTool)).font(.system(size: 11))
                                Text("first failure \(Format.relative(e.at, now: now))").font(.system(size: 11))
                                    .foregroundStyle(Theme.muted(scheme))
                                Spacer()
                                Image(systemName: "arrow.up.right").font(.system(size: 9)).foregroundStyle(Theme.muted(scheme))
                            }
                            .frame(minHeight: 22).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .padding(Theme.s3)
        .background(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous).fill(Theme.card(scheme)))
    }
}

// MARK: - Prototype directions (dev builds only switch between these)

/// A: Pulse. Runs first, context and plan inline, one "worth a look". Became the v1 Now tab.
struct PulseDirection: View {
    @EnvironmentObject var model: CompanionModel
    var now: Date
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            NowView(now: now)
            if model.snapshot?.store.available == true {
                Divider()
                SectionLabel(text: "Context / Plan")
                ForEach(model.orderedRuns.filter { $0.state == .active }.prefix(2)) { ContextRow(run: $0, now: now) }
                ForEach(model.snapshot?.limits.filter { $0.used.isAvailable } ?? []) { PlanRow(limit: $0, now: now) }
            }
        }
    }
}

/// B: Breakpoint. Nearly empty until a recorded condition is met; then one evidence card.
struct BreakpointDirection: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    var now: Date
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            if let a = model.worthALook {
                WorthALook(alert: a, now: now)
            } else {
                Text("Nothing needs you.").font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.ink(scheme))
                Text("\(model.snapshot?.activeRuns.count ?? 0) running. AgentPlay speaks up only when a recorded condition is met.")
                    .font(.system(size: 12)).foregroundStyle(Theme.muted(scheme))
            }
        }
    }
}

/// C: Harness Lab. One cross-session pattern, read-only.
struct LabDirection: View {
    @EnvironmentObject var model: CompanionModel
    var now: Date
    var body: some View { LearnView(now: now) }
}

/// Opens a web page in the browser, only on click. Styled like the other quiet links.
struct ExternalLink: View {
    var title: String
    var url: URL
    var body: some View {
        Button { NSWorkspace.shared.open(url) } label: {
            HStack(spacing: 3) { Text(title); Image(systemName: "arrow.up.right").font(.system(size: 9)) }
        }
        .buttonStyle(LinkButtonStyle())
        .help(url.host ?? url.absoluteString)
    }
}

/// Shown when a scan stalls. The usual cause is a macOS Files and Folders prompt waiting for an answer.
struct PermissionHint: View {
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Still scanning. Is macOS asking for folder access?").font(.system(size: 12, weight: .medium))
                .foregroundStyle(Theme.ink(scheme))
            Text("If your repos live in Documents, Desktop or Downloads, macOS asks once before AgentPlay can look there. It checks paths and git settings to tell which repo a file belongs to. If you decline, it can't tell which repo those sessions worked in.")
                .font(.system(size: 11)).foregroundStyle(Theme.muted(scheme)).fixedSize(horizontal: false, vertical: true)
            ExternalLink(title: "Open Files and Folders settings",
                         url: URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders")!)
        }
        .padding(Theme.s2)
        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.card(scheme)))
    }
}
