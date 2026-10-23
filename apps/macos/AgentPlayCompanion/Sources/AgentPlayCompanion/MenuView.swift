import AgentPlayCore
import AppKit
import Charts
import SwiftUI

/// The menu-bar window, built to behave like a native menu: one column, system material, flat rows that
/// highlight in the accent colour, arrow keys and Return, one action per row, secondary actions on right-click.
struct MenuView: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.openWindow) private var openWindow
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var now = Date()
    @State var highlighted: String?
    @FocusState private var focused: Bool

    var body: some View {
        let list = model.menuItems(now: now)
        // Header and the action rows stay put; only the content between them scrolls when it's tall.
        let split = list.firstIndex { $0.id == "sep:actions" } ?? list.count
        let head = Array(list.prefix(1)), middle = Array(list[1..<split]), foot = Array(list[split...])
        VStack(alignment: .leading, spacing: 0) {
            rows(head)
            ScrollView {
                VStack(alignment: .leading, spacing: 0) { rows(middle) }
            }
            .scrollIndicators(.automatic)
            .frame(maxHeight: Menu.maxHeight - 150)
            .fixedSize(horizontal: false, vertical: true)
            rows(foot)
        }
        .padding(.vertical, 6)
        .frame(width: Menu.width)
        .focusable()
        .focusEffectDisabled()
        .focused($focused)
        .onAppear { focused = true; model.popoverOpen = true }
        .onDisappear { model.popoverOpen = false; highlighted = nil }
        .onKeyPress(.downArrow) { move(1, in: list); return .handled }
        .onKeyPress(.upArrow) { move(-1, in: list); return .handled }
        .onKeyPress(.rightArrow) { toggleProvider(open: true, in: list); return .handled }
        .onKeyPress(.leftArrow) { toggleProvider(open: false, in: list); return .handled }
        .onKeyPress(.return) {
            if let i = list.first(where: { $0.id == highlighted }) { activate(i) }
            return .handled
        }
        .background(shortcuts)
        .overlay(alignment: .bottom) {
            if let t = model.toast {
                Text(t).font(Menu.secondary).padding(.horizontal, 10).padding(.vertical, 6)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
                    .padding(8)
                    .task(id: t) { try? await Task.sleep(for: .seconds(3)); if model.toast == t { model.toast = nil } }
            }
        }
    }

    // MARK: rows

    func rows(_ items: [MenuItem]) -> some View {
        ForEach(items) { item in
            row(item)
                .onHover { inside in
                    guard item.isSelectable else { return }
                    if inside { highlighted = item.id } else if highlighted == item.id { highlighted = nil }
                }
        }
    }

    @ViewBuilder func row(_ item: MenuItem) -> some View {
        let hi = highlighted == item.id
        switch item {
        case .header(let updated):
            HStack(spacing: 6) {
                Image(nsImage: MenuBarMark.image(pulse: false, size: 15)).renderingMode(.template)
                Text("AgentPlay").font(Menu.title)
                Spacer()
                Text(updated).font(Menu.value).foregroundStyle(.secondary)
            }
            .padding(.horizontal, Menu.inset).padding(.top, 4).padding(.bottom, 4)

        case .message(let title, let detail):
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(Menu.body)
                if let detail { Text(detail).font(Menu.secondary).foregroundStyle(.secondary).lineLimit(2) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, Menu.inset).padding(.vertical, 4)

        case .separator:
            Rectangle().fill(Menu.separator).frame(height: 1)
                .padding(.horizontal, Menu.inset).padding(.vertical, 5)

        case .sectionHeader(let title, let trailing):
            HStack {
                Text(title).font(Menu.header).foregroundStyle(.secondary)
                Spacer()
                if let trailing { Text(trailing).font(Menu.value).foregroundStyle(.secondary) }
            }
            .padding(.horizontal, Menu.inset).padding(.top, 2).padding(.bottom, 3)

        case .provider(let p, let expanded):
            RowShell(highlighted: hi, action: { activate(item) }) {
                HStack(spacing: 6) {
                    Text(p.label).font(Menu.title)
                    if let plan = p.plan { Text(plan.capitalized).font(Menu.value).foregroundStyle(.secondary) }
                    Spacer()
                    if !expanded, let summary = collapsedSummary(p) {
                        Text(summary).font(Menu.value).foregroundStyle(.secondary)
                    }
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary).rotationEffect(.degrees(expanded ? 90 : 0))
                }
            }
            .accessibilityLabel("\(p.label), \(expanded ? "expanded" : "collapsed")")

        case .meter(let w):
            RowShell(highlighted: hi, action: { activate(item) }) {
                MeterBlock(window: w, now: now, showLeft: model.showsLeft(w.id))
            }
            .help(w.resetsAt.map { "Resets \(Format.clock($0)). \(w.used.explanation)" } ?? w.used.explanation)
            .contextMenu {
                Button(model.showsLeft(w.id) ? "Show Used" : "Show Left") { model.toggleLeft(w.id) }
                if let u = w.usageUrl.flatMap(URL.init(string:)) {
                    Button("Open \(w.providerLabel) Usage Page") { NSWorkspace.shared.open(u) }
                }
            }

        case .enableLimits(_, let detail):
            RowShell(highlighted: hi, action: { activate(item) }) {
                HStack(spacing: 8) {
                    Image(systemName: "gauge.with.dots.needle.33percent").frame(width: 16)
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Turn On Live Limits…").font(Menu.body)
                        Text(detail).font(Menu.secondary).opacity(0.75)
                    }
                    Spacer()
                }
            }

        case .usage(let period, let u, let expandable, let expanded):
            let content = HStack(spacing: 6) {
                Text(period).font(Menu.body)
                Spacer()
                Text(usageText(u)).font(Menu.value).opacity(0.75)
                if expandable {
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold))
                        .opacity(0.75).rotationEffect(.degrees(expanded ? 90 : 0))
                }
            }
            if expandable {
                RowShell(highlighted: hi, action: { activate(item) }) { content }
                    .help("Estimated from your local maps at list prices. Not your plan's allowance.")
            } else {
                content.padding(.horizontal, Menu.inset).padding(.vertical, 3)
            }

        case .usageDetail(let r):
            UsageDetail(report: r).padding(.horizontal, Menu.inset).padding(.vertical, 4)

        case .session(let s):
            RowShell(highlighted: hi, action: { activate(item) }) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    StatusDot(dot: s.dot)
                    VStack(alignment: .leading, spacing: 1) {
                        HStack {
                            Text(s.title).font(Menu.body).lineLimit(1)
                            Spacer(minLength: 6)
                            Text(s.trailing).font(Menu.value).opacity(0.75)
                        }
                        if !s.detail.isEmpty {
                            Text(s.detail).font(Menu.secondary).opacity(0.75).lineLimit(1).truncationMode(.middle)
                        }
                    }
                }
            }
            .contextMenu { SessionMenu(line: s) }
            .accessibilityHint(s.openable ? "Opens this session's replay" : "Not built yet")

        case .error(let e):
            RowShell(highlighted: hi, action: { activate(item) }) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: e.isPattern ? "arrow.triangle.2.circlepath"
                          : e.resolved ? "checkmark.circle" : "exclamationmark.triangle.fill")
                        .font(.system(size: 11)).frame(width: 16)
                        .foregroundStyle(hi ? AnyShapeStyle(Color.white)
                                         : e.isPattern || e.resolved ? AnyShapeStyle(.secondary) : AnyShapeStyle(Color.red))
                    VStack(alignment: .leading, spacing: 1) {
                        HStack {
                            Text(e.title).font(Menu.body).lineLimit(1)
                            Spacer(minLength: 6)
                            Text(e.trailing).font(Menu.value).opacity(0.75)
                        }
                        Text(e.detail).font(Menu.secondary).opacity(0.75).lineLimit(1)
                    }
                }
            }
            .contextMenu {
                Button("Open in AgentPlay") { activate(item) }
                if let k = e.patternKey {
                    Button(e.isPattern ? "Dismiss Pattern" : "Mute This Pattern") {
                        if e.isPattern { model.dismissLearn(k) } else { model.mutePattern(k) }
                    }
                }
            }

        case .action(let a):
            RowShell(highlighted: hi, height: 20, action: { activate(item) }) {
                HStack(spacing: 8) {
                    if let sym = a.symbol { Image(systemName: sym).frame(width: 16).opacity(0.75) }
                    Text(a.title).font(Menu.body)
                    Spacer()
                    if let k = a.shortcut { Text(k).font(Menu.value).opacity(0.5) }
                }
            }
        }
    }

    // MARK: behaviour

    func activate(_ item: MenuItem) {
        switch item {
        case .provider(let p, let expanded):
            withAnimation(Menu.disclosure(reduceMotion)) { model.setExpanded(p.provider, !expanded) }
        case .meter(let w):
            model.toggleLeft(w.id)
        case .usage(_, _, true, _):
            withAnimation(Menu.disclosure(reduceMotion)) { model.usageExpanded.toggle() }
        case .enableLimits:
            model.settingsPane = .claude
            perform(.settings)
        case .session(let s):
            guard s.openable else { model.toast = "Not built yet. It appears after the next refresh."; return }
            close()
            model.openViewer(sessionId: s.sessionId, seq: s.seq)
        case .error(let e):
            close()
            model.openViewer(sessionId: e.sessionId, seq: e.seq, errorsTab: !e.isPattern)
        case .action(let a):
            perform(a)
        default: break
        }
    }

    func perform(_ a: MenuAction) {
        switch a {
        case .refresh: model.scheduleRefresh(reason: .manual)
        case .openViewer, .showAllSessions: close(); model.openViewer()
        case .settings:
            close()
            NSApp.activate(ignoringOtherApps: true)
            openWindow(id: "settings")
        case .quit: NSApp.terminate(nil)
        }
    }

    func close() { MenuBarControl.close() }

    func move(_ d: Int, in list: [MenuItem]) {
        let sel = list.filter(\.isSelectable).map(\.id)
        guard !sel.isEmpty else { return }
        if let cur = highlighted, let i = sel.firstIndex(of: cur) {
            highlighted = sel[max(0, min(sel.count - 1, i + d))]
        } else {
            highlighted = d > 0 ? sel.first : sel.last
        }
    }

    func toggleProvider(open: Bool, in list: [MenuItem]) {
        guard let cur = list.first(where: { $0.id == highlighted }), case .provider(let p, let expanded) = cur,
              expanded != open else { return }
        withAnimation(Menu.disclosure(reduceMotion)) { model.setExpanded(p.provider, open) }
    }

    func collapsedSummary(_ p: ProviderSummary) -> String? {
        let five = p.window("five_hour")?.used.value?.usedPercent
        let week = p.window("weekly")?.used.value?.usedPercent
        switch (five, week) {
        case let (f?, w?): return "\(Format.percent(f)) · \(Format.percent(w)) wk"
        case let (f?, nil): return Format.percent(f)
        case let (nil, w?): return "\(Format.percent(w)) wk"
        default: return p.windows.isEmpty ? "Unavailable" : nil
        }
    }

    func usageText(_ u: UsagePeriod) -> String {
        var s = "\(Format.tokens(u.tokens.total)) tokens"
        if u.requests > u.unpricedRequests { s += " · \(Format.money(u.costUsd))" }
        else if u.unpricedRequests > 0 { s += " · unpriced" }
        return s
    }

    /// ⌘R, ⌘O, ⌘, and ⌘Q while the menu is open.
    var shortcuts: some View {
        ZStack {
            Button("") { perform(.refresh) }.keyboardShortcut("r", modifiers: .command)
            Button("") { perform(.openViewer) }.keyboardShortcut("o", modifiers: .command)
            Button("") { perform(.settings) }.keyboardShortcut(",", modifiers: .command)
            Button("") { perform(.quit) }.keyboardShortcut("q", modifiers: .command)
        }
        .opacity(0).frame(width: 0, height: 0).accessibilityHidden(true)
    }
}

