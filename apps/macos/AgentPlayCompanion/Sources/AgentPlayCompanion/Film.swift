import AgentPlayCore
import AppKit
import SwiftUI
import UserNotifications

/// Filming the app for a launch video (dev builds only): `--film <take>|all [--film-out <dir>] [--no-record]`.
///
/// Runs the real menu bar item, the real NSMenu and the real windows over a fixture, on a clean backdrop, drives
/// the pointer with synthesized mouse events, and records the filming display with `screencapture`. Every read
/// of this Mac is off (see `CompanionModel.startFilm`), so nothing private can land in a frame. Each take is a
/// short scene in `Film.takes`; `all` records every one in turn.
@MainActor
enum Film {
    static var model: CompanionModel!
    static var status: StatusItemController!
    private static var backdrop: NSWindow?
    static var outDir = URL(fileURLWithPath: "/tmp/agentplay-film")
    static let stage = StageState()

    // MARK: setup

    static func setUp(fixture: String, backdrop: Bool = true) throws {
        let defaults = UserDefaults(suiteName: PrefsStore.domain + ".film")!
        defaults.removePersistentDomain(forName: PrefsStore.domain + ".film")
        let m = CompanionModel(prefsStore: PrefsStore(defaults: defaults), isDevBuild: true)
        model = m
        Windows.model = m
        var snap = try SnapshotDecoder.decode(Data(contentsOf: fixtureURL(fixture)))
        snap.sample = nil
        snap = SnapshotDecoder.rebased(snap, to: Date())
        m.startFilm(snapshot: snap)
        // A fixed slot 150 pt from the right end of the bar: new items otherwise land leftmost, which a menu-bar
        // hiding utility (Ice, Hidden Bar, Bartender) folds away behind its chevron.
        UserDefaults.standard.set(Double(LaunchOptions.value("--film-slot") ?? "150") ?? 150, forKey: "NSStatusItem Preferred Position film-pip")
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.autosaveName = "film-pip"
        status = StatusItemController(model: m, statusItem: item)
        if backdrop { showBackdrop() }
    }

    static func fixtureURL(_ name: String) -> URL {
        if let u = Bundle.main.url(forResource: name, withExtension: "json", subdirectory: "Fixtures") { return u }
        let src = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return src.appendingPathComponent("Fixtures/\(name).json")
    }

    /// The filming display: the one with the menu bar, unless `--film-screen N` picks another.
    static var screen: NSScreen {
        let i = Int(LaunchOptions.value("--film-screen") ?? "0") ?? 0
        return NSScreen.screens[min(i, NSScreen.screens.count - 1)]
    }
    static var screenIndex: Int { NSScreen.screens.firstIndex(of: screen) ?? 0 }
    /// A full-screen app hides the menu bar; then the status item is nowhere to film.
    static var menuBarVisible: Bool { screen.visibleFrame.maxY < screen.frame.maxY - 1 }

    /// Borderless, yet able to be key: that is what moves the active menu bar to the filming display.
    final class FilmWindow: NSWindow {
        override var canBecomeKey: Bool { true }
        override var canBecomeMain: Bool { true }
    }

    /// A window that covers the filming display below the menu bar and above the Dock: the brand's near-black.
    static func showBackdrop() {
        let w = FilmWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
        w.level = NSWindow.Level(rawValue: 21)   // above the Dock (20), below the menu bar (24)
        w.isOpaque = true
        w.hasShadow = false
        w.collectionBehavior = [.canJoinAllSpaces, .stationary]
        w.contentView = NSHostingView(rootView: Backdrop())
        w.orderFrontRegardless()
        backdrop = w
        NSApp.activate(ignoringOtherApps: true)
        w.makeKeyAndOrderFront(nil)
    }

