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
        m.popUp(positioning: nil, at: NSPoint(x: r.minX, y: min(r.minY - 4, (w.screen ?? NSScreen.main)?.visibleFrame.maxY ?? r.minY - 4)), in: nil)
        b.highlight(false)
    }

    /// Two short bars, session over week, filled as far as each is used and tinted by pace: the row's icon.
    static func gauge(_ p: ProviderSummary, now: Date) -> NSImage {
        let img = NSImage(size: NSSize(width: 18, height: 14), flipped: true) { _ in
            for (row, name) in [(0, "five_hour"), (1, "weekly")] {
                let y = CGFloat(row) * 7 + 2.5
                NSColor.tertiaryLabelColor.setFill()
                NSBezierPath(roundedRect: NSRect(x: 0, y: y, width: 18, height: 3.5), xRadius: 1.75, yRadius: 1.75).fill()
                guard let w = p.window(name), let used = w.used.value?.usedPercent else { continue }
                let stale = MeterRow.isStale(w, now: now)
                let level = MeterRow.pace(w, now: now)?.level
                let tint: NSColor = stale ? .tertiaryLabelColor : level == .over ? .systemRed : level == .ahead ? .systemOrange : .controlAccentColor
                tint.setFill()
                NSBezierPath(roundedRect: NSRect(x: 0, y: y, width: max(3.5, 18 * min(1, used / 100)), height: 3.5), xRadius: 1.75, yRadius: 1.75).fill()
            }
            return true
        }
        return img
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
        // Never above the usable area: the button reaches into the menu bar, and a menu whose top edge starts
        // even a point inside it gets a scroll arrow and scrolls on hover.
        let top = min(r.minY - 4, (w.screen ?? NSScreen.main)?.visibleFrame.maxY ?? r.minY - 4)
        // Never taller than the screen: a menu that scrolls hides Pip under its arrow. Trim the least
        // important rows until it fits.
        let bottom = ((w.screen ?? NSScreen.main)?.visibleFrame.minY ?? 0) + 8
        let room = top - bottom
        // Keep trimming while a step still changes the menu: past MenuBuilder.maxTrim each step drops one
        // waiting row, so the count of steps depends on the data, not a constant.
        let now = nowOverride ?? Date()
        var previous: [MenuEntry]?
        for trim in 0...64 {
            let entries = model.menuEntries(now: now, trim: trim)
            if entries == previous { break }  // nothing left to drop
            rebuild(entries, now: now)
            if menu.size.height <= room { break }
            previous = entries
        }
        b.highlight(true)
        menu.popUp(positioning: nil, at: NSPoint(x: r.minX, y: top), in: nil)
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

    func rebuild(now: Date, trim: Int = 0) {
        rebuild(model.menuEntries(now: now, trim: trim), now: now)
    }

    func rebuild(_ entries: [MenuEntry], now: Date) {
        menu.removeAllItems()
        for e in entries { add(e, to: menu, now: now) }
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
            // A glance row like the others: the name, a two-bar gauge, two percents. The card is one level down.
            let i = NSMenuItem(title: p.label, action: nil, keyEquivalent: "")
            i.image = Self.gauge(p, now: now)
            let pct = { (w: ProviderLimitWindow?) in w?.used.value.map { Format.percent($0.usedPercent) } }
            let bits = [pct(p.window("five_hour")).map { "\($0) session" }, pct(p.window("weekly")).map { "\($0) week" }].compactMap { $0 }
            setSubtitle(bits.isEmpty ? (p.provider == "claude-code" ? "Limits are off" : MenuBuilder.shortReason(p)) : bits.joined(separator: " · "), on: i)
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(ProviderCard(provider: p.provider, fallback: p, model: model, now: now)))
            if p.provider == "claude-code" {
                if p.windows.isEmpty { add(.enableLimits(provider: p.provider), to: m, now: now) }
                m.addItem(.separator())
                add(.claudeAccounts, to: m, now: now)
            }
            i.submenu = m
            menu.addItem(i)

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
            menu.addItem(i)

        case .session(let s):
            let i = sessionItem(s)
            // Point at a session for what it's doing; the ways to reach it come first, since a row with a
            // submenu doesn't take a click of its own.
            let m = NSMenu()
            m.autoenablesItems = false
            let state = SessionDetail(line: s, now: now)
            let context = s.run?.context.value?.percent.flatMap { $0 >= 70 ? "context \(Format.percent($0)) full" : nil }
            let glance = [state.status, context].compactMap { $0 }.joined(separator: " · ")
            if let pid = s.pid, let app = TerminalFocus.app(for: pid)?.localizedName {
                let go = item("Go to \(app)") { _ = TerminalFocus.bringForward(pid: pid) }
                go.image = symbol("arrow.up.forward.app")
                setSubtitle(glance, on: go)
                m.addItem(go)
            }
            let replay = item("Open Replay") { [model] in model.openViewer(sessionId: s.sessionId, seq: s.seq) }
            replay.image = symbol("play.circle")
            replay.isEnabled = s.openable
            setSubtitle(s.openable ? "See every step it took" : "Appears after the next refresh", on: replay)
            m.addItem(replay)
            i.submenu = m
            menu.addItem(i)

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
            // The lever first: what the fixes below are worth together. Then the trend.
            var bits: [String] = []
            let gain = ins.fixes.compactMap(\.points).reduce(0, +)
            if gain >= 0.5 {
                bits.append("\(Int((min(100, (ins.score ?? 0) + gain)).rounded())) within reach from \(ins.fixes.count) fix\(ins.fixes.count == 1 ? "" : "es")")
            }
            if let prev = ins.prevScore, abs((ins.score ?? 0) - prev) >= 0.5 {
                let d = (ins.score ?? 0) - prev
                bits.append("\(d > 0 ? "up" : "down") \(Format.points(abs(d)))")
            }
            if bits.isEmpty {
                let weak = ins.components.filter { ($0.points ?? 20) < 10 }.map(\.name)
                if !weak.isEmpty { bits.append("weakest: " + weak.prefix(2).joined(separator: ", ")) }
            }
            setSubtitle(bits.isEmpty ? "Last \(ins.days) days" : bits.joined(separator: " · "), on: i)
            i.image = symbol("heart.text.square")
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(HealthCard(insights: ins)))
            if let applied = ins.applied, !applied.isEmpty {
                m.addItem(.separator())
                m.addItem(.sectionHeader(title: "Applied by AgentPlay"))
                for e in applied {
                    let ai = item("Undo \(Plain.text(e.title ?? "this change"))") { [model] in model.undoApplied(e) }
                    setSubtitle([e.repo, e.file, e.source == "autotune" ? "by Autotune" : nil].compactMap { $0 }.joined(separator: " · "), on: ai)
                    ai.image = symbol("arrow.uturn.backward")
                    m.addItem(ai)
                }
            }
            if let pending = ins.autotune?.pending, !pending.isEmpty {
                m.addItem(.separator())
                m.addItem(.sectionHeader(title: "Autotune Is Holding"))
                for p in pending {
                    let pi = NSMenuItem(title: Plain.text(p.title), action: nil, keyEquivalent: "")
                    setSubtitle([p.points.map { "+\(Format.points($0)) points" }, p.repo].compactMap { $0 }.joined(separator: " · "), on: pi)
                    let pm = NSMenu()
                    pm.addItem(item("Approve and Apply") { [model] in model.autotune("approve", p) })
                    pm.addItem(item("Dismiss") { [model] in model.autotune("dismiss", p) })
                    pi.submenu = pm
                    m.addItem(pi)
                }
            }
            m.addItem(.separator())
            m.addItem(item("Open Run Health in AgentPlay…") { [model] in model.openInsights() })
            i.submenu = m
            menu.addItem(i)

        case .signals(let sig):
            // One row, like Run Health: the two numbers people react to in its subtitle; the card with its
            // trend lines, the sentence and all four rates one level down.
            let c = sig.current
            let i = NSMenuItem(title: "How You Steer", action: nil, keyEquivalent: "")
            let bits = [c.correctionRate.map { "\(Format.percent($0)) corrected" }, c.frustrationRate.map { "\(Format.percent($0)) frustrated" }].compactMap { $0 }
            setSubtitle(bits.isEmpty ? "\(c.messages) messages you typed" : bits.joined(separator: " · "), on: i)
            i.image = symbol("steeringwheel")
            let m = NSMenu()
            m.autoenablesItems = false
            m.addItem(hosted(SteeringCard(signals: sig)))
            m.addItem(.separator())
            m.addItem(item("Open Insights in AgentPlay…") { [model] in model.openInsights(page: "overview") })
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
            // The way to get it done comes first. Apply when AgentPlay can write the change itself; otherwise a
            // prompt that hands the whole case to Claude Code.
            if f.applicable {
                let ap = item("Apply This Fix…") { [model] in model.applyFix(f) }
                setSubtitle(f.hook?.remedy ?? "You see the exact lines first; undo any time", on: ap)
                ap.image = symbol("checkmark.circle.fill")
                m.addItem(ap)
            }
            let prompt = item("Copy a Prompt for Claude Code") { [model] in model.copyText(FixDetail.prompt(f)) }
            setSubtitle("Paste it into a session; it makes the change and shows the diff", on: prompt)
            prompt.image = symbol("text.badge.plus")
            m.addItem(prompt)
            if let file = f.hook?.settingsFiles.first {
                let name = (file as NSString).lastPathComponent
                let open = item("Open \(name)") { NSWorkspace.shared.open(URL(fileURLWithPath: (file as NSString).expandingTildeInPath)) }
                open.image = symbol("doc.text")
                m.addItem(open)
                let reveal = item("Show \(name) in Finder") { [model] in model.reveal(file) }
                reveal.isAlternate = true
                reveal.keyEquivalentModifierMask = [.option]
                m.addItem(reveal)
            }
            m.addItem(.separator())
            if let first = f.evidence.first {
                m.addItem(item("Open a Session Where It Happened") { [model] in model.openViewer(sessionId: first) })
            }
            m.addItem(item("See It in AgentPlay…") { [model] in model.openInsights() })
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
            ForEach(p.windows) { w in MeterRow(window: w, now: now) }
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
                if let tip = model.currentTip {
                    Text(tip.text).font(.subheadline).foregroundStyle(Ink.primary)
                        .lineLimit(2, reservesSpace: true).padding(.top, 3)
                        .id(model.tipIndex)
                        .transition(.opacity)
                }
            }
            Spacer(minLength: 0)
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: model.tipIndex)
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 6)
        // A fixed height: the menu measures rows once, and the tip changes under it.
        .frame(minWidth: Menu.width, maxWidth: .infinity, minHeight: 76, maxHeight: 76, alignment: .topLeading)
    }
}