/// A full-width menu row: native geometry, accent highlight on hover or keyboard, no animation.
struct RowShell<Content: View>: View {
    var highlighted: Bool
    var height: CGFloat? = nil
    var action: () -> Void
    @ViewBuilder var content: Content

    var body: some View {
        content
            .foregroundStyle(highlighted ? AnyShapeStyle(Color.white) : AnyShapeStyle(.primary))
            .environment(\.menuHighlighted, highlighted)
            .frame(maxWidth: .infinity, minHeight: height, alignment: .leading)
            .padding(.horizontal, Menu.inset - Menu.highlightInset)
            .padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: Menu.rowRadius, style: .continuous)
                .fill(highlighted ? Color.accentColor : Color.clear))
            .padding(.horizontal, Menu.highlightInset)
            .contentShape(Rectangle())
            .onTapGesture(perform: action)
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isButton)
            .accessibilityAction { action() }
    }
}

private struct MenuHighlightedKey: EnvironmentKey { static let defaultValue = false }
extension EnvironmentValues {
    var menuHighlighted: Bool {
        get { self[MenuHighlightedKey.self] }
        set { self[MenuHighlightedKey.self] = newValue }
    }
}

struct StatusDot: View {
    var dot: SessionLine.Dot
    @Environment(\.menuHighlighted) private var hi