    struct Backdrop: View {
        var body: some View {
            ZStack {
                Color(red: 0x14 / 255, green: 0x14 / 255, blue: 0x15 / 255)
                RadialGradient(colors: [Color.white.opacity(0.05), .clear], center: .init(x: 0.8, y: 0.05), startRadius: 10, endRadius: 900)
            }
            .ignoresSafeArea()
        }
    }

    /// An invisible window the size of an alert, centred on the filming display: a sheet on it lands where a modal
    /// alert would, without a modal run loop.
    static weak var alert: NSAlert?

    static func alertHost(for a: NSAlert) -> NSWindow? {
        alert = a
        a.layout()
        let size = a.window.frame.size
        let f = screen.frame
        let w = FilmWindow(contentRect: NSRect(x: f.midX - size.width / 2, y: f.midY - size.height / 2, width: size.width, height: size.height),
                           styleMask: .borderless, backing: .buffered, defer: false)
        w.level = NSWindow.Level(rawValue: 23)
        w.isOpaque = false
        w.backgroundColor = .clear
        w.alphaValue = 1   // the sheet is a child window; the host itself draws nothing
        w.hasShadow = false
        w.orderFrontRegardless()
        return w
    }

    // MARK: geometry

    /// The status item's rect in screen points (Cocoa, origin bottom-left).
    static var pipRect: NSRect {
        guard let b = status.statusItem.button, let w = b.window else { return .zero }
        return w.convertToScreen(b.convert(b.bounds, to: nil))
    }

    /// Cocoa screen point to the global CG point mouse events use (origin top-left of the first screen).
    static func cg(_ p: NSPoint) -> CGPoint { CGPoint(x: p.x, y: NSScreen.screens[0].frame.maxY - p.y) }
    static func center(_ r: NSRect) -> CGPoint { cg(NSPoint(x: r.midX, y: r.midY)) }
    static var pipCenter: CGPoint { center(pipRect) }

    /// The menu bar's mean brightness over our backdrop, from a screenshot of the strip left of the status item.
    /// A translucent bar over the near-black backdrop reads about 40 to 70; a full-screen Space shows a black
    /// overlay bar or nothing at all (under 25), which `visibleFrame` does not report.
    static func barLuma() -> Double {
        let f = screen.frame
        let r = pipRect
        let x = Int(max(f.minX, r.minX - 260)), w = 200, h = Int(f.maxY - screen.visibleFrame.maxY)
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("agentplay-film-bar.png")
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        p.arguments = ["-x", "-D", "\(screenIndex + 1)", "-R", "\(x),0,\(w),\(max(h, 24))", tmp.path]
        try? p.run(); p.waitUntilExit()
        guard let img = NSImage(contentsOf: tmp), let rep = img.representations.first as? NSBitmapImageRep ?? img.tiffRepresentation.flatMap(NSBitmapImageRep.init) else { return -1 }
        var sum = 0.0, n = 0.0
        for y in stride(from: 0, to: rep.pixelsHigh, by: 4) {
            for xx in stride(from: 0, to: rep.pixelsWide, by: 4) {
                if let c = rep.colorAt(x: xx, y: y) { sum += Double(c.brightnessComponent) * 255; n += 1 }
            }
        }
        return n > 0 ? sum / n : -1
    }

    /// Waits for a visible menu bar and for the status item to sit in it (it slides in after launch).
    static func waitForPip() async {
        var warned = false
        for i in 0..<6000 {
            if menuBarVisible {
                let r = pipRect
                if screen.frame.contains(NSPoint(x: r.midX, y: r.midY)), r.minY > screen.frame.maxY - 40 {
                    // The bar must be the normal translucent one, twice in a row, before a take records.
                    var good = 0
                    while good < 2 {
                        let l = barLuma()
                        if l >= 28 && l <= 95 { good += 1 } else {
                            good = 0
                            print("film: the menu bar reads \(Int(l)) (a full-screen Space shows a black bar). Waiting for a normal bar.")
                            try? await Task.sleep(for: .seconds(6))
                        }
                        try? await Task.sleep(for: .milliseconds(400))
                    }
                    return
                }
                if i % 10 == 0 {
                    // A real click on our backdrop: that is what macOS takes as "the user is on this display".
                    await Cursor.click()
                    NSApp.activate(ignoringOtherApps: true); backdrop?.makeKeyAndOrderFront(nil)
                }
            } else if !warned {
                warned = true
                print("film: the menu bar is hidden on the filming display (a full-screen app?). Waiting for it.")
            }
            try? await Task.sleep(for: .milliseconds(50))
        }
        print("film: gave up waiting for the status item: \(pipRect)"); exit(3)
    }

