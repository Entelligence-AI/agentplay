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
    /// The app's own status item, for the first run's hand-off: the welcome window flies into it.
    static weak var current: StatusItemController?

    /// Where Pip sits on screen.
    var pipScreenRect: NSRect? {
        guard let b = statusItem.button, let w = b.window else { return nil }
        return w.convertToScreen(b.convert(b.bounds, to: nil))
    }
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
            // Pip looks up when the pointer comes over it. Events, not a timer: nothing runs while it's away.
            b.addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                             owner: self, userInfo: nil))
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
            let eyes: PipIcon.Eyes = model.pipPeek && pose != .sleep && pose != .limit ? .up : .pose
            button.image = PipIcon.image(pose, frame: frame, session: lv.session, weekly: lv.weekly, stale: bars.stale, eyes: eyes)
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
            ?? "\(PipIcon.description(pose)). Right-click for Settings and Quit."
        var parts = ["AgentPlay"]
        if let s = bars.session { parts.append("session \(Int(s * 100)) percent used") }
        if let w = bars.weekly { parts.append("weekly \(Int(w * 100)) percent used") }
        if model.hasWaiting { parts.append("a session needs you") }
        if let e = model.pipEvent { parts.append(e.text) }
        button.setAccessibilityLabel(parts.joined(separator: ", "))
    }

    static func label(_ a: Pip.Tip.Action) -> String {
        switch a {
        case .goTo: "Go to It"
        case .fix(let f): f.applicable ? "Apply This Fix…" : "Show Me the Fix…"
        case .usagePage: "Open My Usage Page"
        case .usage: "See the Breakdown in AgentPlay"
        case .insights: "Open Run Health…"
        case .none: ""
        }
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

    @objc func mouseEntered(with event: NSEvent) { model.pipPeek = true }
    @objc func mouseExited(with event: NSEvent) { model.pipPeek = false }

    // MARK: clicks

    @objc private func clicked() {
        let event = NSApp.currentEvent
        let right = event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true
        if !right, event?.modifierFlags.contains(.option) == true { model.openViewer(); return }
        if !right, let e = model.pipEvent { model.act(on: e); updateIcon(); return }
        // While the line beside Pip shows, a left click acts on it, so right-click is the way to the menu.
        if right { model.pipEvent != nil ? openMenu() : openContextMenu() } else { openMenu() }
    }

    /// Right-click: the app's own commands. The left-click menu is about the agents; this one is about AgentPlay.
    func openContextMenu() {
        guard let b = statusItem.button, let w = b.window else { return }
        let m = NSMenu()
        m.autoenablesItems = false
        if nowOverride == nil { m.appearance = Self.systemAppearance }
        let now = nowOverride ?? Date()
        for e in MenuBuilder.contextMenu(hasStore: model.snapshot?.store.available == true) { add(e, to: m, now: now) }
        let r = w.convertToScreen(b.convert(b.bounds, to: nil))
        b.highlight(true)
        startGlass()
        m.popUp(positioning: nil, at: NSPoint(x: r.minX, y: Self.menuTop(below: r, screen: w.screen)), in: nil)
        stopGlass()
        b.highlight(false)
    }

    /// Where the menu's first row goes. macOS draws the menu's rounded top padding (about 6 pt) above that
    /// point; if the padding would cross the usable screen edge, macOS pins the window and scrolls the content
    /// instead, which shows the arrow and hides the first line. Measured with `--film probe`.
    static func menuTop(below r: NSRect, screen: NSScreen?) -> CGFloat {
        let edge = (screen ?? NSScreen.main)?.visibleFrame.maxY ?? r.minY
        return min(r.minY - 4, edge - 8)
    }

    /// Light or Dark as set in System Settings, whatever the menu bar or wallpaper looks like.
    static var systemAppearance: NSAppearance? {
        let dark = UserDefaults.standard.string(forKey: "AppleInterfaceStyle") == "Dark"
        return NSAppearance(named: dark ? .darkAqua : .aqua)
    }

    func openMenu() {
        guard let b = statusItem.button, let w = b.window else { return }
        // Opened inside the status button, a menu takes the menu bar's glass, which on macOS 26 follows the
        // wallpaper: Dark Mode text on light glass, grey and hard to read. Opened at a screen point it takes the
        // app's look, which follows the system setting, as Apple's menus do.
        if nowOverride == nil { menu.appearance = Self.systemAppearance }
        let r = w.convertToScreen(b.convert(b.bounds, to: nil))
        let top = Self.menuTop(below: r, screen: w.screen)
        // Never taller than the screen: a menu that scrolls hides Pip under its arrow. Trim the least
        // important rows until it fits.
        let bottom = ((w.screen ?? NSScreen.main)?.visibleFrame.minY ?? 0) + 8
        room = top - bottom
        prepare(now: nowOverride ?? Date())
        b.highlight(true)
        menu.popUp(positioning: nil, at: NSPoint(x: r.minX, y: top), in: nil)
        b.highlight(false)
    }

    /// Fill the menu for the tab showing, at one width for every tab.
    func prepare(now: Date) {
        // One width for every tab, so switching never makes the menu jump sideways: the widest tab's.
        // Measured once per snapshot: building the other tabs costs tens of milliseconds.
        menu.minimumWidth = 0
        let key = "\(model.snapshot?.generatedAt.timeIntervalSince1970 ?? 0)|\(model.prefs.asksClaudeForSuggestions)|\(model.askingClaude)"
        var widest: CGFloat = 0
        if let w = widthCache, w.key == key {
            widest = w.width
        } else {
            let shown = model.menuTab
            for t in MenuTab.allCases {
                model.menuTab = t
                let probe = NSMenu()
                probe.autoenablesItems = false
                for e in model.menuEntries(now: now) { add(e, to: probe, now: now) }
                widest = max(widest, probe.size.width)
            }
            model.menuTab = shown
            widthCache = (key, widest)
        }
        fill(now: now)
        menu.minimumWidth = max(widest, menu.size.width)
    }
    private var widthCache: (key: String, width: CGFloat)?

    /// How tall the open menu may be: from its top to the bottom of the usable screen.
    private var room: CGFloat = .greatestFiniteMagnitude

    /// Build the menu for the current tab, trimming the least important rows until it fits. `fromTabs`: keep
    /// Pip and the tab strip in place (the strip is mid-click, and its pill slides), replace what's below.
    private func fill(now: Date, fromTabs: Bool = false) {
        // Keep trimming while a step still changes the menu: past MenuBuilder.maxTrim each step drops one
        // waiting row, so the count of steps depends on the data, not a constant.
        var previous: [MenuEntry]?
        for trim in 0...64 {
            let entries = model.menuEntries(now: now, trim: trim)
            if entries == previous { break }  // nothing left to drop
            if fromTabs, let keep = menu.items.firstIndex(where: { $0.view is TabHostingView }),
               let at = entries.firstIndex(where: { if case .tabs = $0 { true } else { false } }) {
                while menu.items.count > keep + 1 { menu.removeItem(at: keep + 1) }
                for e in entries.dropFirst(at + 1) { add(e, to: menu, now: now) }
                addHiddenCommands()
            } else {
                rebuild(entries, now: now)
            }
            if menu.size.height <= room { break }
            previous = entries
        }
    }

    /// A tab in the strip was clicked: switch in place, the menu stays open.
    func switchTab(_ t: MenuTab) {
        guard t != model.menuTab else { return }
        model.menuTab = t
        model.hop()
        // After this click's event: the strip handling it stays, everything below it is rebuilt.
        RunLoop.main.perform(inModes: [.eventTracking, .default, .modalPanel]) { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.fill(now: self.nowOverride ?? Date(), fromTabs: true)
            }
        }
    }

    // MARK: NSMenuDelegate

    func menuNeedsUpdate(_ menu: NSMenu) {}  // built in openMenu, before it shows

    func menuWillOpen(_ menu: NSMenu) {
        model.popoverOpen = true
        startGlass()
    }

    func menuDidClose(_ menu: NSMenu) {
        model.popoverOpen = false
        stopGlass()
    }

    /// The menu and each submenu draw on Liquid Glass that shows whatever is behind it; tint it solid so text
    /// reads the same over a white page or a busy photo. Submenus open later: look while it's open.
    private func startGlass() {
        glassTimer?.invalidate()
        let t = Timer(timeInterval: 0.03, repeats: true) { _ in MainActor.assumeIsolated { Self.tintMenuGlass() } }
        RunLoop.main.add(t, forMode: .eventTracking)
        RunLoop.main.add(t, forMode: .default)
        glassTimer = t
    }

    private func stopGlass() {
        glassTimer?.invalidate()
        glassTimer = nil
    }

    private var glassTimer: Timer?
    /// Off only for `--contrast-check`'s "before" captures.
    static var glassTintOn = true

    /// Close to the window background, light or dark, with a little of the glass still showing at the edges.
    static let glassTint = NSColor(name: nil) { ap in
        ap.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? NSColor(white: 0.12, alpha: 0.97) : NSColor(white: 0.975, alpha: 0.9)
    }

    /// Tints the glass of every open menu window this app shows. Public API on the system's own view; a no-op
    /// before macOS 26 (the older menu material is already near-opaque) and when Reduce Transparency is on.
    static func tintMenuGlass() {
        guard #available(macOS 26.0, *), glassTintOn else { return }
        for w in NSApp.windows where w.isVisible && w.className.contains("PopupMenuWindow") {
            guard let root = w.contentView?.superview ?? w.contentView else { continue }
            func walk(_ v: NSView) {
                if let g = v as? NSGlassEffectView {
                    if g.tintColor !== glassTint { g.tintColor = glassTint }
                    // Dark glass still lifts toward a bright page behind it: a solid backing inside the rim.
                    if let c = g.contentView {
                        let dark = g.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                        c.wantsLayer = true
                        c.layer?.backgroundColor = dark ? NSColor(white: 0.13, alpha: 1).cgColor : nil
                        c.layer?.cornerRadius = g.cornerRadius
                        c.layer?.cornerCurve = .continuous
                    }
                    return
                }
                v.subviews.forEach(walk)
            }
            walk(root)
        }
    }

    // MARK: building

    func rebuild(now: Date, trim: Int = 0) {
        rebuild(model.menuEntries(now: now, trim: trim), now: now)
    }

    func rebuild(_ entries: [MenuEntry], now: Date) {
        menu.removeAllItems()
        for e in entries { add(e, to: menu, now: now) }
        addHiddenCommands()
    }

    private func addHiddenCommands() {
        // The commands live on right-click; their shortcuts keep working here through hidden items.
        for a in [MenuAction.openViewer, .refresh, .settings, .quit] {
            let i = item(a.title, key: a.key) { [weak self] in self?.perform(a) }
            i.isHidden = true
            i.allowsKeyEquivalentWhenHidden = true
            menu.addItem(i)
        }
    }

    private func add(_ e: MenuEntry, to menu: NSMenu, now: Date) {
        switch e {
        case .pip:
            let host = ClickHostingView(rootView: AnyView(PipHeader(model: model).environmentObject(model)))
            host.onClick = { [weak self] in self?.model.nextTip() }
            host.sizeToContent(width: Menu.width)
            host.autoresizingMask = [.width]
            let h = NSMenuItem()
            h.view = host
            menu.addItem(h)

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
            sub.addItem(item("Manage Accounts…") { [model] in model.settingsPane = .claude; Windows.show(.settings) })
            i.submenu = sub
            menu.addItem(i)

        case .session(let s):
            let i = sessionItem(s)
            // Point at a session for what it's doing; the ways to reach it come first, since a row with a
            // submenu doesn't take a click of its own.
            let m = NSMenu()
            m.autoenablesItems = false
            // How it's going, then the one way in: its terminal. No replay site; the menu says it here.
            m.addItem(hosted(SessionDetail(line: s, now: now)))
            if let pid = s.pid, let app = TerminalFocus.app(for: pid)?.localizedName {
                m.addItem(.separator())
                let go = item("Go to \(app)") { _ = TerminalFocus.bringForward(pid: pid) }
                go.image = symbol("arrow.up.forward.app")
                m.addItem(go)
            }
            i.submenu = m
            menu.addItem(i)

        case .usage(let u):
            menu.addItem(hosted(UsageSummary(report: u)))

        case .usageDetails:
            let i = item("Usage Details") { [model] in model.showInsights(.usage) }
            i.image = symbol("chart.bar.xaxis")
            menu.addItem(i)

        case .health(let ins):
            let score = Int((ins.score ?? 0).rounded())
            let i = item("Run Health  \(score) / 100") { [model] in model.showInsights(.overview) }
            var sub = "The last 30 days"
            if let prev = ins.prevScore, abs((ins.score ?? 0) - prev) >= 0.5 {
                let d = (ins.score ?? 0) - prev
                sub = "\(d > 0 ? "Up" : "Down") \(Format.points(abs(d))) this month"
            }
            setSubtitle(sub, on: i)
            i.image = symbol("heart.text.square")
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(HealthCard(insights: ins)))
            m.addItem(.separator())
            m.addItem(item("Open in Insights…") { [model] in model.showInsights(.overview) })
            i.submenu = m
            menu.addItem(i)

        case .signals(let sig):
            let c = sig.current
            let i = item("How You Steer") { [model] in model.showInsights(.steering) }
            let bits = [c.correctionRate.map { "\(Format.percent($0)) corrected" }, c.frustrationRate.map { "\(Format.percent($0)) frustrated" }].compactMap { $0 }
            setSubtitle(bits.isEmpty ? "\(c.messages) messages you typed" : bits.joined(separator: " · "), on: i)
            i.image = symbol("steeringwheel")
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(SteeringCard(signals: sig)))
            m.addItem(.separator())
            m.addItem(item("Open in Insights…") { [model] in model.showInsights(.steering) })
            i.submenu = m
            menu.addItem(i)

        case .fix(let f):
            let count = model.snapshot?.insights?.fixes.count ?? 1
            let i = item(Plain.text(f.title)) { [model] in model.showInsights(.fixes) }
            let pts = f.points.flatMap { $0 >= 0.05 ? "+\(Format.points($0)) points" : nil }
            let part = model.snapshot?.insights?.components.first { $0.key == f.component }?.name
            setSubtitle([pts, part].compactMap { $0 }.joined(separator: " · "), on: i)
            i.image = symbol("wrench.and.screwdriver")
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(FixDetail(fix: f)))
            m.addItem(.separator())
            if f.applicable {
                let ap = item("Apply This Fix…") { [model] in model.applyFix(f) }
                setSubtitle("You see the exact lines first; undo any time", on: ap)
                ap.image = symbol("checkmark.circle.fill")
                m.addItem(ap)
            }
            m.addItem(item(count > 1 ? "All \(count) Fixes in Insights…" : "Open in Insights…") { [model] in model.showInsights(.fixes) })
            i.submenu = m
            menu.addItem(i)

        case .tabs(let n):
            let host = TabHostingView(rootView: AnyView(TabStrip(model: model, improveCount: n).environmentObject(model)))
            host.onClick = { [weak self, weak host] x in
                guard let self, let host else { return }
                if let t = TabStrip.tab(at: x, width: host.bounds.width) { self.switchTab(t) }
            }
            host.sizeToContent(width: Menu.width)
            host.autoresizingMask = [.width]
            let h = NSMenuItem()
            h.view = host
            menu.addItem(h)

        case .suggestion(let sg):
            let i = item(sg.title) { [model] in model.showInsights(.fixes) }
            setSubtitle(SuggestionCard.subtitle(sg), on: i)
            i.image = symbol(SuggestionCard.symbol(sg.kind))
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(SuggestionCard(suggestion: sg, coach: model.snapshot?.coach)))
            if let name = sg.name, sg.kind == "use-skill" {
                m.addItem(.separator())
                let copy = item("Copy /\(name)") { [model] in
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString("/\(name)", forType: .string)
                    model.toast = "Copied /\(name). Paste it into Claude Code."
                }
                setSubtitle("Paste it at the start of your next task", on: copy)
                copy.image = symbol("doc.on.doc")
                m.addItem(copy)
            }
            if sg.kind == "remove" {
                m.addItem(.separator())
                m.addItem(item("Show Skills Folder") { NSWorkspace.shared.open(Self.claudeFolder("skills")) })
            }
            m.addItem(.separator())
            m.addItem(item("Ask Claude Again") { [model] in model.askClaude(force: true) })
            i.submenu = m
            menu.addItem(i)

        case .askClaude(let asking):
            if asking {
                let i = NSMenuItem(title: "Asking Claude…", action: nil, keyEquivalent: "")
                i.isEnabled = false
                setSubtitle("A few seconds, on Haiku", on: i)
                i.image = symbol("sparkles")
                menu.addItem(i)
            } else {
                let i = item("Ask Claude for Suggestions") { [model] in model.prefs.claudeSuggestions = true }
                setSubtitle("Once a day on Haiku. Counts and skill names only", on: i)
                i.image = symbol("sparkles")
                menu.addItem(i)
            }

        case .setupItem(let it):
            let i = item(it.name) { [model] in model.showInsights(.setup) }
            setSubtitle(ImpactCard.subtitle(it), on: i)
            i.image = ImpactCard.flagImage(it) ?? symbol(SetupCard.symbol(it.kind))
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(ImpactCard(item: it)))
            if let recent = it.measured?.recent, !recent.isEmpty {
                m.addItem(.separator())
                m.addItem(.sectionHeader(title: "Your Latest Sessions with It"))
                for r in recent {
                    let uuid = r.sessionId.split(separator: ":").last.map(String.init) ?? r.sessionId
                    let ri = item([r.repo, r.startedAt.flatMap(SnapshotDecoder.parseISO).map { Format.relative($0, now: now) }]
                                    .compactMap { $0 }.joined(separator: " · ")) { [model] in
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString("claude --resume \(uuid)", forType: .string)
                        model.toast = "Copied claude --resume. Paste it in a terminal in that project."
                    }
                    setSubtitle("\(r.calls) call\(r.calls == 1 ? "" : "s")" + (r.failed > 0 ? ", \(r.failed) failed" : "") + " · click to copy resume", on: ri)
                    ri.image = symbol("arrow.uturn.backward.circle")
                    m.addItem(ri)
                }
            }
            if it.source == "claude.ai connector" {
                m.addItem(.separator())
                let c = item("Manage Connectors on claude.ai") { NSWorkspace.shared.open(URL(string: "https://claude.ai/settings/connectors")!) }
                c.image = symbol("safari")
                m.addItem(c)
            }
            m.addItem(.separator())
            m.addItem(item("Open in Insights…") { [model] in model.showInsights(.setup) })
            i.submenu = m
            menu.addItem(i)

        case .unused(let kind, let names):
            let i = NSMenuItem(title: "\(names.count) \(SetupCard.kindName(kind, plural: names.count != 1))", action: nil, keyEquivalent: "")
            setSubtitle(Format.names(names), on: i)
            i.image = symbol(SetupCard.symbol(kind))
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(UnusedCard(kind: kind, names: names)))
            if let folder = ["skill": "skills", "agent": "agents", "command": "commands"][kind] {
                m.addItem(.separator())
                let show = item("Show \(folder.capitalized) Folder") { NSWorkspace.shared.open(Self.claudeFolder(folder)) }
                setSubtitle("Move out what you don't need; Claude Code stops loading it", on: show)
                show.image = symbol("folder")
                m.addItem(show)
            }
            i.submenu = m
            menu.addItem(i)

        case .split(let sp):
            menu.addItem(hosted(SplitCard(split: sp)))

        case .hooks(let hooks):
            menu.addItem(hosted(HooksCard(hooks: hooks)))

        case .openInsights(let section, let title):
            let i = item(title) { [model] in model.showInsights(section) }
            i.image = symbol("macwindow")
            menu.addItem(i)

        case .applied(let ins):
            let all = ins.applied ?? []
            guard let first = all.first else { return }
            let i = item(all.count == 1 ? Plain.text(first.title ?? "1 change applied") : "\(all.count) changes applied") { [model] in model.showInsights(.applied) }
            let verdict: String = switch first.verdict {
            case "worked": "Worked"
            case "no_change": "No change yet"
            case "measuring": "Measuring, \(first.sessionsSince ?? 0) of 3 sessions"
            default: "Applied"
            }
            setSubtitle(verdict, on: i)
            i.image = symbol("checkmark.seal")
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(item("Open in Insights…") { [model] in model.showInsights(.applied) })
            m.addItem(item("Undo \(Plain.text(first.title ?? "this change"))") { [model] in model.undoApplied(first) })
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
        i.image = dot(s.dot)
        setSubtitle(s.detail, on: i)
        i.setAccessibilityLabel("\(s.title), \(dotLabel(s.dot))")
        return i
    }

    func perform(_ a: MenuAction) {
        switch a {
        case .leaveSample: model.leaveSample()
        case .refresh: model.scheduleRefresh(reason: .manual)
        case .openViewer, .showAllSessions: model.openViewer()
        case .settings: Windows.show(.settings)
        case .checkUpdates: Updater.shared.checkNow()
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

    static func claudeFolder(_ sub: String) -> URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/\(sub)")
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
class MenuHostingView: NSHostingView<AnyView> {
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
                if let line = status(p) {
                    Text(line).font(.subheadline).foregroundStyle(Ink.secondary).lineLimit(3).fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(p.windows) { w in
                let f = forecast(p, w)
                // With a forecast, the weekly line shows its reset; the forecast says when it really runs out.
                MeterRow(window: w, now: now, linear: f == nil, runsOut: f.map { $0.runsOutAt != nil })
                if let f, let r = w.resetsAt {
                    HStack(spacing: 5) {
                        if f.runsOutAt != nil {
                            Image(systemName: "exclamationmark.triangle.fill").font(.caption)
                                .foregroundStyle(Color(nsColor: .systemOrange))
                        }
                        Text(f.short(reset: r, now: now)).font(.subheadline)
                            .foregroundStyle(f.runsOutAt != nil ? Ink.primary : Ink.secondary)
                            .lineLimit(1).minimumScaleFactor(0.9)
                    }
                }
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

    /// Claude's weekly window, from your usual spend per weekday; nil for other windows or without history.
    func forecast(_ p: ProviderSummary, _ w: ProviderLimitWindow) -> Forecast? {
        guard p.provider == "claude-code", w.window == "weekly", !MeterRow.isStale(w, now: now),
              let days = model.snapshot?.usage?.claudeDaily, let used = w.used.value?.usedPercent else { return nil }
        return Forecast.of(used: used, windowMinutes: w.windowMinutes, resetsAt: w.resetsAt, days: days, now: now)
    }

    /// A line under the title only when there is something to say: why there are no numbers, that they are
    /// stale, or that they are old. Fresh numbers speak for themselves.
    func status(_ p: ProviderSummary) -> String? {
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
        if let seen, now.timeIntervalSince(seen) > 15 * 60 { return "Updated \(Format.relative(seen, now: now))" }
        return nil
    }

}

/// Pip at twice its menu-bar size, alive while the menu is open: it blinks, types and waves, hops when clicked,
/// and says one thing worth knowing. The animation lives only as long as this view, so nothing runs when closed.
struct PipHeader: View {
    @ObservedObject var model: CompanionModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.periodic(from: .now, by: reduceMotion ? 3600 : 0.25)) { t in
            let tick = Int(t.date.timeIntervalSinceReferenceDate * 4)
            content(tick: reduceMotion ? 0 : tick)
        }
    }

    func content(tick: Int) -> some View {
        let now = Date()
        let pose = model.pipPose
        let lv = Pip.levels(model.snapshot?.limits ?? [])
        let say = Pip.sentence(pose, live: model.snapshot?.live ?? [], limits: model.snapshot?.limits ?? [],
                               recent: model.pipRecent, now: now)
        // A blink every four seconds; typing and waving step twice a second.
        let blink = tick % 16 == 0 && pose != .sleep && pose != .limit
        let frame = (pose == .work || pose == .wave) ? tick / 2 : 0
        return HStack(alignment: .top, spacing: 10) {
            Image(nsImage: PipIcon.image(pose, frame: frame, session: lv.session, weekly: lv.weekly,
                                         eyes: blink ? .closed : .pose, hop: model.pipHop))
                .renderingMode(.template).interpolation(.none).resizable()
                .frame(width: 40, height: 36)
                .foregroundStyle(Ink.primary)
                .accessibilityLabel(PipIcon.description(pose))
                .help("Click me for another suggestion")
            VStack(alignment: .leading, spacing: 2) {
                Text(say.title).font(.headline).foregroundStyle(Ink.primary).lineLimit(1).minimumScaleFactor(0.85)
                Text(model.tipIndex > 0 ? Self.quip(model, n: model.tipIndex) : Self.now(model, now: now))
                    .font(.subheadline).foregroundStyle(Ink.secondary).lineLimit(1)
                    .id(model.tipIndex).transition(.opacity)
            }
            Spacer(minLength: 0)
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: model.tipIndex)
        .padding(.horizontal, Menu.inset)
        .padding(.top, 8).padding(.bottom, 6)
        .help("Click Pip")
        // One height for both lines; the menu measures rows once.
        .frame(minWidth: Menu.width, maxWidth: .infinity, minHeight: 54, maxHeight: 54, alignment: .leading)
    }

    /// The one thing worth knowing right now, in a few words.
    static func now(_ m: CompanionModel, now: Date) -> String {
        guard let s = m.snapshot else { return "Reading your sessions" }
        // A waiting session is Pip's title already; this line says the next thing worth knowing.
        // The week's forecast is on the plan card right below; Pip doesn't say it twice.
        if let n = s.insights?.fixes.count, n > 0 { return n == 1 ? "1 fix ready in Improve" : "\(n) fixes ready in Improve" }
        let busy = (s.live ?? []).filter { $0.status == .busy }.count
        return busy > 0 ? "Click me if you're bored" : "Nothing needs you"
    }

    /// Pip, clicked: something cheeky about your own numbers.
    static func quip(_ m: CompanionModel, n: Int) -> String {
        let s = m.snapshot
        var lines: [String] = []
        if let f = s?.usage?.signals?.current.frustrationRate, f >= 20 { lines.append("\(Format.percent(f)) of your messages are frustrated. Same.") }
        if let t = s?.usage?.today, t.costUsd > 0 { lines.append("\(Format.money(t.costUsd)) today. I won't tell finance.") }
        if let c = s?.usage?.signals?.current.correctionRate, c >= 5 { lines.append("You correct me 1 in \(Int((100 / c).rounded())). Rude, but fair.") }
        if let k = s?.usage?.last30Days.tokens.total, k > 0 { lines.append("\(Format.tokens(k)) tokens this month. I read every one.") }
        if let busy = s?.live?.filter({ $0.status == .busy }).count, busy > 1 { lines.append("\(busy) agents at once. I'm sweating for them.") }
        if let score = s?.insights?.score { lines.append("Run health \(Int(score.rounded())). We can do better. You can.") }
        lines.append("Boop.")
        return lines[(n - 1) % lines.count]
    }
}

/// A custom row that takes a click without closing the menu: Pip, in the header.
/// The tab strip: a click says where it landed, and the menu stays open.
final class TabHostingView: MenuHostingView {
    var onClick: ((CGFloat) -> Void)?
    override func mouseUp(with event: NSEvent) { onClick?(convert(event.locationInWindow, from: nil).x) }
    override func mouseDown(with event: NSEvent) {}
}

final class ClickHostingView: MenuHostingView {
    var onClick: (() -> Void)?
    override func mouseUp(with event: NSEvent) { onClick?() }
    override func mouseDown(with event: NSEvent) {}
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
            // A hover card: the five parts as bars, each out of 20. What each counts is in the Insights window.
            ForEach(insights.components) { c in
                HStack(alignment: .center, spacing: 8) {
                    Text(c.name).font(.subheadline).foregroundStyle(Ink.primary)
                    Spacer(minLength: 4)
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color(nsColor: .quaternaryLabelColor))
                        Capsule().fill(tint(c)).frame(width: max(3, 64 * min(1, (c.points ?? 0) / 20)))
                    }
                    .frame(width: 64, height: 4)
                    Text(c.points.map { Format.points($0) } ?? "–").font(.subheadline.monospacedDigit())
                        .foregroundStyle(Ink.secondary).frame(width: 30, alignment: .trailing)
                }
                .padding(.top, 4)
            }
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 8)
        .frame(width: Menu.width, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    /// What each part counts, as health.py computes it. Nothing here the core doesn't measure.
    static let meaning: [String: String] = [
        "shipping": "Sessions that edited code and ended in a PR",
        "verification": "Sessions that ran a check after the last edit",
        "efficiency": "Errors and retries per call, and cache hits",
        "context": "Compactions, and files re-read after one",
        "environment": "Denied, blocked or missing tools per call",
    ]

    /// Weak parts stand out: under half their 20 points is orange.
    func tint(_ c: Insights.Component) -> Color {
        (c.points ?? 0) < 10 ? Color(nsColor: .systemOrange) : Color(nsColor: .controlAccentColor)
    }
}