    var body: some View {
        Image(systemName: filled ? "circle.fill" : "circle")
            .font(.system(size: 7))
            .foregroundStyle(hi ? AnyShapeStyle(Color.white) : color)
            .frame(width: 16)
            .accessibilityLabel(label)
    }

    var filled: Bool { dot == .working || dot == .waiting || dot == .idle }

    var color: AnyShapeStyle {
        switch dot {
        case .working: AnyShapeStyle(Color.accentColor)
        case .waiting: AnyShapeStyle(Color.orange)
        case .recent: AnyShapeStyle(.secondary)
        case .idle, .ended: AnyShapeStyle(Menu.tertiary)
        }
    }

    var label: String {
        switch dot {
        case .working: "Working"
        case .waiting: "Needs you"
        case .idle: "Idle"
        case .recent: "Recently active"
        case .ended: "Ended"
        }
    }
}

/// "Session" / bar / "34% used ... Resets in 2h 18m" / optional pace line. Tint follows pace, not level.
struct MeterBlock: View {
    var window: ProviderLimitWindow
    var now: Date
    var showLeft: Bool
    @Environment(\.menuHighlighted) private var hi

    var stale: Bool {
        window.used.freshness == .stale || (window.resetsAt.map { $0 <= now } ?? false)
            || window.used.isStale(now: now, maxAge: 6 * 3600)
    }