    /// The open menu windows, the main menu first, each with its rows as screen rects in item order.
    static func menuWindows() -> [(window: NSWindow, rows: [NSRect])] {
        NSApp.windows
            .filter { $0.isVisible && $0.className.contains("Menu") && !$0.className.contains("StatusBar") }
            .sorted { $0.windowNumber < $1.windowNumber }
            .map { w in
                var rows: [NSRect] = []
                func walk(_ v: NSView) {
                    if v.className == "NSTableRowView" { rows.append(w.convertToScreen(v.convert(v.bounds, to: nil))) }
                    v.subviews.forEach(walk)
                }
                if let root = w.contentView { walk(root) }
                return (w, rows.sorted { $0.minY > $1.minY })
            }
    }

    /// The screen point to hover for the first main-menu item matching `pred`: the left third of its row.
    static func row(_ pred: (NSMenuItem) -> Bool) -> CGPoint? {
        let windows = menuWindows()
        let items = status.menu.items.filter { !$0.isAlternate }   // alternates share a row with the item above
        guard let main = windows.first, let i = items.firstIndex(where: pred), i < main.rows.count else {
            print("film: row not found; menu windows \(windows.count), rows \(windows.first?.rows.count ?? 0), items: "
                  + items.map { $0.isSeparatorItem ? "---" : ($0.view != nil ? "<view \(Int($0.view!.frame.height))>" : $0.title) }.joined(separator: " | "))
            return nil
        }
        let r = main.rows[i]
        return center(NSRect(x: r.minX + 60, y: r.minY, width: 1, height: r.height))
    }

    /// Hovers a row, then aims again: the menu can still be settling into place when the rows are first read.
    static func hover(_ pred: @escaping (NSMenuItem) -> Bool, over d: TimeInterval = 0.5, hold: TimeInterval = 1.2) async {
        guard let p = row(pred) else { return }
        await Cursor.moveTo(p, over: d)
        await sleep(0.15)
        if let q = row(pred), abs(q.y - Cursor.location().y) > 2 { await Cursor.moveTo(q, over: 0.12) }
        await sleep(hold)
    }

    static func sleep(_ s: Double) async { try? await Task.sleep(for: .milliseconds(Int(s * 1000))) }

    /// Opens the menu by clicking Pip, as a hand would, and waits for it to draw.
    static func openMenu() async {
        await Cursor.moveTo(pipCenter, over: 0.7)
        await sleep(0.9)                         // Pip looks up at the pointer
        await Cursor.moveTo(pipCenter, over: 0.1)
        await Cursor.click()
        await sleep(0.8)
    }

    /// Every take starts with no menu open and nothing beside Pip.
    static func reset() async {
        status.menu.cancelTracking()
        model.filmEvent(nil)
        await sleep(0.4)
    }

    /// Closes the menu with a click on the backdrop, well away from it.
    static func closeMenu() async {
        await Cursor.moveTo(cg(NSPoint(x: screen.frame.minX + 200, y: screen.frame.minY + 200)), over: 0.4)
        await Cursor.click()
        await sleep(0.5)
    }

    // MARK: the fixture's people

    static var live: [LiveSession] { model.snapshot?.live ?? [] }
    static var waiting: LiveSession? { live.first { $0.status == .waiting } }
    static var busy: LiveSession? { live.first { $0.status == .busy } }

