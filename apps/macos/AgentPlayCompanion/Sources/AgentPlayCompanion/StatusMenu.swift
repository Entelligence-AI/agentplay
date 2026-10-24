import AgentPlayCore
import AppKit
import Charts
import Combine
import SwiftUI

/// The status item and its menu: a real NSMenu, so hover, keyboard, submenus, dismissal and key equivalents
/// are the system's own. Every clickable row is a native NSMenuItem. The only custom views are display-only:
/// the provider cards, the usage summary and the usage chart, which take no clicks and no highlight.
@MainActor
final class StatusItemController: NSObject, NSMenuDelegate {
    let model: CompanionModel
    let statusItem: NSStatusItem
    let menu = NSMenu()
    private var bag: Set<AnyCancellable> = []
    private var iconPending = false
    /// Fixture renders draw at the fixtures' clock.
    var nowOverride: Date?

    init(model: CompanionModel, statusItem: NSStatusItem? = nil) {
        self.model = model
        self.statusItem = statusItem ?? NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        super.init()
        menu.delegate = self
        menu.autoenablesItems = false
        // No `statusItem.menu`: AppKit would open it on every click and we'd never see one. We open it ourselves,
        // so a click on the line beside Pip can act on what it says.
        if let b = self.statusItem.button {
            b.imagePosition = .imageLeading
            b.target = self
            b.action = #selector(clicked)
            b.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        model.objectWillChange
            .sink { [weak self] _ in self?.scheduleIconUpdate() }
            .store(in: &bag)
        model.$toast
            .compactMap { $0 }
            .sink { [weak self] text in
                guard let self else { return }
                Toast.show(text, under: self.statusItem.button)
                DispatchQueue.main.async { self.model.toast = nil }
            }
            .store(in: &bag)
        updateIcon()
    }

    // MARK: icon

    private func scheduleIconUpdate() {
        guard !iconPending else { return }
        iconPending = true
        DispatchQueue.main.async { [weak self] in
            self?.iconPending = false
            self?.updateIcon()
        }
    }

    private var waveFrame = 0
    private var waveTimer: Timer?
    private var lastEventId: String?
    private var lastTick = 0
    private var typeFrame = 0

    func updateIcon() {
        guard let button = statusItem.button else { return }
        let bars = model.statusBars
        let pose = model.pipPose
        if model.prefs.showsPip {
            if model.activityTick != lastTick { lastTick = model.activityTick; typeFrame &+= 1 }
            let frame = pose == .wave ? waveFrame : pose == .work ? typeFrame : 0
            let lv = model.isSample ? (session: nil, weekly: nil) : Pip.levels(model.snapshot?.limits ?? [])
            button.image = PipIcon.image(pose, frame: frame, session: lv.session, weekly: lv.weekly, stale: bars.stale)
        } else {
            button.image = StatusIcon.image(session: bars.session, weekly: bars.weekly, stale: bars.stale,
                                            waiting: model.hasWaiting, pulse: false)
        }
        if let e = model.pipEvent {
            // The line beside Pip: short, so it never pushes neighbours behind the notch.
            // The pose says what happened; the line says which one. Short, so it never pushes icons behind the notch.
            button.title = " " + Format.truncate(e.label, max: 14)
            button.font = .systemFont(ofSize: 12, weight: .medium)
            if e.id != lastEventId { lastEventId = e.id; if e.kind == .needsYou { startWave() } }
        } else if model.prefs.showActivityInMenuBar, let pct = bars.session {
            button.title = Format.percent((pct * 100).rounded())
            button.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        } else {
            button.title = ""
        }
        button.toolTip = model.pipEvent.map { "\($0.text). Click to \(actionWord($0)); right-click for the menu." }
            ?? PipIcon.description(pose)
        var parts = ["AgentPlay"]
        if let s = bars.session { parts.append("session \(Int(s * 100)) percent used") }
        if let w = bars.weekly { parts.append("weekly \(Int(w * 100)) percent used") }
        if model.hasWaiting { parts.append("a session needs you") }
        if let e = model.pipEvent { parts.append(e.text) }
        button.setAccessibilityLabel(parts.joined(separator: ", "))
    }

    func actionWord(_ e: PipEvent) -> String {
        switch e.kind {
        case .needsYou, .finished: "go to it"
        case .failed: "see where it failed"
        case .limit: "open your usage page"
        }
    }

    /// The wave moves for a few seconds when someone starts waiting, then holds the raised hand.
    private func startWave() {
        waveTimer?.invalidate()
        guard !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion,
              !ProcessInfo.processInfo.isLowPowerModeEnabled else { return }
        var ticks = 0
        waveTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] t in
            MainActor.assumeIsolated {
                guard let self else { t.invalidate(); return }
                ticks += 1
                self.waveFrame &+= 1
                self.updateIcon()
                if ticks >= 16 { t.invalidate(); self.waveTimer = nil; self.waveFrame = 0; self.updateIcon() }
            }
        }
    }

    // MARK: clicks

    @objc private func clicked() {
        let event = NSApp.currentEvent
        let right = event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true
        if !right, event?.modifierFlags.contains(.option) == true { model.openViewer(); return }
        if !right, let e = model.pipEvent { model.act(on: e); updateIcon(); return }
        openMenu()
    }

    /// Light or Dark as set in System Settings, whatever the menu bar or wallpaper looks like.
    static var systemAppearance: NSAppearance? {
        let dark = UserDefaults.standard.string(forKey: "AppleInterfaceStyle") == "Dark"
        return NSAppearance(named: dark ? .darkAqua : .aqua)
    }

    func openMenu() {
        guard let b = statusItem.button else { return }
        rebuild(now: nowOverride ?? Date())
        // Opened from the status button, a menu takes the menu bar's look, which on macOS 26 follows the wallpaper
        // (a light wallpaper gives a light menu in Dark Mode). Menus follow the system setting, as Apple's do.
        if nowOverride == nil { menu.appearance = Self.systemAppearance }
        b.highlight(true)
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: b.isFlipped ? b.bounds.height + 4 : -4), in: b)
        b.highlight(false)
    }

    // MARK: NSMenuDelegate

    func menuNeedsUpdate(_ menu: NSMenu) {}  // built in openMenu, before it shows

    func menuWillOpen(_ menu: NSMenu) {
        model.popoverOpen = true
    }

    func menuDidClose(_ menu: NSMenu) {
        model.popoverOpen = false
    }

    // MARK: building

    func rebuild(now: Date) {
        menu.removeAllItems()
        for e in model.menuEntries(now: now) { add(e, to: menu, now: now) }
    }

    private func add(_ e: MenuEntry, to menu: NSMenu, now: Date) {
        switch e {
        case .pip:
            menu.addItem(hosted(PipHeader(model: model)))

        case .header(let t):
            menu.addItem(.sectionHeader(title: t))

        case .message(let title, let detail):
            let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            i.isEnabled = false
            if let detail { setSubtitle(detail, on: i) }
            menu.addItem(i)

        case .provider(let p):
            menu.addItem(hosted(ProviderCard(provider: p.provider, fallback: p, model: model, now: now)))

        case .enableLimits:
            guard !model.prefs.usesClaudeAccount else { return }  // the card says what's wrong with it
            let editor = model.claudeEditorOnly != nil
            guard editor || !model.claudeBridgeOn else { return }  // bridge on, a terminal reply will fill it
            let i = item(editor ? "Show Usage from My Claude Account…" : "Turn On Live Limits…") { [model] in
                model.settingsPane = .claude
                Windows.show(.settings)
            }
            setSubtitle(editor ? "Your 5-hour and weekly usage, as claude.ai shows it" : "Show Claude's 5-hour and weekly usage", on: i)
            i.image = symbol("gauge.with.dots.needle.33percent")
            menu.addItem(i)

        case .claudeAccounts:
            guard let me = model.claudeIdentity else { return }
            let i = NSMenuItem(title: "Switch Claude Account", action: nil, keyEquivalent: "")
            i.image = symbol("person.2")
            let sub = NSMenu()
            sub.autoenablesItems = false
            for a in model.savedAccounts {
                let active = a.accountUuid == me.accountUuid
                let ai = item(a.title) { [model] in if !active { model.switchAccount(to: a) } }
                ai.state = active ? .on : .off
                var parts = [a.organization, a.seatTier?.capitalized].compactMap { $0 }
                if let s = a.lastSession, let w = a.lastWeekly {
                    parts.append("\(Format.percent(s)) session · \(Format.percent(w)) week"
                                 + (active ? "" : a.lastSeen.map { ", \(Format.relative($0, now: now))" } ?? ""))
                }
                setSubtitle(parts.joined(separator: " · "), on: ai)
                ai.isEnabled = !model.switching
                sub.addItem(ai)
            }
            if !model.savedAccounts.contains(where: { $0.accountUuid == me.accountUuid }) {
                let save = item("Save \(me.email ?? "This Account") for Switching") { [model] in model.saveCurrentAccount() }
                setSubtitle("Keeps its login in your Keychain, for one-click switching", on: save)
                sub.addItem(save)
            }
            sub.addItem(.separator())
            let add = item(model.addingAccount ? "Waiting for Sign-In…" : "Add Another Account…") { [model] in model.addClaudeAccount() }
            setSubtitle("Opens Claude's sign-in; AgentPlay saves the new account by itself", on: add)
            add.isEnabled = !model.addingAccount
            sub.addItem(add)
            sub.addItem(item("Manage Accounts…") { [model] in model.settingsPane = .account; Windows.show(.settings) })
            i.submenu = sub
            if let cur = model.savedAccounts.first(where: { $0.accountUuid == me.accountUuid }) { setSubtitle(cur.title, on: i) }
            menu.addItem(i)

        case .session(let s):
            menu.addItem(sessionItem(s))
            // Option swaps in the replay, like the system menus' alternate items.
            if s.pid != nil, s.openable {
                let alt = item("Open Replay") { [model] in model.openViewer(sessionId: s.sessionId, seq: s.seq) }
                alt.isAlternate = true
                alt.keyEquivalentModifierMask = [.option]
                alt.image = dot(s.dot)
                setSubtitle(s.detail, on: alt)
                menu.addItem(alt)
            }

        case .usage(let u):
            menu.addItem(hosted(UsageSummary(report: u)))

        case .usageDetails(let u):
            let i = NSMenuItem(title: "Usage Details", action: nil, keyEquivalent: "")
            i.image = symbol("chart.bar.xaxis")
            let sub = NSMenu()
            sub.autoenablesItems = false
            sub.addItem(hosted(UsageDetail(report: u)))
            i.submenu = sub
            menu.addItem(i)

        case .health(let ins):
            // One row; the five parts are one level down.
            let score = Int((ins.score ?? 0).rounded())
            let i = NSMenuItem(title: "Run Health  \(score) / 100", action: nil, keyEquivalent: "")
            var bits: [String] = []
            if let prev = ins.prevScore, abs((ins.score ?? 0) - prev) >= 0.5 {
                let d = (ins.score ?? 0) - prev
                bits.append("\(d > 0 ? "Up" : "Down") \(Format.points(abs(d)))")
            }
            let weak = ins.components.filter { ($0.points ?? 20) < 10 }.map(\.name)
            if !weak.isEmpty { bits.append("weakest: " + weak.prefix(2).joined(separator: ", ")) }
            setSubtitle(bits.isEmpty ? "Last \(ins.days) days" : bits.joined(separator: " · "), on: i)
            i.image = symbol("heart.text.square")
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(HealthCard(insights: ins)))
            m.addItem(.separator())
            m.addItem(item("Open Run Health in AgentPlay…") { [model] in model.openInsights() })
            i.submenu = m
            menu.addItem(i)

        case .fix(let f):
            let i = NSMenuItem(title: Plain.text(f.title), action: nil, keyEquivalent: "")
            // One line: what it's worth and where. The pattern is one level down.
            let sub = [f.points.map { "+\(Format.points($0)) points" }, Insights.componentName(f.component), f.repo].compactMap { $0 }
            setSubtitle(sub.joined(separator: " · "), on: i)
            i.image = symbol("wrench.and.screwdriver")
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(FixDetail(fix: f)))
            m.addItem(.separator())
            m.addItem(item(f.applicable ? "Preview and Apply in AgentPlay…" : "See It in AgentPlay…") { [model] in model.openInsights() })
            if let first = f.evidence.first {
                m.addItem(item("Open an Example Session") { [model] in model.openViewer(sessionId: first) })
            }
            m.addItem(item("Copy as Text") { [model] in
                model.copyText("\(Plain.text(f.title))\n\(Plain.text(f.pattern))\n\(Plain.text(f.effect))")
            })
            i.submenu = m
            menu.addItem(i)

        case .errors(let errs):
            for err in errs {
                let i = NSMenuItem(title: err.title, action: nil, keyEquivalent: "")
                setSubtitle(err.detail, on: i)
                i.image = symbol(err.isPattern ? "arrow.triangle.2.circlepath" : err.resolved ? "checkmark.circle" : "xmark.octagon")
                let m = NSMenu()
                m.autoenablesItems = false
                m.addItem(hosted(ErrorDetail(line: err, now: now)))
                m.addItem(.separator())
                if let l = err.learn {
                    // Every run it failed in, straight to the failing step.
                    for e in l.evidence.prefix(6) {
                        let ei = item(e.repo ?? "Session") { [model] in model.openViewer(sessionId: e.sessionId, seq: e.seq, errorsTab: true) }
                        setSubtitle("\(AgentNames.label(e.agentTool)) · \(Format.relative(e.at, now: now)) · step \(e.seq)", on: ei)
                        ei.image = symbol("play.circle")
                        m.addItem(ei)
                    }
                    m.addItem(.separator())
                    m.addItem(item("Hide This Pattern for Two Weeks") { [model] in model.hideLearn(l.patternKey) })
                } else {
                    let open = item("Open Replay at the Failure") { [model] in
                        model.openViewer(sessionId: err.sessionId, seq: err.seq, errorsTab: true)
                    }
                    open.isEnabled = err.sessionId != nil
                    m.addItem(open)
                    if let k = err.patternKey { m.addItem(item("Mute This Pattern") { [model] in model.mutePattern(k) }) }
                }
                i.submenu = m
                menu.addItem(i)
            }

        case .action(let a):
            let i = item(a.title, key: a.key) { [weak self] in self?.perform(a) }
            if a == .refresh, let u = model.lastUpdated, !model.isSample { setSubtitle("Updated \(Format.relative(u, now: now))", on: i) }
            if a == .leaveSample { setSubtitle("Everything below is made-up example data", on: i) }
            menu.addItem(i)

        case .separator:
            menu.addItem(.separator())
        }
    }

    private func sessionItem(_ s: SessionLine) -> NSMenuItem {
        let i = item(s.title) { [model] in
            if let pid = s.pid, TerminalFocus.bringForward(pid: pid) { return }
            if s.openable { model.openViewer(sessionId: s.sessionId, seq: s.seq) }
            else { model.toast = "Its replay appears after the next refresh." }
        }
        if let pid = s.pid, let app = TerminalFocus.app(for: pid)?.localizedName {
            i.toolTip = "Click to go to \(app). Option-click for the replay."
        }
        i.image = dot(s.dot)
        setSubtitle(s.detail, on: i)
        if i.toolTip == nil { i.toolTip = s.pid != nil ? "Option-click for the replay." : "Click to open the replay." }
        i.setAccessibilityLabel("\(s.title), \(dotLabel(s.dot))")
        return i
    }

    func perform(_ a: MenuAction) {
        switch a {
        case .leaveSample: model.leaveSample()
        case .refresh: model.scheduleRefresh(reason: .manual)
        case .openViewer, .showAllSessions: model.openViewer()
        case .settings: Windows.show(.settings)
        case .quit: NSApp.terminate(nil)
        }
    }

    // MARK: item helpers

    private func item(_ title: String, key: String = "", _ action: @escaping () -> Void) -> NSMenuItem {
        ClosureMenuItem(title: title, keyEquivalent: key, action: action)
    }

    private func hosted<V: View>(_ view: V) -> NSMenuItem {
        let host = MenuHostingView(rootView: AnyView(view.environmentObject(model)))
        host.sizeToContent(width: Menu.width)
        host.autoresizingMask = [.width]  // follow the menu when native rows make it wider
        let i = NSMenuItem()
        i.view = host
        i.isEnabled = false  // display-only: the arrow keys and the mouse pass over it
        return i
    }

    private func setSubtitle(_ text: String, on item: NSMenuItem) {
        if #available(macOS 14.4, *) { item.subtitle = text } else { item.toolTip = text }
    }

    private func symbol(_ name: String) -> NSImage? {
        NSImage(systemSymbolName: name, accessibilityDescription: nil)
    }

    private func dot(_ d: SessionLine.Dot) -> NSImage? {
        let color: NSColor
        switch d {
        case .waiting: color = .systemOrange
        case .working: color = .controlAccentColor
        case .idle, .recent, .ended: color = .secondaryLabelColor
        }
        let name = d == .recent || d == .ended ? "circle" : "circle.fill"
        let config = NSImage.SymbolConfiguration(pointSize: 8, weight: .regular)
            .applying(NSImage.SymbolConfiguration(paletteColors: [color]))
        let img = NSImage(systemSymbolName: name, accessibilityDescription: dotLabel(d))?.withSymbolConfiguration(config)
        // A fixed 16pt slot keeps titles aligned with the other icons in the menu.
        guard let img else { return nil }
        let slot = NSImage(size: NSSize(width: 16, height: 16), flipped: false) { r in
            img.draw(in: NSRect(x: (r.width - img.size.width) / 2, y: (r.height - img.size.height) / 2,
                                width: img.size.width, height: img.size.height))
            return true
        }
        slot.accessibilityDescription = dotLabel(d)
        return slot
    }

    private func dotLabel(_ d: SessionLine.Dot) -> String {
        switch d {
        case .working: "Working"
        case .waiting: "Needs you"
        case .idle: "Idle"
        case .recent: "Recently active"
        case .ended: "Ended"
        }
    }
}