    var body: some View {
        let v = window.used.value
        let used = v?.usedPercent ?? 0
        let pace = Pace.of(used: used, windowMinutes: window.windowMinutes, resetsAt: window.resetsAt, now: now)
        let tint: PaceTint = stale ? .stale
            : pace.map { $0.level == .over ? .over : $0.level == .ahead ? .ahead : .normal } ?? .normal
        VStack(alignment: .leading, spacing: 3) {
            Text(window.window == "five_hour" ? "Session" : window.windowLabel).font(Menu.body)
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(hi ? Color.white.opacity(0.35) : Color.primary.opacity(0.12))
                    Capsule().fill(hi ? Color.white : tint.color)
                        .frame(width: max(3, g.size.width * min(1, used / 100)))
                }
            }
            .frame(height: 4)
            .accessibilityHidden(true)
            HStack {
                Text(showLeft ? "\(Format.percent(v?.leftPercent ?? 0)) left" : "\(Format.percent(used)) used")
                Spacer()
                Text(resetText)
            }
            .font(Menu.value).opacity(0.75)
            if let line = paceLine(pace) {
                Text(line).font(Menu.secondary).opacity(0.6)
            }
        }
        .padding(.vertical, 1)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(window.window == "five_hour" ? "Session" : window.windowLabel), \(Format.percent(used)) used, \(resetText)")
    }

    var resetText: String {
        if stale { return "Stale · \(Format.relative(window.used.observedAt, now: now))" }
        guard let r = window.resetsAt else { return "" }
        let left = r.timeIntervalSince(now)
        return left < 86400 ? "Resets in \(Format.duration(left))" : "Resets \(Format.weekdayTime(r))"
    }

    func paceLine(_ p: Pace?) -> String? {
        if stale { return "Updates when \(window.providerLabel) runs again" }
        guard let p, let out = p.runsOutAt, p.level != .normal else { return nil }
        return "At this pace, runs out in \(Format.duration(out.timeIntervalSince(now)))"
    }
}