    static func event(_ kind: PipEvent.Kind, _ l: LiveSession?, text: String, label: String) -> PipEvent {
        PipEvent(kind: kind, text: text, label: label, sessionId: l?.sessionId, pid: l?.pid, seq: nil, id: "film-\(label)-\(Date().timeIntervalSince1970)")
    }

    static func needsYouEvent() {
        guard let w = waiting else { return }
        model.filmEvent(event(.needsYou, w, text: "\(w.project ?? "A session") needs you", label: w.project ?? "docs-site"))
    }

    // MARK: takes

    /// Name, the scene, and how long the recording runs (a little over the scene; `screencapture` stops by itself).
    static let takes: [(name: String, run: () async -> Void, seconds: Int)] = [
        ("probe", probe, 8),
        ("needs-you", needsYou, 19),
        ("peek-click", peekClick, 17),
        ("menu-tour", menuTour, 23),
        ("fix-apply", fixApply, 21),
        ("limit", limit, 13),
        ("done-and-oops", doneAndOops, 15),
        ("poses", poses, 22),
        ("onboarding", onboarding, 23),
    ]

    /// Pip asleep, an agent starts working, then docs-site needs you: the wave, the line beside Pip, the notification.
    /// Clicks Pip for real and reports where macOS put the menu: its window frame against the usable screen,
    /// the first row, and how far the menu is scrolled (non-zero is the arrow at the top).
    static func probe() async {
        await Cursor.moveTo(pipCenter, over: 0.4)
        await sleep(0.3)
        await Cursor.click()
        await sleep(0.9)
        print("probe: pip \(pipRect) visibleMaxY \(screen.visibleFrame.maxY) frameMaxY \(screen.frame.maxY)")
        for (w, rows) in menuWindows() {
            var scroll = "none"
            func walk(_ v: NSView) {
                if let sv = v as? NSScrollView {
                    scroll = "docVisible.y=\(sv.documentVisibleRect.origin.y) docH=\(sv.documentView?.frame.height ?? 0) clipH=\(sv.contentView.bounds.height)"
                }
                v.subviews.forEach(walk)
            }
            if let root = w.contentView { walk(root) }
            print("probe: menu window \(w.frame) maxY \(w.frame.maxY) firstRow \(rows.first ?? .zero) rows \(rows.count) scroll \(scroll)")
        }
        for (i, it) in status.menu.items.enumerated() where it.view != nil {
            let v = it.view!
            print("probe: item \(i) \(type(of: v)) frame \(v.frame.size) intrinsic \(v.intrinsicContentSize) fitting \(v.fittingSize)")
        }
        status.menu.cancelTracking()
        await sleep(0.3)
    }

    static func needsYou() async {
        print("film: notifications \(model.notificationStatus.rawValue) (2 = authorized)")
        let all = live
        model.filmLive([])
        await sleep(2.0)
        model.filmLive(all.filter { $0.status == .busy })
        for _ in 0..<10 { await sleep(0.32); model.filmTick() }
        await sleep(0.8)
        model.filmLive(all)
        needsYouEvent()
        if let w = waiting, model.notificationStatus == .authorized { Notifier.postNeedsYou(w, includeRepo: true) }
        await sleep(7.0)
    }

    /// The pointer comes to Pip (it looks up), a click opens the menu, Pip hops through two suggestions.
    static func peekClick() async {
        needsYouEvent()
        await sleep(1.0)
        await Cursor.moveTo(pipCenter, over: 0.7)
        await sleep(1.0)                          // Pip looks up
        await Cursor.moveTo(pipCenter, over: 0.1)
        await Cursor.click()                      // acts on the line beside Pip: to the terminal
        await sleep(2.6)
        model.filmEvent(nil)
        await sleep(0.6)
        await Cursor.moveTo(pipCenter, over: 0.15)
        await Cursor.click()                      // now a click opens the menu
        await sleep(1.5)
        // Pip in the header: a click hops to the next suggestion.
        if let p = row({ $0.view is ClickHostingView }) {
            let pip = CGPoint(x: p.x - 20, y: p.y)
            await Cursor.moveTo(pip, over: 0.5); await sleep(0.4)
            await Cursor.click(); await sleep(1.6)
            await Cursor.click(); await sleep(1.8)
        }
        await closeMenu()
    }