final class ClosureMenuItem: NSMenuItem {
    private let closure: () -> Void
    init(title: String, keyEquivalent: String, action: @escaping () -> Void) {
        closure = action
        super.init(title: title, action: #selector(run), keyEquivalent: keyEquivalent)
        target = self
    }
    required init(coder: NSCoder) { fatalError("unused") }
    @objc private func run() { closure() }
}

/// NSMenu sizes custom rows from `intrinsicContentSize`, so the measured height goes there.
final class MenuHostingView: NSHostingView<AnyView> {
    private var measured: NSSize?
    override var intrinsicContentSize: NSSize { measured ?? super.intrinsicContentSize }

    func sizeToContent(width: CGFloat) {
        frame = NSRect(x: 0, y: 0, width: width, height: 10)
        layoutSubtreeIfNeeded()
        let h = ceil(fittingSize.height)
        measured = NSSize(width: width, height: h)
        frame = NSRect(x: 0, y: 0, width: width, height: h)
        invalidateIntrinsicContentSize()
    }
}

// MARK: - Display-only rows

/// One provider: name and plan, how fresh the numbers are, then each window as one line and a bar.
/// Reads the latest snapshot itself, so a refresh that lands while the menu is open shows up in place.
struct ProviderCard: View {
    let provider: String
    let fallback: ProviderSummary
    @ObservedObject var model: CompanionModel
    var now: Date