/// How you steer, as the row people stop at: corrections and frustration as two numbers with their trend and
/// a sparkline, then one plain sentence. Pointing at it opens the four rates in full.
struct SteeringCard: View {
    var signals: Signals

    var body: some View {
        let c = signals.current, p = signals.previous
        VStack(alignment: .leading, spacing: 6) {
            Text("Of \(c.messages) messages you typed, last 30 days").font(.subheadline).foregroundStyle(Ink.secondary)
            Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 10) {
                GridRow {
                    figure("corrected", c.correctionRate, p.correctionRate, \.correctionRate)
                    figure("frustrated", c.frustrationRate, p.frustrationRate, \.frustrationRate)
                }
                GridRow {
                    figure("interrupted", c.interruptRate, p.interruptRate, \.interruptRate)
                    figure("refused", c.refusalRate, p.refusalRate, \.refusalRate)
                }
            }
            Text(Self.sentence(signals)).font(.subheadline).foregroundStyle(Ink.primary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 8)
        .frame(width: Menu.width, alignment: .leading)
    }

    func figure(_ what: String, _ now: Double?, _ before: Double?, _ key: KeyPath<Signals.Day, Double?>) -> some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text(now.map { $0 < 1 && $0 > 0 ? String(format: "%.1f%%", $0) : Format.percent($0) } ?? "–")
                        .font(.system(size: 22, weight: .semibold, design: .rounded).monospacedDigit())
                        .foregroundStyle(Ink.primary)
                    if let now, let before, abs(now - before) >= 0.5 {
                        // Lower is better for both: up is orange.
                        Image(systemName: now > before ? "arrow.up.right" : "arrow.down.right")
                            .font(.caption.weight(.bold))
                            .foregroundStyle(now > before ? Color(nsColor: .systemOrange) : Color(nsColor: .systemGreen))
                    }
                }
                Text(what).font(.caption).foregroundStyle(Ink.secondary)
            }
            Chart(signals.daily.filter { $0.messages > 0 }) { d in
                LineMark(x: .value("Day", d.date), y: .value("Rate", d[keyPath: key] ?? 0))
                    .foregroundStyle(Color(nsColor: .controlAccentColor))
                    .interpolationMethod(.monotone)
            }
            .chartXAxis(.hidden).chartYAxis(.hidden)
            .frame(width: 56, height: 26)
            .accessibilityHidden(true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Two sentences from the rates: how often a message corrects the agent and what kind of correction it
    /// mostly is (a result that didn't hold points at verification), else which way frustration went.
    static func sentence(_ s: Signals) -> String {
        var parts: [String] = []
        let c = s.current
        if let nw = c.notWorking, c.corrections >= 5, Double(nw) / Double(c.corrections) >= 0.5 {
            parts.append("Most corrections say it still isn't working: the agent calls done too early.")
        } else if let r = c.correctionRate, r > 0 {
            let n = Int((100 / r).rounded())
            parts.append(n <= 1 ? "Nearly every message corrects the agent." : "1 in \(n) messages corrects the agent.")
        }
        return parts.isEmpty ? "Counted from what you type; no text is kept." : parts.joined(separator: " ")
    }
}