struct UsageDetail: View {
    var report: UsageReport

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Chart(report.daily) { d in
                BarMark(x: .value("Day", d.date), y: .value("Tokens", d.tokens))
                    .foregroundStyle(Color.accentColor.opacity(d.id == report.daily.last?.id ? 1 : 0.4))
                    .cornerRadius(1.5)
            }
            .chartXAxis(.hidden)
            .chartYAxis(.hidden)
            .frame(height: 40)
            .accessibilityLabel("Tokens per day, last 14 days")
            HStack {
                Text("14 days ago"); Spacer(); Text("Today")
            }
            .font(Menu.secondary).foregroundStyle(Menu.tertiary)
            line("Last 7 days", report.last7Days)
            line("Last 30 days", report.last30Days)
            if let models = report.last7Days.byModel, !models.isEmpty {
                Text("By model · 7 days").font(Menu.header).foregroundStyle(.secondary).padding(.top, 4)
                ForEach(models.prefix(3)) { slice($0) }
            }
            if let repos = report.last7Days.byRepo, !repos.isEmpty {
                Text("By project · 7 days").font(Menu.header).foregroundStyle(.secondary).padding(.top, 4)
                ForEach(repos.prefix(3)) { slice($0) }
            }
        }
    }

    func line(_ title: String, _ p: UsagePeriod) -> some View {
        HStack {
            Text(title).font(Menu.secondary)
            Spacer()
            Text("\(Format.tokens(p.tokens.total)) · \(Format.money(p.costUsd))\(p.unpricedRequests > 0 ? " + unpriced" : "")")
                .font(Menu.value).foregroundStyle(.secondary)
        }
    }

    func slice(_ s: UsageSlice) -> some View {
        HStack {
            Text(s.key).font(Menu.secondary).lineLimit(1).truncationMode(.middle)
            Spacer()
            Text(s.isUnpriced ? "\(Format.tokens(s.tokens.total)) · unpriced"
                 : "\(Format.tokens(s.tokens.total)) · \(Format.money(s.costUsd))")
                .font(Menu.value).foregroundStyle(.secondary)
        }
    }
}

struct SessionMenu: View {
    @EnvironmentObject var model: CompanionModel
    var line: SessionLine

    var body: some View {
        if line.openable {
            Button("Open Replay") { MenuBarControl.close(); model.openViewer(sessionId: line.sessionId, seq: line.seq) }
            if let run = model.run(line.sessionId) {
                if let err = run.lastError {
                    Button("Jump to Last Error") {
                        MenuBarControl.close(); model.openViewer(sessionId: run.sessionId, seq: err.seq, errorsTab: true)
                    }
                }
                Divider()
                Button("Copy Redacted Summary") { model.copySummary(run) }
                Button("Copy Local Replay Link") { model.copyLink(run) }
            }
            Divider()
        }
        Button(model.prefs.pinnedSessions.contains(line.sessionId) ? "Unpin" : "Pin to Top") { model.togglePin(line.sessionId) }
        Button("Mute Alerts for This Session") { model.muteRun(line.sessionId) }
    }
}