    var body: some View {
        let p = model.snapshot.flatMap { s in ProviderSummary.build(s.limits).first { $0.provider == provider } } ?? fallback
        VStack(alignment: .leading, spacing: 7) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(alignment: .firstTextBaseline) {
                    Text(p.label).font(.headline).foregroundStyle(Ink.primary)
                    Spacer()
                    if let plan = p.plan { Text(plan.capitalized).font(.subheadline).foregroundStyle(Ink.secondary) }
                }
                if p.provider == "claude-code", let who = model.claudeIdentity?.line {
                    // Which account these numbers belong to.
                    Text(who).font(.subheadline).foregroundStyle(Ink.secondary).lineLimit(1).truncationMode(.middle)
                }
                Text(status(p)).font(.subheadline).foregroundStyle(Ink.secondary).lineLimit(3).fixedSize(horizontal: false, vertical: true)
            }
            ForEach(p.windows) { w in MeterRow(window: w, now: now) }
            if let warning = paceWarning(p) {
                Text(warning).font(.subheadline).foregroundStyle(Color(nsColor: .systemOrange))
            }
            if let plans = p.conflictingPlans {
                Text("Recent sessions reported \(plans.map(\.capitalized).joined(separator: " and ")). They may be different accounts.")
                    .font(.subheadline).foregroundStyle(Ink.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, Menu.inset)
        .padding(.top, 5).padding(.bottom, 7)
        .frame(minWidth: Menu.width, maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    func status(_ p: ProviderSummary) -> String {
        if p.windows.isEmpty {
            if p.provider == "claude-code", model.prefs.usesClaudeAccount {
                return model.accountFailure?.message ?? "Asking your Claude account…"
            }
            if p.provider == "claude-code", let editor = model.claudeEditorOnly {
                // The statusline is the only local report of plan usage, and editor panels don't have one.
                return "Your sessions run in \(editor)'s agent panel, which doesn't report plan usage. "
                    + "Terminal Claude Code does."
            }
            if p.provider == "claude-code" && !model.claudeBridgeOn { return "Live limits are off" }
            if p.provider == "claude-code" { return "Appears after Claude Code's next reply in a terminal" }
            return MenuBuilder.shortReason(p)
        }
        if model.isSample { return "Sample data" }
        let seen = p.windows.compactMap(\.used.observedAt).max()
        if p.windows.allSatisfy({ MeterRow.isStale($0, now: now) }) {
            return "Stale · last report \(Format.relative(seen, now: now))"
        }
        var parts = ["Updated \(Format.relative(seen, now: now))"]
        if paceWarning(p) == nil, let w = p.window("weekly"), !MeterRow.isStale(w, now: now),
           MeterRow.pace(w, now: now) != nil {
            parts.append("on pace for the week")
        }
        return parts.joined(separator: " · ")
    }

    /// The one verdict worth colour: a window that runs out before it resets.
    func paceWarning(_ p: ProviderSummary) -> String? {
        for w in p.windows where !MeterRow.isStale(w, now: now) {
            guard let pace = MeterRow.pace(w, now: now), pace.level != .normal, let out = pace.runsOutAt else { continue }
            let name = w.window == "five_hour" ? "Session" : w.windowLabel
            return "\(name) runs out in \(Format.duration(out.timeIntervalSince(now))) at this pace"
        }
        return nil
    }
}

/// Pip at twice its menu-bar size, and one line about now. Updates in place while the menu is open.
struct PipHeader: View {
    @ObservedObject var model: CompanionModel

    var body: some View {
        let now = Date()
        let pose = model.pipPose
        let lv = Pip.levels(model.snapshot?.limits ?? [])
        let say = Pip.sentence(pose, live: model.snapshot?.live ?? [], limits: model.snapshot?.limits ?? [],
                               recent: model.pipRecent, now: now)
        HStack(spacing: 10) {
            Image(nsImage: PipIcon.image(pose, session: lv.session, weekly: lv.weekly))
                .renderingMode(.template).interpolation(.none).resizable()
                .frame(width: 40, height: 36)
                .foregroundStyle(Ink.primary)
                .accessibilityLabel(PipIcon.description(pose))
            VStack(alignment: .leading, spacing: 2) {
                Text(say.title).font(.headline).foregroundStyle(Ink.primary).lineLimit(2)
                if let d = say.detail { Text(d).font(.subheadline.monospacedDigit()).foregroundStyle(Ink.secondary) }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 6)
        .frame(minWidth: Menu.width, maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Insights

/// Strips the Markdown code ticks the health report uses around names.
enum Plain {
    static func text(_ s: String) -> String { s.replacingOccurrences(of: "`", with: "") }
}

/// Run health out of 100, its change from the 30 days before, and the five 20-point parts as short bars.
struct HealthCard: View {
    var insights: Insights

    var body: some View {
        let score = insights.score ?? 0
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("Run health").font(.headline).foregroundStyle(Ink.primary)
                Spacer()
                Text("\(Int(score.rounded()))").font(.title3.weight(.semibold).monospacedDigit()).foregroundStyle(Ink.primary)
                Text("/ 100").font(.subheadline).foregroundStyle(Ink.secondary)
            }
            if let prev = insights.prevScore {
                let d = score - prev
                Text(abs(d) < 0.5 ? "Same as the 30 days before" : "\(d > 0 ? "Up" : "Down") \(Format.points(abs(d))) from \(Int(prev.rounded())) the 30 days before")
                    .font(.subheadline)
                    .foregroundStyle(d <= -3 ? Color(nsColor: .systemOrange) : Ink.secondary)
            }
            ForEach(insights.components) { c in
                HStack(spacing: 8) {
                    Text(c.name).font(.subheadline).foregroundStyle(Ink.primary).frame(width: 118, alignment: .leading)
                    GeometryReader { g in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color(nsColor: .quaternaryLabelColor))
                            Capsule().fill(tint(c)).frame(width: max(3, g.size.width * min(1, (c.points ?? 0) / 20)))
                        }
                    }
                    .frame(height: 4)
                    Text(c.points.map { Format.points($0) } ?? "–").font(.subheadline.monospacedDigit())
                        .foregroundStyle(Ink.secondary).frame(width: 30, alignment: .trailing)
                }
            }
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 8)
        .frame(width: Menu.width, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    /// Weak parts stand out: under half their 20 points is orange.
    func tint(_ c: Insights.Component) -> Color {
        (c.points ?? 0) < 10 ? Color(nsColor: .systemOrange) : Color(nsColor: .controlAccentColor)
    }
}

/// A fix in full: what was seen, what fixing it changes, where, and what it's worth.
struct FixDetail: View {
    var fix: Insights.Fix

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(Plain.text(fix.title)).font(.headline).foregroundStyle(Ink.primary)
            Text(Plain.text(fix.pattern)).font(.body).foregroundStyle(Ink.primary)
            Text(Plain.text(fix.effect)).font(.body).foregroundStyle(Ink.secondary)
            Text([fix.points.map { "+\(Format.points($0)) points" }, Insights.componentName(fix.component),
                  fix.repo, "\(fix.sessions) session\(fix.sessions == 1 ? "" : "s")"].compactMap { $0 }.joined(separator: " · "))
                .font(.subheadline).foregroundStyle(Ink.secondary)
        }
        .fixedSize(horizontal: false, vertical: true)
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 8)
        .frame(width: Menu.width, alignment: .leading)
    }
}

/// An error in full: what failed, where, when, why, and whether it later passed.
struct ErrorDetail: View {
    var line: ErrorLine
    var now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let l = line.learn {
                Text(Plain.text(l.title)).font(.headline).foregroundStyle(Ink.primary)
                Text(l.denominator).font(.subheadline).foregroundStyle(Ink.secondary)
                Text(l.detail).font(.body).foregroundStyle(Ink.primary)
                Text("The runs it failed in:").font(.subheadline).foregroundStyle(Ink.secondary)
            } else if let a = line.alert {
                Text(line.title).font(.headline).foregroundStyle(Ink.primary)
                Text(rows(a).joined(separator: "\n")).font(.body).foregroundStyle(Ink.primary)
                Text(a.isResolved ? "It passed later in the same run." : "It hasn't passed since in that run.")
                    .font(.subheadline).foregroundStyle(a.isResolved ? Ink.secondary : Color(nsColor: .systemOrange))
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 8)
        .frame(width: Menu.width, alignment: .leading)
    }