/// Words and symbols for the kinds of setup Claude Code loads.
enum SetupCard {
    static func kindName(_ k: String, plural: Bool = false) -> String {
        let one = ["skill": "skill", "agent": "subagent", "command": "command", "mcp": "MCP server", "hook": "hook"][k] ?? k
        return plural ? one + "s" : one
    }

    static func symbol(_ k: String) -> String {
        ["skill": "sparkle", "agent": "person.2", "command": "command", "mcp": "puzzlepiece.extension", "hook": "bolt.horizontal"][k] ?? "square.stack.3d.up"
    }
}

/// The menu's tabs: Now, Improve, Setup. A pill slides to the one showing; the menu stays open.
struct TabStrip: View {
    @ObservedObject var model: CompanionModel
    var improveCount: Int
    @Namespace private var ns
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Which tab a click at `x` (in the strip's own coordinates) lands on.
    static func tab(at x: CGFloat, width: CGFloat) -> MenuTab? {
        let inner = width - 2 * Menu.inset, rel = x - Menu.inset
        guard rel >= 0, rel < inner else { return nil }
        return MenuTab.allCases[min(MenuTab.allCases.count - 1, Int(rel / (inner / CGFloat(MenuTab.allCases.count))))]
    }

    var body: some View {
        HStack(spacing: 0) {
            ForEach(MenuTab.allCases, id: \.self) { t in
                let on = t == model.menuTab
                HStack(spacing: 5) {
                    Text(t.title).font(.subheadline.weight(on ? .semibold : .regular))
                    if t == .improve, improveCount > 0 {
                        Text("\(improveCount)").font(.caption2.weight(.bold).monospacedDigit())
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().fill(on ? Color.white : Color(nsColor: .controlAccentColor)))
                            .foregroundStyle(on ? Color(nsColor: .controlAccentColor) : Color.white)
                    }
                }
                .foregroundStyle(on ? Color.white : Ink.primary)
                .frame(maxWidth: .infinity, minHeight: 26, maxHeight: 26)
                .background { if on { Knob().matchedGeometryEffect(id: "knob", in: ns) } }
                .contentShape(Rectangle())
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
            }
        }
        .padding(2)
        .background(Capsule().fill(Ink.track))
        .animation(reduceMotion ? nil : .spring(response: 0.32, dampingFraction: 0.78), value: model.menuTab)
        .padding(.horizontal, Menu.inset)
        .padding(.top, 2).padding(.bottom, 8)
        .frame(minWidth: Menu.width, maxWidth: .infinity)
    }