/// A custom row that takes a click without closing the menu: Pip, in the header.
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
            let gain = insights.fixes.compactMap(\.points).reduce(0, +)
            if gain >= 0.5 {
                Text("The fixes below would take it to \(Int(min(100, score + gain).rounded())).")
                    .font(.subheadline).foregroundStyle(Ink.primary)
            }
            Text("Five parts, 20 points each. Every one counts something your sessions did.")
                .font(.footnote).foregroundStyle(Ink.tertiary).padding(.top, 2)
            ForEach(insights.components) { c in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(c.name).font(.subheadline).foregroundStyle(Ink.primary)
                        Text(Self.meaning[c.key] ?? "").font(.caption).foregroundStyle(Ink.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 4)
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color(nsColor: .quaternaryLabelColor))
                        Capsule().fill(tint(c)).frame(width: max(3, 64 * min(1, (c.points ?? 0) / 20)))
                    }
                    .frame(width: 64, height: 4)
                    Text(c.points.map { Format.points($0) } ?? "–").font(.subheadline.monospacedDigit())
                        .foregroundStyle(Ink.secondary).frame(width: 30, alignment: .trailing)
                }
                .padding(.top, 3)
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

/// What one live session is doing: who runs it, which model, how far its context is, and its last few steps.
struct SessionDetail: View {
    var line: SessionLine
    var now: Date