    func rows(_ a: AlertCandidate) -> [String] {
        var r = ["\(AgentNames.label(a.agentTool))" + (line.repo.map { " in \($0)" } ?? "")]
        if let n = a.count { r.append("\(n) failures in a row") }
        r.append("Why: \(Format.errClass(a.errClass))")
        let first = a.firstAt ?? a.triggerAt
        r.append("Started \(Format.clock(first)), \(Format.relative(first, now: now))")
        return r
    }
}

/// "Session        34% · resets in 2h 18m" over a thin bar. Tint follows pace, not level.
struct MeterRow: View {
    var window: ProviderLimitWindow
    var now: Date

    static func isStale(_ w: ProviderLimitWindow, now: Date) -> Bool {
        w.used.freshness == .stale || (w.resetsAt.map { $0 <= now } ?? false) || w.used.isStale(now: now, maxAge: 6 * 3600)
    }

    static func pace(_ w: ProviderLimitWindow, now: Date) -> Pace? {
        Pace.of(used: w.used.value?.usedPercent ?? 0, windowMinutes: w.windowMinutes, resetsAt: w.resetsAt, now: now)
    }

    var body: some View {
        let stale = Self.isStale(window, now: now)
        let used = window.used.value?.usedPercent ?? 0
        let pace = Self.pace(window, now: now)
        let tint: PaceTint = stale ? .stale
            : pace.map { $0.level == .over ? .over : $0.level == .ahead ? .ahead : .normal } ?? .normal
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(.body).foregroundStyle(Ink.primary)
                Spacer()
                Text(detail(stale, used: used)).font(.subheadline.monospacedDigit()).foregroundStyle(Ink.secondary)
            }
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color(nsColor: .quaternaryLabelColor))
                    Capsule().fill(tint.color).frame(width: max(5, g.size.width * min(1, used / 100)))
                }
            }
            .frame(height: 5)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title), \(detail(stale, used: used))")
    }

    var title: String { window.window == "five_hour" ? "Session" : window.windowLabel }

    func detail(_ stale: Bool, used: Double) -> String {
        if stale { return "\(Format.percent(used)) · stale, updates when \(window.providerLabel) runs" }
        guard let r = window.resetsAt else { return Format.percent(used) }
        let left = r.timeIntervalSince(now)
        let reset = left < 86400 ? "resets in \(Format.duration(left))" : "resets \(Format.weekdayTime(r))"
        return "\(Format.percent(used)) · \(reset)"
    }
}