    /// The selected tab: the accent colour, white text on it, as a selected sidebar row or button is.
    struct Knob: View {
        var body: some View {
            Capsule().fill(Color(nsColor: .controlAccentColor))
                .shadow(color: .black.opacity(0.22), radius: 2, y: 1)
        }
    }
}

/// One of Claude's suggestions, in full, and where it came from.
struct SuggestionCard: View {
    var suggestion: Coach.Suggestion
    var coach: Coach?

    /// A short line under the title; the reason is one level down, in full.
    static func subtitle(_ s: Coach.Suggestion) -> String {
        switch s.kind {
        case "use-skill": s.name.map { "A skill you have · /\($0)" } ?? "A skill you have"
        case "remove": "Lighter sessions"
        case "fix": "A fix"
        default: "A habit to try"
        }
    }

    static func symbol(_ kind: String) -> String {
        ["use-skill": "sparkle", "remove": "trash", "habit": "lightbulb", "fix": "wrench.and.screwdriver"][kind] ?? "sparkles"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(suggestion.title).font(.headline).foregroundStyle(Ink.primary).fixedSize(horizontal: false, vertical: true)
            Text(suggestion.why).font(.subheadline).foregroundStyle(Ink.primary).fixedSize(horizontal: false, vertical: true)
            Text(source).font(.caption).foregroundStyle(Ink.secondary).padding(.top, 2)
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 9)
        .frame(width: Menu.width, alignment: .leading)
    }

    var source: String {
        var bits = ["From Claude Haiku, on your plan"]
        if let at = coach?.generatedAt.flatMap(SnapshotDecoder.parseISO) { bits.append(Format.relative(at, now: Date())) }
        if let t = coach?.tokens { bits.append("\(Format.tokens(t)) tokens") }
        return bits.joined(separator: " · ")
    }
}