    var body: some View {
        let r = line.run
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(line.title).font(.headline).foregroundStyle(Ink.primary).lineLimit(1)
                Spacer()
                Text(status).font(.subheadline).foregroundStyle(line.dot == .waiting ? Color(nsColor: .systemOrange) : Ink.secondary)
            }
            Text(who).font(.subheadline).foregroundStyle(Ink.secondary)
            if let pct = r?.context.value?.percent {
                HStack(spacing: 8) {
                    Text("Context").font(.subheadline).foregroundStyle(Ink.primary)
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color(nsColor: .quaternaryLabelColor))
                        Capsule().fill(pct >= 90 ? Color(nsColor: .systemOrange) : Color(nsColor: .controlAccentColor))
                            .frame(width: max(3, 120 * min(1, pct / 100)))
                    }
                    .frame(width: 120, height: 4)
                    Text(Format.percent(pct)).font(.subheadline.monospacedDigit()).foregroundStyle(Ink.secondary)
                }
            }
            if let c = r?.counts, let calls = c.calls {
                Text([ "\(calls) tool calls", c.errors.map { "\($0) error\($0 == 1 ? "" : "s")" }, c.retries.map { "\($0) retr\($0 == 1 ? "y" : "ies")" }]
                    .compactMap { $0 }.joined(separator: " · "))
                    .font(.subheadline).foregroundStyle(Ink.secondary)
            }
            if let steps = r?.recentEvents.suffix(4), !steps.isEmpty {
                Text("Last steps").font(.caption.weight(.semibold)).foregroundStyle(Ink.tertiary).padding(.top, 2)
                ForEach(Array(steps.reversed())) { e in
                    HStack(alignment: .firstTextBaseline) {
                        Text(e.step).font(.caption).foregroundStyle(e.err ? Color(nsColor: .systemOrange) : Ink.primary).lineLimit(1)
                        Spacer(minLength: 6)
                        Text(Format.relative(e.at, now: now)).font(.caption.monospacedDigit()).foregroundStyle(Ink.tertiary)
                    }
                }
            } else if r == nil {
                Text("Its map isn't built yet. The replay and its steps appear after the next refresh.")
                    .font(.caption).foregroundStyle(Ink.tertiary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, Menu.inset)
        .padding(.vertical, 8)
        .frame(width: Menu.width, alignment: .leading)
    }

    var status: String {
        let since = line.live?.statusSince.map { Format.duration(now.timeIntervalSince($0)) }
        switch line.dot {
        case .waiting: return "Waiting on you" + (since.map { " · \($0)" } ?? "")
        case .working: return "Working" + (since.map { " · \($0)" } ?? "")
        default: return line.run?.liveStatus?.label ?? "Idle"
        }
    }

    var who: String {
        var bits = [AgentNames.label(line.live?.agentTool ?? line.run?.agentTool)]
        if let models = line.run?.models, !models.isEmpty { bits.append(models.map(Format.modelName).joined(separator: ", ")) }
        if let start = line.live?.startedAt ?? line.run?.startedAt { bits.append("started \(Format.relative(start, now: now))") }
        return bits.joined(separator: " · ")
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
        // One line: the name, the bar, the number. Two of these are a plan card; they must not stack up.
        HStack(alignment: .center, spacing: 10) {
            Text(title).font(.subheadline).foregroundStyle(Ink.primary).frame(width: 56, alignment: .leading)
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color(nsColor: .quaternaryLabelColor))
                    Capsule().fill(tint.color).frame(width: max(5, g.size.width * min(1, used / 100)))
                }
            }
            .frame(height: 5)
            Text(detail(stale, used: used)).font(.subheadline.monospacedDigit())
                .foregroundStyle(tint == .normal || tint == .stale ? Ink.secondary : tint.color)
                .lineLimit(1).fixedSize()
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title), \(detail(stale, used: used))")
    }

    var title: String { window.window == "five_hour" ? "Session" : window.windowLabel }

    func detail(_ stale: Bool, used: Double) -> String {
        if stale { return "\(Format.percent(used)) · stale, updates when \(window.providerLabel) runs" }
        // Ahead of pace: the time it runs out replaces the reset time, in the bar's own orange or red.
        if let pace = Self.pace(window, now: now), pace.level != .normal, let out = pace.runsOutAt {
            return "\(Format.percent(used)) · out in \(Format.duration(out.timeIntervalSince(now)))"
        }
        guard let r = window.resetsAt else { return Format.percent(used) }
        let left = r.timeIntervalSince(now)
        let reset = left < 86400 ? "resets in \(Format.duration(left))" : "resets \(Format.weekdayTime(r))"
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