/// Label colours for SwiftUI inside NSMenu, where the hierarchical styles don't resolve.
enum Ink {
    static let primary = Color(nsColor: .controlTextColor)  // what native menu titles use
    static let secondary = Color(nsColor: .secondaryLabelColor)
    static let tertiary = Color(nsColor: .tertiaryLabelColor)
}

/// Today and the last 30 days, label left, value right.
struct UsageSummary: View {
    var report: UsageReport

    var body: some View {
        VStack(spacing: 3) {
            row("Today", report.today)
            row("Last 30 Days", report.last30Days)
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 3)
        .frame(minWidth: Menu.width, maxWidth: .infinity)
    }

    func row(_ title: String, _ p: UsagePeriod) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).foregroundStyle(Ink.primary)
            Spacer()
            Text(UsageText.period(p)).monospacedDigit().foregroundStyle(Ink.secondary)
        }
        .font(.body)
    }
}

enum UsageText {
    static func period(_ p: UsagePeriod) -> String {
        let tokens = "\(Format.tokens(p.tokens.total)) tokens"
        if p.requests > p.unpricedRequests { return "\(Format.money(p.costUsd)) · \(tokens)" }
        return tokens
    }

    static func slice(_ s: UsageSlice) -> String {
        s.isUnpriced ? "\(Format.tokens(s.tokens.total)) · unpriced" : "\(Format.money(s.costUsd)) · \(Format.tokens(s.tokens.total))"
    }
}