/// How one MCP server, skill or subagent performed, measured from every session's map: its own calls, what
/// they cost in context and time, the one thing to do about it, and how your sessions with it compare.
struct ImpactCard: View {
    var item: SetupReport.Item

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(alignment: .firstTextBaseline) {
                    Text(item.name).font(.headline).foregroundStyle(Ink.primary)
                    Spacer()
                    Text(Self.sourceLine(item)).font(.subheadline).foregroundStyle(Ink.secondary)
                }
                Text(usedLine).font(.subheadline).foregroundStyle(Ink.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let m = item.measured {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
                    row("Failed", m.failed == 0 ? "none" : "\(m.failed) of \(m.calls) (\(Format.percent(100 * Double(m.failed) / Double(max(1, m.calls)))))",
                        warn: m.advice.kind == "failing")
                    row("Into context", m.tokens == 0 ? "nothing" : "\(Format.tokens(m.tokens)) tokens · \(Format.tokens(m.tokens / max(1, m.sessions))) a session",
                        warn: m.advice.kind == "heavy")
                    if m.ms >= 60_000 { row("Waiting on it", Format.duration(Double(m.ms) / 1000), warn: false) }
                }
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: Self.adviceSymbol(m.advice.kind)).font(.subheadline)
                        .foregroundStyle(Self.adviceColor(m.advice.kind))
                    Text(m.advice.text).font(.subheadline.weight(.medium)).foregroundStyle(Ink.primary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.top, 2)
                if let c = m.compare { compare(c) }
            }
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 10)
        .frame(width: Menu.width + 40, alignment: .leading)
    }

    var usedLine: String {
        guard let m = item.measured else { return "Used \(item.uses)× in the last 30 days" }
        var s = "\(m.calls) call\(m.calls == 1 ? "" : "s") in \(m.sessions) session\(m.sessions == 1 ? "" : "s")"
        if m.yours != m.sessions { s += " (\(m.yours) yours, \(m.sessions - m.yours) automated)" }
        if let at = m.last.flatMap(SnapshotDecoder.parseISO) { s += " · last \(Format.relative(at, now: Date()))" }
        return s
    }

    @ViewBuilder func row(_ label: String, _ value: String, warn: Bool) -> some View {
        GridRow {
            Text(label).font(.subheadline).foregroundStyle(Ink.secondary)
            Text(value).font(.subheadline.monospacedDigit().weight(warn ? .semibold : .regular))
                .foregroundStyle(warn ? Color(nsColor: .systemOrange) : Ink.primary)
        }
    }

    @ViewBuilder func compare(_ c: SetupReport.Measured.Compare) -> some View {
        if c.verdict == "too few" {
            Text("Compared with your other sessions: \(c.with) of yours used it, too few to compare (needs 5).")
                .font(.caption).foregroundStyle(Ink.secondary).fixedSize(horizontal: false, vertical: true)
        } else {
            VStack(alignment: .leading, spacing: 3) {
                Text("Your \(c.with) sessions with it vs \(c.without) without").font(.caption.weight(.semibold)).foregroundStyle(Ink.secondary)
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 2) {
                    pair("Failed calls", c.errorRateWith.map { Format.percent($0) }, c.errorRateWithout.map { Format.percent($0) })
                    pair("Compactions", c.compactionsWith.map { String(format: "%.1f", $0) }, c.compactionsWithout.map { String(format: "%.1f", $0) })
                    pair("Cost", c.costWith.map { Format.money($0) }, c.costWithout.map { Format.money($0) })
                }
                Text("Goes with it; doesn't prove it caused it.").font(.caption).foregroundStyle(Ink.secondary)
            }
            .padding(.top, 2)
        }
    }

    @ViewBuilder func pair(_ label: String, _ a: String?, _ b: String?) -> some View {
        GridRow {
            Text(label).font(.caption).foregroundStyle(Ink.secondary)
            Text(a ?? "–").font(.caption.monospacedDigit()).foregroundStyle(Ink.primary).gridColumnAlignment(.trailing)
            Text("vs").font(.caption).foregroundStyle(Ink.secondary)
            Text(b ?? "–").font(.caption.monospacedDigit()).foregroundStyle(Ink.primary).gridColumnAlignment(.trailing)
        }
    }

    static func sourceLine(_ it: SetupReport.Item) -> String {
        let kind = SetupCard.kindName(it.kind)
        let k = kind.prefix(1).uppercased() + kind.dropFirst()
        return it.source == "you" ? k : "\(k), \(it.source)"
    }

    /// Under the row's name: what it is, how much it ran, and the one flag worth seeing.
    static func subtitle(_ it: SetupReport.Item) -> String {
        var bits = [it.kind == "mcp" ? "MCP" : SetupCard.kindName(it.kind), "\(Format.count(it.uses)) call\(it.uses == 1 ? "" : "s")"]
        if let m = it.measured {
            switch m.advice.kind {
            case "failing": bits.append("\(m.failed) failed")
            case "heavy": bits.append("\(Format.tokens(m.tokens / max(1, m.sessions)))/session")
            case "helps": bits.append("helps")
            default: break
            }
        }
        return bits.joined(separator: " · ")
    }

    static func adviceSymbol(_ k: String) -> String {
        ["failing": "xmark.octagon.fill", "heavy": "exclamationmark.triangle.fill", "helps": "checkmark.seal.fill",
         "watch": "eye.fill"][k] ?? "checkmark.circle"
    }

    static func adviceColor(_ k: String) -> Color {
        switch k {
        case "failing": Color(nsColor: .systemRed)
        case "heavy", "watch": Color(nsColor: .systemOrange)
        case "helps": Color(nsColor: .systemGreen)
        default: Ink.secondary
        }
    }

    /// Rows that need a look get the flag's own symbol, in colour, instead of the kind's.
    static func flagImage(_ it: SetupReport.Item) -> NSImage? {
        guard let k = it.measured?.advice.kind, ["failing", "heavy", "helps"].contains(k) else { return nil }
        let color: NSColor = k == "failing" ? .systemRed : k == "heavy" ? .systemOrange : .systemGreen
        return NSImage(systemSymbolName: adviceSymbol(k), accessibilityDescription: k)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(paletteColors: [color]))
    }
}