    /// The menu, top to bottom: needs you, plan limits, sessions, insights (with submenus), usage details.
    static func menuTour() async {
        await openMenu()
        await hover({ $0.title == waiting?.project }, hold: 1.4)
        await hover({ ($0.view as? MenuHostingView).map { $0.frame.height > 90 } ?? false }, hold: 1.6)   // the Claude card
        await hover({ $0.title == "Show All Sessions…" }, hold: 0.8)
        await hover({ $0.title.hasPrefix("Run Health") }, hold: 1.8)                             // submenu opens
        await hover({ $0.title == "Steering" }, hold: 1.8)
        await hover({ $0.title.hasPrefix("Fix the") }, hold: 2.0)
        await hover({ $0.title == "Usage Details" }, hold: 2.4)
        await closeMenu()
    }

    /// From the fix row into its submenu, Apply…, the preview sheet, Apply, and the toast under Pip.
    static func fixApply() async {
        await openMenu()
        await hover({ $0.title.hasPrefix("Fix the") }, hold: 1.6)
        guard let fixItem = status.menu.items.first(where: { $0.title.hasPrefix("Fix the") }), let sub = fixItem.submenu else { return }
        await sleep(0.4)
        let windows = menuWindows()
        guard windows.count > 1, let i = sub.items.filter({ !$0.isAlternate }).firstIndex(where: { $0.title == "Apply…" }), i < windows[1].rows.count else {
            print("film: the fix submenu did not open"); return
        }
        // Slide right along the row into the submenu, then down to Apply… (a straight line keeps the submenu open).
        let r = windows[1].rows[i]
        await Cursor.moveTo(center(NSRect(x: r.minX + 40, y: windows[1].rows[0].midY, width: 1, height: 1)), over: 0.35)
        await Cursor.moveTo(center(NSRect(x: r.minX + 60, y: r.minY, width: 1, height: r.height)), over: 0.45)
        await sleep(1.0)
        await Cursor.click()
        await sleep(1.4)
        // The alert: its Apply button is the default, bottom right.
        func button(_ v: NSView) -> NSButton? {
            if let b = v as? NSButton, b.title == "Apply" { return b }
            for s in v.subviews { if let b = button(s) { return b } }
            return nil
        }
        await sleep(0.6)
        if let a = alert?.window, let b = a.contentView.flatMap(button) {
            await sleep(1.2)
            await Cursor.moveTo(center(a.convertToScreen(b.convert(b.bounds, to: nil))), over: 0.6)
            await sleep(0.5)
            await Cursor.click()
        } else {
            print("film: no Apply sheet")
        }
        await sleep(3.2)   // the toast under Pip
        status.menu.cancelTracking()
    }

    /// Going fast, then the session window is spent: the sweat drop, then the hourglass and the reset time.
    static func limit() async {
        model.filmLive(live.filter { $0.status == .busy })
        model.filmLimits(session: 0.91, weekly: 0.58, resetsIn: 48 * 60)
        await sleep(2.5)
        model.filmLimits(session: 1.0, weekly: 0.58, resetsIn: 48 * 60)
        let at = Format.clock(Date().addingTimeInterval(48 * 60))
        model.filmEvent(event(.limit, busy, text: "Out of session usage until \(at)", label: "until \(at)"))
        await sleep(5.0)
        model.filmLimits(session: 0.34, weekly: 0.58, resetsIn: 4 * 3600)
        model.filmEvent(nil)
        await sleep(1.0)
    }