/// The Usage Details submenu: 14-day chart, then 7 and 30 days, by model and by project.
struct UsageDetail: View {
    var report: UsageReport

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Tokens per Day").font(.subheadline).foregroundStyle(Ink.secondary)
                Chart(report.daily.filter { $0.day != nil }) { d in
                    BarMark(x: .value("Day", d.day ?? .distantPast, unit: .day), y: .value("Tokens", d.tokens))
                        .foregroundStyle(Color.accentColor.opacity(d.id == report.daily.last?.id ? 1 : 0.45))
                        .cornerRadius(2)
                }
                .chartYAxis(.hidden)
                .chartXAxis {
                    AxisMarks(values: .stride(by: .day, count: 7)) { _ in
                        AxisValueLabel(format: .dateTime.month(.abbreviated).day())
                    }
                }
                .frame(height: 64)
                .accessibilityLabel("Tokens per day, last 14 days")
            }
            group(nil, [("Last 7 Days", UsageText.period(report.last7Days)),
                        ("Last 30 Days", UsageText.period(report.last30Days))])
            if let models = report.last7Days.byModel, !models.isEmpty {
                group("By Model · 7 Days", models.prefix(4).map { ($0.key, UsageText.slice($0)) })
            }
            if let repos = report.last7Days.byRepo, !repos.isEmpty {
                group("By Project · 7 Days", repos.prefix(4).map { ($0.key, UsageText.slice($0)) })
            }
            Text("Estimated from this Mac's sessions at API list prices.")
                .font(.footnote).foregroundStyle(Ink.tertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 8)
        .frame(minWidth: Menu.width, maxWidth: .infinity, alignment: .leading)
    }

    func group(_ title: String?, _ rows: [(String, String)]) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            if let title { Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Ink.secondary) }
            ForEach(rows, id: \.0) { r in
                HStack(alignment: .firstTextBaseline) {
                    Text(r.0).foregroundStyle(Ink.primary).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 12)
                    Text(r.1).monospacedDigit().foregroundStyle(Ink.secondary).lineLimit(1)
                }
                .font(.body)
            }
        }
    }
}