/// Whose sessions these are: yours, with two or more messages from you, and one-prompt runs from scripts.
struct SplitCard: View {
    var split: SetupReport.Split

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            row("Yours", split.yours, split.yoursCostUsd)
            row("Automated", split.automated, split.automatedCostUsd)
            Text("Automated: a single prompt from a script, a bot or claude -p.")
                .font(.caption).foregroundStyle(Ink.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 6)
        .frame(width: Menu.width, alignment: .leading)
    }

    func row(_ label: String, _ n: Int, _ cost: Double) -> some View {
        HStack {
            Text(label).font(.subheadline).foregroundStyle(Ink.primary)
            Spacer()
            Text("\(Format.count(n)) session\(n == 1 ? "" : "s") · \(Format.money(cost))")
                .font(.subheadline.monospacedDigit()).foregroundStyle(Ink.primary)
        }
    }
}

/// Each hook: the event it runs on and its script.
struct HooksCard: View {
    var hooks: [SetupReport.Item]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(hooks) { h in
                HStack {
                    Text(h.name).font(.subheadline).foregroundStyle(Ink.primary)
                    Spacer()
                    Text(h.detail ?? "").font(.subheadline.monospaced()).foregroundStyle(Ink.secondary)
                }
            }
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 6)
        .frame(width: Menu.width, alignment: .leading)
    }
}