    /// A session finishes (arms up), then a command fails three times (X eyes).
    static func doneAndOops() async {
        model.filmLive(live.filter { $0.status == .busy })
        for _ in 0..<5 { await sleep(0.32); model.filmTick() }
        let b = busy
        model.filmLive([])
        model.filmEvent(event(.finished, b, text: "\(b?.project ?? "A session") is done", label: b?.project ?? "payments-api"))
        await sleep(4.5)
        model.filmEvent(event(.failed, nil, text: "pnpm test failed 3×", label: "pnpm test ×3"))
        await sleep(4.5)
        model.filmEvent(nil)
        await sleep(0.6)
    }

    /// Pip large, every pose in turn, the body filling as the session is used. Close-ups come from here, never from
    /// magnifying menu-bar pixels.
    static func poses() async {
        let w = FilmWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 640), styleMask: .borderless, backing: .buffered, defer: false)
        w.level = NSWindow.Level(rawValue: 22)
        w.isOpaque = false
        w.backgroundColor = .clear
        w.contentView = NSHostingView(rootView: StageView(state: stage))
        let f = screen.frame
        w.setFrameOrigin(NSPoint(x: f.midX - 360, y: f.midY - 320))
        w.orderFrontRegardless()
        await Cursor.moveTo(cg(NSPoint(x: f.minX + 80, y: f.minY + 80)), over: 0.3)
        let script: [(PipPose, Double, Double)] = [(.sleep, 0.08, 2.2), (.work, 0.3, 2.6), (.wave, 0.45, 2.4), (.cheer, 0.55, 2.2),
                                                  (.oops, 0.62, 2.2), (.sweat, 0.9, 2.2), (.limit, 1.0, 2.6)]
        for (pose, fill, hold) in script {
            stage.pose = pose
            stage.session = fill
            if pose == .cheer { stage.hop = true; Task { await sleep(0.35); stage.hop = false } }
            await sleep(hold)
        }
        stage.pose = .sleep; stage.session = 0.08
        await sleep(1.2)
        w.orderOut(nil)
    }

    /// The first run, as Pip tells it: meet, read, learnt, home.
    static func onboarding() async {
        model.prefs.onboardingDone = false
        Windows.show(.welcome)
        guard let w = NSApp.windows.first(where: { $0.title == "Welcome to AgentPlay" }) else { return }
        w.level = NSWindow.Level(rawValue: 22)
        let f = screen.frame
        w.setFrameOrigin(NSPoint(x: f.midX - w.frame.width / 2, y: f.midY - w.frame.height / 2))
        w.makeKeyAndOrderFront(nil)
        await Cursor.moveTo(cg(NSPoint(x: w.frame.midX + 120, y: w.frame.minY - 60)), over: 0.3)
        await sleep(3.4)                                   // Pip wakes and waves
        func primary(_ title: String) -> CGPoint {
            let width = (title as NSString).size(withAttributes: [.font: NSFont.systemFont(ofSize: 13)]).width + 22
            return cg(NSPoint(x: w.frame.maxX - 22 - width / 2, y: w.frame.minY + 16 + 14))
        }
        await Cursor.moveTo(primary("Nice to Meet You"), over: 0.7); await sleep(0.4); await Cursor.click()
        await sleep(6.0)                                   // reading, then the learnings reveal themselves
        await Cursor.moveTo(primary("Continue"), over: 0.5); await sleep(0.3); await Cursor.click()
        await sleep(3.2)                                   // the legend
        await Cursor.moveTo(primary("Start"), over: 0.5); await sleep(0.3); await Cursor.click()
        await sleep(1.0)
    }

    // MARK: recording

    static func run(takeNamed name: String, record: Bool) {
        let chosen = name == "all" ? takes : takes.filter { $0.name == name }
        guard !chosen.isEmpty else { print("no take named \(name); have \(takes.map(\.name))"); exit(2) }
        Task {
            try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
            let park = Cursor.location()
            Cursor.warp(cg(NSPoint(x: screen.frame.minX + 40, y: screen.frame.minY + 40)))
            await waitForPip()
            let s0 = model.snapshot
            for take in chosen {
                await reset()
                model.filmLive(s0?.live ?? [])
                var rec: Process?
                if record {
                    let p = Process()
                    p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
                    p.arguments = ["-v", "-x", "-C", "-D", "\(screenIndex + 1)", "-V", "\(take.seconds)", outDir.appendingPathComponent("\(take.name).mov").path]
                    try? p.run()
                    rec = p
                    await sleep(1.8)
                }
                let started = Date()
                await take.run()
                await sleep(0.6)
                if let rec {
                    let took = Date().timeIntervalSince(started)
                    if took > Double(take.seconds) - 1.5 { print("film: \(take.name) ran \(Int(took)) s, longer than its \(take.seconds) s recording") }
                    rec.waitUntilExit()
                }
                print("take \(take.name): \(String(format: "%.1f", Date().timeIntervalSince(started))) s"
                      + (record ? " → \(outDir.appendingPathComponent("\(take.name).mov").path)" : ""))
                await sleep(0.8)
            }
            Cursor.warp(park)
            exit(0)
        }
    }
}