// MARK: - Terminal focus

/// From an agent's process to the app it runs in: walk up the parents until one is a regular app
/// (Terminal, iTerm, Ghostty, VS Code, Cursor…) and bring it forward. tmux and ssh sessions have no such app.
enum TerminalFocus {
    @MainActor
    static func bringForward(pid: Int32) -> Bool {
        guard let app = app(for: pid) else { return false }
        return app.activate(from: .current, options: [])  // cooperative activation, macOS 14
    }

    static func app(for pid: Int32) -> NSRunningApplication? {
        var p = pid
        for _ in 0..<16 {
            if let app = NSRunningApplication(processIdentifier: p), app.activationPolicy == .regular { return app }
            guard let parent = parent(of: p), parent > 1 else { return nil }
            p = parent
        }
        return nil
    }

    static func parent(of pid: Int32) -> Int32? {
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        guard sysctl(&mib, 4, &info, &size, nil, 0) == 0, size > 0 else { return nil }
        return info.kp_eproc.e_ppid
    }
}

// MARK: - Toast

/// A short confirmation under the status item ("Copied"), since the menu has already closed.
@MainActor
enum Toast {
    private static var panel: NSPanel?
    private static var hide: DispatchWorkItem?

    static func show(_ text: String, under button: NSStatusBarButton?) {
        panel?.orderOut(nil)
        let host = NSHostingView(rootView:
            Text(text).font(.callout).multilineTextAlignment(.leading)
                .frame(maxWidth: 280, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10, style: .continuous)))
        let size = host.fittingSize
        let p = NSPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.level = .statusBar
        p.appearance = StatusItemController.systemAppearance
        p.contentView = host
        if let b = button, let w = b.window {
            let r = w.convertToScreen(b.convert(b.bounds, to: nil))
            p.setFrameOrigin(NSPoint(x: r.midX - size.width / 2, y: r.minY - size.height - 6))
        }
        p.orderFrontRegardless()
        panel = p
        hide?.cancel()
        let work = DispatchWorkItem { p.orderOut(nil) }
        hide = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5, execute: work)
    }
}