/// What sits unused: every name, and why it matters.
struct UnusedCard: View {
    var kind: String
    var names: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Not used in 30 days").font(.caption.weight(.semibold)).foregroundStyle(Ink.secondary)
            Text(names.joined(separator: ", ")).font(.subheadline).foregroundStyle(Ink.primary).fixedSize(horizontal: false, vertical: true)
            if kind != "command" {
                Text("Each one's description loads into every session, used or not.")
                    .font(.caption).foregroundStyle(Ink.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 9)
        .frame(width: Menu.width, alignment: .leading)
    }
}

/// One live session at a glance: what it's doing, how heavy it is, what it has done so far.
struct SessionDetail: View {
    var line: SessionLine
    var now: Date

    var body: some View {
        let r = line.run, c = r?.counts
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                Text(status).font(.headline).foregroundStyle(line.dot == .waiting ? Color(nsColor: .systemOrange) : Ink.primary)
                Spacer()
                if let m = r?.models?.last { Text(Format.modelName(m)).font(.subheadline).foregroundStyle(Ink.secondary) }
            }
            if let doing { Text(doing).font(.subheadline).foregroundStyle(Ink.primary).lineLimit(2) }
            // Weight: money, tokens, and how full its context is.
            HStack(spacing: 18) {
                stat(c?.costUsd.flatMap { $0 > 0 ? Format.money($0) : nil } ?? "–", "spent")
                stat(c?.tokens.flatMap { $0 > 0 ? Format.tokens($0) : nil } ?? "–", "tokens")
                stat(r?.context.value?.percent.map { Format.percent($0) } ?? "–", "context",
                     warn: (r?.context.value?.percent ?? 0) >= 80)
            }
            if let made { Text(made).font(.caption).foregroundStyle(Ink.secondary) }
            if let mix { Text(mix).font(.caption).foregroundStyle(Ink.secondary) }
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 9)
        .frame(width: Menu.width, alignment: .leading)
    }

    func stat(_ v: String, _ label: String, warn: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(v).font(.system(size: 17, weight: .semibold, design: .rounded).monospacedDigit())
                .foregroundStyle(warn ? Color(nsColor: .systemOrange) : Ink.primary)
            Text(label).font(.caption).foregroundStyle(Ink.secondary)
        }
    }

    var status: String {
        let since = line.live?.statusSince.map { Format.duration(now.timeIntervalSince($0)) }
        let age = (line.live?.startedAt ?? line.run?.startedAt).map { Format.duration(now.timeIntervalSince($0)) }
        switch line.dot {
        case .waiting: return "Waiting on you" + (since.map { " · \($0)" } ?? "")
        case .working: return "Working" + (since.map { " · \($0)" } ?? "") + (age.map { ", open \($0)" } ?? "")
        default: return "Idle" + (since.map { " · \($0)" } ?? "")
        }
    }

    /// What it's doing right now, in words.
    var doing: String? {
        if let p = line.run?.pendingTool { return "Now: " + p.event.label }
        if let e = line.run?.lastEvent { return "Last: " + e.label }
        return nil
    }

    /// What it has produced: files, commits, PRs, turns, errors.
    var made: String? {
        guard let c = line.run?.counts else { return nil }
        var bits: [String] = []
        if let f = c.filesTouched, f > 0 { bits.append("\(f) file\(f == 1 ? "" : "s") touched") }
        if let k = c.commits, k > 0 { bits.append("\(k) commit\(k == 1 ? "" : "s")") }
        if let p = c.prsCreated, p > 0 { bits.append("\(p) PR\(p == 1 ? "" : "s")") }
        if let t = c.userTurns, t > 0 { bits.append("\(t) message\(t == 1 ? "" : "s") from you") }
        if let e = c.errors, e > 0 { bits.append("\(e) error\(e == 1 ? "" : "s")") }
        return bits.isEmpty ? nil : bits.joined(separator: " · ")
    }

    /// Where its effort goes, as the two biggest kinds of work.
    var mix: String? {
        guard let cats = line.run?.categories else { return nil }
        let names = ["read": "reading", "search": "searching", "edit": "editing", "exec": "running commands",
                     "verify": "testing", "mcp": "MCP tools", "subagent": "subagents", "web": "the web"]
        let total = names.keys.reduce(0) { $0 + (cats[$1] ?? 0) }
        guard total >= 5 else { return nil }
        let top = names.keys.map { ($0, cats[$0] ?? 0) }.filter { $0.1 > 0 }.sorted { $0.1 > $1.1 }.prefix(2)
        return "Mostly " + top.map { "\(names[$0.0]!) \(Int((100 * Double($0.1) / Double(total)).rounded()))%" }.joined(separator: ", ")
    }
}

/// Blume's signal rates, from the user's own messages: each against the 30 days before, with its trend.
struct SignalsCard: View {
    var signals: Signals

    var body: some View {
        let c = signals.current, p = signals.previous
        VStack(alignment: .leading, spacing: 8) {
            Text("How often you steer").font(.headline).foregroundStyle(Ink.primary)
            Text("\(c.messages) messages you typed in the last \(signals.days) days").font(.subheadline).foregroundStyle(Ink.secondary)
            row("Corrections", Self.correctionKinds(c), c.correctionRate, p.correctionRate, \.correctionRate)
            row("Frustration", Self.frustrationKinds(c), c.frustrationRate, p.frustrationRate, \.frustrationRate)
            row("Interruptions", "of your messages stop the agent mid-step", c.interruptRate, p.interruptRate, \.interruptRate)
            row("Refusals", "of tool calls you said no to", c.refusalRate, p.refusalRate, \.refusalRate)
            Text("Counted from what you type in Claude Code; prompts a program sends don't count. No message text is kept.")
                .font(.footnote).foregroundStyle(Ink.tertiary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 8)
        .frame(width: Menu.width, alignment: .leading)
    }

    /// Under 1%, one decimal: refusals are rare, and "0%" would hide them.
    func small(_ v: Double) -> String { v < 1 && v > 0 ? String(format: "%.1f%%", v) : Format.percent(v) }

    /// What the corrections were: a result that didn't hold, or a change of direction.
    static func correctionKinds(_ c: Signals.Rates) -> String {
        guard let nw = c.notWorking, c.corrections > 0 else { return "of your messages turn the agent around" }
        return "of your messages turn the agent around; \(nw) of \(c.corrections) said it still isn't working"
    }

    static func frustrationKinds(_ c: Signals.Rates) -> String {
        guard let sw = c.swearing, let all = c.frustrated, all > 0 else { return "of your messages swear or pile up ! and ?" }
        return "of your messages swear or pile up ! and ?; \(sw) swore, \(all - sw) piled up punctuation"
    }

    func row(_ name: String, _ what: String, _ now: Double?, _ before: Double?, _ key: KeyPath<Signals.Day, Double?>) -> some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(name).font(.body.weight(.medium)).foregroundStyle(Ink.primary)
                    Text(now.map { small($0) } ?? "–").font(.body.monospacedDigit()).foregroundStyle(Ink.primary)
                    if let now, let before {
                        let d = now - before
                        // Each of these is better lower: up is orange.
                        Text(abs(d) < 0.05 ? "same" : "\(d > 0 ? "↑" : "↓") from \(small(before))")
                            .font(.subheadline.monospacedDigit())
                            .foregroundStyle(d >= 0.5 ? Color(nsColor: .systemOrange) : Ink.secondary)
                    }
                }
                Text(what).font(.caption).foregroundStyle(Ink.secondary)
            }
            Spacer(minLength: 4)
            Chart(signals.daily.filter { $0.messages > 0 }) { d in
                LineMark(x: .value("Day", d.date), y: .value("Rate", d[keyPath: key] ?? 0))
                    .foregroundStyle(Color(nsColor: .controlAccentColor))
                    .interpolationMethod(.monotone)
            }
            .chartXAxis(.hidden).chartYAxis(.hidden)
            .frame(width: 70, height: 22)
            .accessibilityHidden(true)
        }
    }
}