/// Pip, large, for the close-ups: the same template image the menu bar draws, at twelve times the size.
final class StageState: ObservableObject {
    @Published var pose: PipPose = .sleep
    @Published var session: Double = 0.1
    @Published var weekly: Double = 0.58
    @Published var hop = false
}

struct StageView: View {
    @ObservedObject var state: StageState

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.25)) { t in
            let tick = Int(t.date.timeIntervalSinceReferenceDate * 4)
            let blink = tick % 14 == 0 && state.pose != .sleep && state.pose != .limit
            let frame = (state.pose == .work || state.pose == .wave) ? tick / 2 : 0
            Image(nsImage: PipIcon.image(state.pose, frame: frame, session: state.session, weekly: state.weekly,
                                         eyes: blink ? .closed : .pose, hop: state.hop))
                .renderingMode(.template).interpolation(.none).resizable()
                .frame(width: 480, height: 432)
                .foregroundStyle(Color(red: 0xF2 / 255, green: 0xF2 / 255, blue: 0xF0 / 255))
        }
        .frame(width: 720, height: 640)
    }
}

/// The pointer, moved with synthesized events so the real menu bar, menu and windows react as they would to a hand.
enum Cursor {
    static func location() -> CGPoint { CGEvent(source: nil)?.location ?? .zero }
    static func warp(_ p: CGPoint) { CGWarpMouseCursorPosition(p) }

    static func moveTo(_ to: CGPoint, over d: TimeInterval) async {
        let from = location()
        let steps = max(1, Int(d * 90))
        for i in 1...steps {
            let t = Double(i) / Double(steps)
            let e = t < 0.5 ? 4 * t * t * t : 1 - pow(-2 * t + 2, 3) / 2   // in-out cubic
            let p = CGPoint(x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e)
            CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
            try? await Task.sleep(for: .milliseconds(max(1, Int(d * 1000) / steps)))
        }
    }

    static func click() async {
        let p = location()
        CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
        try? await Task.sleep(for: .milliseconds(90))
        CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    }
}

final class FilmDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        Notifier.register(delegate: self)
        MainActor.assumeIsolated {
            NSApp.mainMenu = AppDelegate.mainMenu()
            do { try Film.setUp(fixture: LaunchOptions.value("--fixture") ?? "film") } catch { print("film: \(error)"); exit(1) }
            Film.run(takeNamed: LaunchOptions.value("--film") ?? "all", record: !LaunchOptions.args.contains("--no-record"))
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async
        -> UNNotificationPresentationOptions { [.banner, .list] }
}