/// A fix as three short labelled lines: what happened, why, what the fix gets you. The way to do it is the
/// first row under the card, so nobody has to work it out from the text.
struct FixDetail: View {
    var fix: Insights.Fix

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(Plain.text(fix.title)).font(.headline).foregroundStyle(Ink.primary)
                Spacer()
                if let p = fix.points { Text("+\(Format.points(p))").font(.headline.monospacedDigit()).foregroundStyle(Color(nsColor: .systemGreen)) }
            }
            labelled("What happened", Plain.text(fix.pattern), Ink.primary)
            if let cause = fix.hook?.cause { labelled("Why", cause, Color(nsColor: .systemOrange)) }
            labelled("After the fix", Plain.text(fix.effect), Ink.primary)
            Text([Insights.componentName(fix.component), fix.repo, "seen in \(fix.sessions) session\(fix.sessions == 1 ? "" : "s")"]
                .compactMap { $0 }.joined(separator: " · "))
                .font(.caption).foregroundStyle(Ink.tertiary)
        }
        .fixedSize(horizontal: false, vertical: true)
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 8)
        .frame(width: Menu.width, alignment: .leading)
    }

    func labelled(_ label: String, _ text: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label).font(.caption.weight(.semibold)).foregroundStyle(Ink.tertiary)
            Text(text).font(.subheadline).foregroundStyle(color)
        }
    }

    /// A paste-ready prompt carrying the whole case, so a Claude Code session can make the change.
    static func prompt(_ f: Insights.Fix) -> String {
        var lines = ["Please fix this in my coding-agent setup, then show me the diff. Don't change anything else.", ""]
        lines.append("Problem: \(Plain.text(f.pattern))")
        if let cause = f.hook?.cause { lines.append("Cause: \(cause)") }
        if let h = f.hook {
            if !h.commands.isEmpty { lines.append("The hook currently runs: " + h.commands.joined(separator: " ; ")) }
            if !h.settingsFiles.isEmpty { lines.append("Configured in: " + h.settingsFiles.joined(separator: ", ")) }
        }
        if let repo = f.repo { lines.append("Project: \(repo)") }
        lines.append("What fixing it should change: \(Plain.text(f.effect))")
        return lines.joined(separator: "\n")
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
    /// Show the straight-line "out in …" on this line (off when a forecast line follows).
    var linear: Bool = true
    /// The forecast's answer, when there is one: it decides the bar's colour, not the straight line.
    var runsOut: Bool? = nil

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
        // One warning colour: orange when it's on course to run out before it resets, red only near the end.
        let ahead = runsOut ?? pace.map { $0.level != .normal } ?? false
        let tint: PaceTint = stale ? .stale : used >= 90 ? .over : ahead ? .ahead : .normal
        // One line: the name, the bar, the number. Two of these are a plan card; they must not stack up.
        HStack(alignment: .center, spacing: 10) {
            Text(title).font(.subheadline).foregroundStyle(Ink.primary).frame(width: 54, alignment: .leading)
            // A fixed-width bar, so session and week compare at a glance whatever the text says.
            ZStack(alignment: .leading) {
                Capsule().fill(Color(nsColor: .quaternaryLabelColor))
                Capsule().fill(tint.color).frame(width: max(5, Self.barWidth * min(1, used / 100)))
            }
            .frame(width: Self.barWidth, height: 6)
            Text(detail(stale, used: used)).font(.subheadline.monospacedDigit())
                .foregroundStyle(tint == .over ? tint.color : Ink.primary)
                .lineLimit(1)
            Spacer(minLength: 0)
        }
        .frame(height: 18)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title), \(detail(stale, used: used))")
    }

    static let barWidth: CGFloat = 72

    /// "Tue", or "tomorrow": short enough to sit beside the bar; the exact time is in the forecast line's tooltip.
    static func weekday(_ d: Date) -> String {
        if Calendar.current.isDateInTomorrow(d) { return "tomorrow" }
        let f = DateFormatter(); f.dateFormat = "EEEE"
        return f.string(from: d)
    }

    /// "Tue 1:29": a weekday and a time, short enough to sit beside a bar.
    static func shortDay(_ d: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "EEE"
        let t = DateFormatter()
        t.timeStyle = .short
        let time = t.string(from: d).replacingOccurrences(of: ":00", with: "").replacingOccurrences(of: " ", with: "").lowercased()
        return "\(f.string(from: d)) \(time)"
    }
    var title: String { window.window == "five_hour" ? "Session" : window.windowLabel }

    func detail(_ stale: Bool, used: Double) -> String {
        if stale { return "\(Format.percent(used)) · stale, updates when \(window.providerLabel) runs" }
        // Ahead of pace: the time it runs out replaces the reset time, in the bar's own orange or red.
        if linear, let pace = Self.pace(window, now: now), pace.level != .normal, let out = pace.runsOutAt {
            return "\(Format.percent(used)) · out in \(Format.duration(out.timeIntervalSince(now)))"
        }
        guard let r = window.resetsAt else { return Format.percent(used) }
        let left = r.timeIntervalSince(now)
        let reset = left < 86400 ? "resets \(Format.duration(left))" : "resets \(Self.weekday(r))"
        return "\(Format.percent(used)) · \(reset)"
    }
}

/// Label colours for SwiftUI inside NSMenu, where the hierarchical styles don't resolve.
enum Ink {
    static let primary = Color(nsColor: .controlTextColor)  // what native menu titles use
    // Stronger than the window defaults (55% and 25%): menu glass shows what's behind it. Resolved per appearance
    // when drawn; `withAlphaComponent` on a system colour would freeze it at launch.
    static let secondary = Color(nsColor: ink(0.75))
    static let tertiary = Color(nsColor: ink(0.58))
    /// The tab strip's track and its raised knob.
    static let track = Color(nsColor: NSColor(name: nil) { ap in
        ap.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? NSColor(white: 1, alpha: 0.10) : NSColor(white: 0, alpha: 0.07)
    })
    static let knob = Color(nsColor: NSColor(name: nil) { ap in
        ap.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? NSColor(white: 1, alpha: 0.24) : NSColor(white: 1, alpha: 0.95)
    })

    private static func ink(_ alpha: CGFloat) -> NSColor {
        NSColor(name: nil) { ap in
            ap.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? NSColor(white: 1, alpha: alpha) : NSColor(white: 0, alpha: alpha - 0.05)
        }
    }
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

    private static var controller: NSHostingController<AnyView>?

    static func show(_ text: String, under button: NSStatusBarButton?) {
        panel?.orderOut(nil)
        let vc = NSHostingController(rootView: AnyView(
            Text(text).font(.callout).multilineTextAlignment(.leading)
                .frame(maxWidth: 280, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10, style: .continuous))))
        // sizeThatFits measures the view; a hosting view's fittingSize reports several hundred points here.
        let size = vc.sizeThatFits(in: NSSize(width: 304, height: 400))
        vc.sizingOptions = []   // or the hosting view's own intrinsic size (the same bad number) would grow the panel
        controller = vc
        let host = vc.view
        host.frame = NSRect(origin: .zero, size: size)
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
